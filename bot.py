"""問題児ティア表Bot

  ヤバい発言に返信して Bot をメンションすると投票が立つ。
  1分後、Yes を押した人数ぶんポイントが入る。順位は毎月リセット。

設計書: .company/secretary/notes/problem-child-tier-bot-spec.md
"""

import asyncio
import hashlib
import json
import logging
import os
import socket
import sys
from datetime import timedelta
from logging.handlers import RotatingFileHandler

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import db
import report
import roles
from tally import Config, tally, PASSED, REJECTED, FALSE_REPORT, NO_QUORUM

_handlers = [
    # タスクスケジューラから起動すると画面がないので、ファイルに残す
    RotatingFileHandler("bot.log", maxBytes=1_000_000, backupCount=3,
                        encoding="utf-8"),
]
if sys.stderr is not None:
    # pythonw.exe には標準エラー出力がない。あるときだけ画面にも出す
    _handlers.append(logging.StreamHandler())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("tier")


def single_instance(port: int):
    """多重起動を防ぐ。ポートを1つ握って、取れなければ起動しない。

    タスクスケジューラの自動起動と手動起動が重なると、
    Botが2つDiscordに繋がってパネルが二重に出る。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
    except OSError:
        raise SystemExit(
            "すでにBotが起動しています。二重起動はできません。\n"
            "止めるには stop.ps1 を実行してください。")
    return s   # プロセスが終わるまで握り続ける

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = os.getenv("OWNER_ID", "")
GUILD_ID = os.getenv("GUILD_ID", "")

with open("config.json", encoding="utf-8") as f:
    CONF = json.load(f)
CFG = Config.from_dict(CONF)

SYNC_STATE = ".sync_state"     # 前回同期したコマンド構成のハッシュ

COLOR_VOTE = 0xA8402F
COLOR_PASS = 0xA8402F
COLOR_FAIL = 0x1F5F53
COLOR_VOID = 0x828C84


# ---------------------------------------------------------------- helpers

def jump_url(guild_id, channel_id, message_id) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def excerpt(text: str, limit: int = 300) -> str:
    text = (text or "").strip()
    if not text:
        return "（本文なし / 画像や添付のみ）"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def ts(dt, style: str = "R") -> str:
    """Discordの時刻表示。見る人のタイムゾーンで出る。"""
    return f"<t:{int(dt.timestamp())}:{style}>"


def is_owner(user_id) -> bool:
    return str(user_id) == str(OWNER_ID)


def write_summary() -> dict:
    """集計を書き出す。JSONは機械向け、tier.txt は人が読む用。

    どちらも tier.db から作る出力なので、消しても次の更新で作り直される。
    """
    data = db.summary()
    write_atomic("summary.json",
                 json.dumps(data, ensure_ascii=False, indent=2))
    write_atomic("tier.txt", report.text(data))
    return data


def write_atomic(path: str, body: str):
    """書き込み途中で落ちてもファイルが壊れないように差し替える。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def remember(user) -> None:
    """表示名を控える。IDだけの集計は読めないため。"""
    try:
        db.set_name(user.id, getattr(user, "display_name", None) or user.name)
    except Exception:
        pass


def vote_label(choice: bool) -> str:
    return "🔴 **Yes**（問題児）" if choice else "🟢 **No**（セーフ）"


# ---------------------------------------------------------------- bot

intents = discord.Intents.default()
intents.message_content = True


class TierBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!tier ", intents=intents, help_command=None)

    async def setup_hook(self):
        db.connect(tz_offset_hours=CONF.get("timezone_offset_hours", 9))
        db.set_monthly_reset(CONF.get("monthly_reset", True))
        db.set_expire_days(CONF.get("expire_days", 0),
                           CONF.get("expire_manual", True))
        self.add_view(VoteView())          # 再起動後もボタンを生かす
        await self.sync_commands()
        self.loop.create_task(heartbeat_loop())
        self.loop.create_task(role_sync_loop())

    async def sync_commands(self):
        """コマンドに変更があったときだけ同期する。

        毎回叩くとDiscordにレート制限され、起動が数分止まる。
        （開発中に何度も再起動すると普通に起きる）
        """
        sig = json.dumps(
            [[c.name, c.description,
              [(p.name, str(p.type), p.required)
               for p in getattr(c, "parameters", [])]]
             for c in sorted(self.tree.get_commands(), key=lambda c: c.name)],
            ensure_ascii=False, sort_keys=True,
        )
        want = hashlib.sha256(f"{GUILD_ID}:{sig}".encode()).hexdigest()

        try:
            with open(SYNC_STATE, encoding="utf-8") as f:
                have = f.read().strip()
        except OSError:
            have = ""

        if want == have and not CONF.get("force_sync"):
            log.info("コマンドに変更なし。同期をスキップします")
            return

        log.info("スラッシュコマンドを同期します…")
        try:
            if GUILD_ID:
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
        except discord.HTTPException as e:
            log.warning("同期に失敗しました（前回の内容のまま動きます）: %s", e)
            return

        with open(SYNC_STATE, "w", encoding="utf-8") as f:
            f.write(want)
        log.info("同期しました")


bot = TierBot()


# ---------------------------------------------------------------- 投票パネル

def vote_embed(target: discord.abc.User, reporter: discord.abc.User,
               content: str, link: str, deadline, count: int,
               extended: int = 0) -> discord.Embed:
    e = discord.Embed(
        title="この発言、問題児ポイント入れる？",
        description=f"> {excerpt(content).replace(chr(10), chr(10) + '> ')}\n\n[該当メッセージへ飛ぶ]({link})",
        color=COLOR_VOTE,
    )
    e.add_field(name="対象", value=target.mention, inline=True)
    e.add_field(name="通報者", value=reporter.mention, inline=True)
    e.add_field(name="締切", value=ts(deadline), inline=True)
    foot = f"{count}人が投票済み ・ 内訳は開票まで非公開 ・ 👀で自分の票を確認できます"
    if extended:
        foot += f" ・ 延長 +{extended}秒"
    e.set_footer(text=foot)
    return e


def apply_vote(poll_id, user_id, choice: bool):
    """票を入れる。(成功したか, 表示するメッセージ) を返す。

    Discordのボタンは全員に同じ見た目でしか出せないので、
    投票の受付と、自分用パネルの描画はここで分けている。
    """
    poll = db.get_poll(poll_id)
    uid = str(user_id)

    if poll is None or poll["status"] != "open":
        return False, "この投票はもう終わっています。"
    if db.now() >= db.parse(poll["deadline"]):
        return False, "締切を過ぎています。"
    if uid == poll["target_id"]:
        return False, "自分の案件には投票できません。"
    if uid == poll["reporter_id"]:
        return False, "通報した人は投票できません（自動で賛成扱いにもなりません）。"
    if CONF.get("members_only_voting", True) and not db.is_member(uid):
        return False, "投票できるのは参加者だけです。`/join` で参加してください。"

    db.cast_vote(poll_id, uid, choice)

    # ロスタイム: 締切間際に票が動いたら延長する
    extended = poll["extended_sec"]
    deadline = db.parse(poll["deadline"])
    remain = (deadline - db.now()).total_seconds()
    if (remain <= CONF["extend_threshold_sec"]
            and extended < CONF["extend_max_sec"]):
        add = min(CONF["extend_sec"], CONF["extend_max_sec"] - extended)
        deadline = deadline + timedelta(seconds=add)
        extended += add
        db.extend_poll(poll_id, deadline, extended)

    return True, ""


def my_panel(poll_id, choice):
    """自分にだけ見えるパネル。選んでいる方のボタンが光る。"""
    poll = db.get_poll(poll_id)
    if choice is None:
        body = "まだ投票していません。"
    else:
        body = f"いまの投票: {vote_label(choice)}"
    if poll is not None and poll["status"] == "open":
        body += (f"\n締切 {ts(db.parse(poll['deadline']))}"
                 " ・ それまで何度でも押し直せます。")
    else:
        body += "\nこの投票は終了しています。"
    return body, MyVoteView(poll_id, choice)


class MyVoteView(discord.ui.View):
    """自分専用の投票パネル。押している側のボタンに ✅ が付いて色が変わる。"""

    def __init__(self, poll_id, choice):
        super().__init__(timeout=900)
        self.poll_id = str(poll_id)

        yes = discord.ui.Button(
            label="✅ Yes（問題児）" if choice is True else "Yes（問題児）",
            style=(discord.ButtonStyle.danger if choice is True
                   else discord.ButtonStyle.secondary),
            custom_id=f"tier:my:yes:{self.poll_id}",
        )
        no = discord.ui.Button(
            label="✅ No（セーフ）" if choice is False else "No（セーフ）",
            style=(discord.ButtonStyle.success if choice is False
                   else discord.ButtonStyle.secondary),
            custom_id=f"tier:my:no:{self.poll_id}",
        )
        yes.callback = self._cb(True)
        no.callback = self._cb(False)
        self.add_item(yes)
        self.add_item(no)

    def _cb(self, choice: bool):
        async def callback(it: discord.Interaction):
            ok, err = apply_vote(self.poll_id, it.user.id, choice)
            if not ok:
                return await it.response.edit_message(content=err, view=None)
            body, view = my_panel(self.poll_id, choice)
            await it.response.edit_message(content=body, view=view)
            await refresh_panel(self.poll_id)
        return callback


class VoteView(discord.ui.View):
    """公開パネル。timeout=None + custom_id固定で、再起動をまたいでも反応する。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger,
                       custom_id="tier:vote:yes")
    async def yes(self, it: discord.Interaction, _):
        await self.cast(it, True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary,
                       custom_id="tier:vote:no")
    async def no(self, it: discord.Interaction, _):
        await self.cast(it, False)

    @discord.ui.button(label="自分の票", emoji="👀",
                       style=discord.ButtonStyle.secondary,
                       custom_id="tier:vote:check")
    async def check(self, it: discord.Interaction, _):
        """投票を変えずに、自分用パネルをもう一度出す。"""
        mine = db.votes_of(it.message.id).get(str(it.user.id))
        body, view = my_panel(it.message.id, mine)
        await it.response.send_message(body, view=view, ephemeral=True)

    async def cast(self, it: discord.Interaction, choice: bool):
        ok, err = apply_vote(it.message.id, it.user.id, choice)
        if not ok:
            return await it.response.send_message(err, ephemeral=True)
        body, view = my_panel(it.message.id, choice)
        await it.response.send_message(body, view=view, ephemeral=True)
        await refresh_panel(it.message.id)


async def refresh_panel(message_id):
    """投票人数だけを更新する。内訳は出さない。"""
    poll = db.get_poll(message_id)
    if poll is None or poll["status"] != "open":
        return
    try:
        channel = bot.get_channel(int(poll["channel_id"])) or \
            await bot.fetch_channel(int(poll["channel_id"]))
        msg = await channel.fetch_message(int(poll["message_id"]))
    except discord.HTTPException:
        return
    if not msg.embeds:
        return
    e = msg.embeds[0]
    foot = (f"{db.vote_count(message_id)}人が投票済み ・ 内訳は開票まで非公開"
            " ・ 👀で自分の票を確認できます")
    if poll["extended_sec"]:
        foot += f" ・ 延長 +{poll['extended_sec']}秒"
    e.set_footer(text=foot)
    for i, f in enumerate(e.fields):
        if f.name == "締切":
            e.set_field_at(i, name="締切",
                           value=ts(db.parse(poll["deadline"])), inline=True)
    try:
        await msg.edit(embed=e)
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------- 通報

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    if bot.user is None or f"<@{bot.user.id}>" not in message.content.replace("<@!", "<@"):
        return
    if message.reference is None or message.reference.message_id is None:
        return await message.reply(
            "問題児ポイントを入れたい発言に**返信**して、私をメンションしてください。",
            mention_author=False)

    await start_poll(message)


async def start_poll(trigger: discord.Message):
    reporter = trigger.author
    try:
        target_msg = await trigger.channel.fetch_message(
            trigger.reference.message_id)
    except discord.HTTPException:
        return await trigger.reply("元のメッセージが見つかりませんでした。",
                                   mention_author=False)

    target = target_msg.author

    if target.bot:
        return await trigger.reply("Botは対象にできません。", mention_author=False)
    if target.id == reporter.id:
        return await trigger.reply("自分で自分を通報はできません。",
                                   mention_author=False)
    if not db.is_member(str(reporter.id)):
        return await trigger.reply(
            "通報できるのは参加者だけです。`/join` で参加してください。",
            mention_author=False)
    if not db.is_member(str(target.id)):
        return await trigger.reply(
            f"{target.display_name} さんはティア表に参加していないので対象にできません。",
            mention_author=False)
    if db.has_open_poll_for(target_msg.id):
        return await trigger.reply("その発言はもう審議中です。",
                                   mention_author=False)

    # 時効: 古い発言は掘り返せない
    max_age = CONF.get("report_max_age_days", 0)
    if max_age:
        age = db.now() - target_msg.created_at
        if age > timedelta(days=max_age):
            return await trigger.reply(
                f"その発言は{max_age}日より前のものなので時効です。"
                f"（{ts(target_msg.created_at)}の発言）",
                mention_author=False)

    remember(target)
    remember(reporter)

    deadline = db.now() + timedelta(seconds=CONF["vote_duration_sec"])
    link = jump_url(trigger.guild.id, trigger.channel.id, target_msg.id)

    panel = await trigger.channel.send(
        embed=vote_embed(target, reporter, target_msg.content, link, deadline, 0),
        view=VoteView(),
    )

    thread_id = None
    if CONF.get("create_thread", False):
        try:
            thread = await panel.create_thread(
                name=f"審議: {target.display_name}"[:100],
                auto_archive_duration=60,
            )
            thread_id = thread.id
            await thread.send(
                "ここで弁明・野次をどうぞ。締切まで票は何度でも変えられます。")
        except discord.HTTPException:
            pass  # スレッドが作れなくても投票は成立させる

    db.create_poll(panel.id, trigger.channel.id, thread_id, target_msg.id,
                   target.id, reporter.id, deadline)
    bot.loop.create_task(run_poll(panel.id))
    log.info("poll opened %s target=%s", panel.id, target.id)


# ---------------------------------------------------------------- 締め

async def run_poll(message_id):
    """締切まで待つ。延長されたら待ち直す。"""
    while True:
        poll = db.get_poll(message_id)
        if poll is None or poll["status"] != "open":
            return
        remain = (db.parse(poll["deadline"]) - db.now()).total_seconds()
        if remain <= 0:
            break
        await asyncio.sleep(min(remain, 15))
    await close_poll(message_id)


async def close_poll(message_id):
    if not db.claim_poll(message_id):     # アトミック。二重集計が起きない
        return
    poll = db.get_poll(message_id)
    votes = db.votes_of(message_id)

    r = tally(
        votes,
        reporter_id=poll["reporter_id"],
        target_id=poll["target_id"],
        cfg=CFG,
        repeats_24h=db.repeats_24h(poll["target_id"]),
        gained_today=db.gained_today(poll["target_id"]),
    )

    target = f"<@{poll['target_id']}>"
    reporter = f"<@{poll['reporter_id']}>"

    if r.status == PASSED:
        db.record_conviction(poll["target_id"], poll["reporter_id"],
                             poll["target_message_id"], r.yes, r.no,
                             r.points, r.unanimous)
        total = current_points(poll["target_id"])
        title = "🏆 満場一致・殿堂入り" if r.unanimous else "可決"
        desc = (f"{target} に **+{r.points}pt**\n"
                f"Yes **{r.yes}** / No **{r.no}**\n"
                f"今シーズンの累計: **{total}pt**")
        if r.unanimous:
            desc += f"\n\n全員がYes。文句なしです。"
        if r.points == 0 and CFG.daily_cap > 0:
            desc += "\n（1日の上限に達しているため加点なし）"
        color = COLOR_PASS

    elif r.status == FALSE_REPORT:
        db.record_conviction(poll["reporter_id"], poll["target_id"],
                             poll["target_message_id"], r.yes, r.no,
                             r.reporter_points, False)
        title = "否決 ・ 言いがかり"
        desc = (f"Yes **{r.yes}** / No **{r.no}**\n"
                f"大差で否決されたので、通報した {reporter} に **+1pt**。")
        color = COLOR_FAIL

    elif r.status == REJECTED:
        title = "否決"
        desc = f"Yes **{r.yes}** / No **{r.no}**\n変動なし。"
        color = COLOR_FAIL

    else:  # NO_QUORUM
        title = "不成立"
        desc = (f"投票が **{r.yes + r.no}票** しかなく、"
                f"{CFG.quorum}票に届きませんでした。変動なし。")
        color = COLOR_VOID

    await announce(poll, title, desc, color)
    db.finish_poll(message_id, "closed")
    write_summary()
    log.info("poll closed %s status=%s pt=%s", message_id, r.status, r.points)
    await sync_rank_roles()


async def void_poll(poll, reason: str):
    if not db.claim_poll(poll["message_id"]):
        return
    await announce(poll, "無効",
                   f"{reason}\n必要ならもう一度通報してください。", COLOR_VOID)
    db.finish_poll(poll["message_id"], "void")
    log.info("poll voided %s", poll["message_id"])


async def announce(poll, title: str, desc: str, color: int):
    """パネルを結果表示に差し替え、ボタンを外す。"""
    try:
        channel = bot.get_channel(int(poll["channel_id"])) or \
            await bot.fetch_channel(int(poll["channel_id"]))
    except discord.HTTPException:
        return

    e = discord.Embed(title=f"開票 ・ {title}", description=desc, color=color)
    try:
        msg = await channel.fetch_message(int(poll["message_id"]))
        await msg.edit(embed=e, view=None)
        await msg.reply(embed=e, mention_author=False)
    except discord.HTTPException:
        try:
            await channel.send(embed=e)
        except discord.HTTPException:
            pass


def current_points(user_id) -> int:
    for row in db.ranking(limit=200):
        if row["target_id"] == str(user_id):
            return row["pt"]
    return 0


# ---------------------------------------------------------------- 上位ロール

ROLE_STATE = ".role_state"     # 作ったロールのIDと、今誰に付けているか

# 付け外しは「自分が付けた人」の記録を頼りに行う。
# メンバー一覧の取得には特権インテントが要るが、記録があれば要らずに済む。


def _load_role_state() -> dict:
    try:
        with open(ROLE_STATE, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("roles", {})      # "0" -> role_id
    s.setdefault("holders", {})    # "0" -> [user_id]
    s.setdefault("crowned", {})    # user_id -> 王冠を付ける前のニックネーム
    return s


def _save_role_state(state: dict) -> None:
    write_atomic(ROLE_STATE, json.dumps(state, ensure_ascii=False, indent=2))


def rank_guild():
    if not GUILD_ID:
        return None
    return bot.get_guild(int(GUILD_ID))


async def ensure_rank_roles(guild, state: dict) -> list:
    """設定された名前のロールを用意する。無ければ作る。

    一度作ったらIDを覚えるので、Discord側で名前を変えても作り直さない。
    """
    names = CONF.get("rank_roles") or []
    try:
        color = discord.Color.from_str(CONF.get("rank_role_color", "#A8402F"))
    except ValueError:
        color = discord.Color.default()

    out = []
    for i, name in enumerate(names):
        role = None
        rid = state["roles"].get(str(i))
        if rid:
            role = guild.get_role(int(rid))
        if role is None:
            role = discord.utils.get(guild.roles, name=name)
        if role is None:
            try:
                role = await guild.create_role(
                    name=name, colour=color,
                    hoist=CONF.get("rank_role_hoist", True),
                    mentionable=False,
                    reason="ティア表の上位ロール",
                )
                log.info("role created %s (%s)", name, role.id)
            except discord.Forbidden:
                log.warning("ロールを作れません。Botに「ロールの管理」権限が要ります")
                return []
            except discord.HTTPException as e:
                log.warning("ロールを作れませんでした: %s", e)
                return []
        state["roles"][str(i)] = str(role.id)
        out.append(role)
    return out


async def _member(guild, user_id):
    m = guild.get_member(int(user_id))
    if m is not None:
        return m
    try:
        return await guild.fetch_member(int(user_id))
    except discord.HTTPException:
        return None        # 抜けた人。放っておいてよい


async def sync_crown(guild, state: dict, winners: set) -> None:
    """1位のニックネームの末尾に王冠を付ける。

    元の名前は付ける前に控えておき、順位が落ちたらそこへ戻す。
    サーバーのオーナーだけは Discord の仕様でBotが改名できないため、
    その人が1位のときは王冠が付かない（ロールは普通に付く）。
    """
    emoji = CONF.get("crown_emoji") or ""
    if not emoji:
        return

    crowned = state["crowned"]

    for uid in list(crowned):
        if uid in winners:
            continue
        m = await _member(guild, uid)
        if m is None:
            del crowned[uid]              # 抜けた人。戻す先がない
            continue
        try:
            await m.edit(nick=crowned[uid] or None,
                         reason="ティア表の1位から落ちたため")
        except discord.HTTPException as e:
            # 控えを消すと元の名前が分からなくなる。次回また試すために残す
            log.warning("ニックネームを戻せませんでした (%s): %s", uid, e)
            continue
        del crowned[uid]
        log.info("crown - %s", uid)

    for uid in winners:
        if uid in crowned:
            continue
        m = await _member(guild, uid)
        if m is None:
            continue
        if m.id == guild.owner_id:
            log.info("crown skip %s（サーバーのオーナーは改名できません）", uid)
            continue
        prev = m.nick or ""              # 空文字＝元は未設定。戻すときは None
        base = m.nick or m.display_name
        try:
            await m.edit(nick=roles.crowned_nick(base, emoji),
                         reason="ティア表の1位になったため")
        except discord.Forbidden:
            log.warning("ニックネームを変えられません。Botに「ニックネームの管理」"
                        "権限と、その人より上のロールが要ります (%s)", uid)
            continue
        except discord.HTTPException as e:
            log.warning("ニックネームを変えられませんでした (%s): %s", uid, e)
            continue
        crowned[uid] = prev
        log.info("crown + %s", uid)


async def sync_rank_roles() -> None:
    """ティア表の上位3名にロールを合わせる。

    ptが動くたびに呼ぶ。差分だけ触るので、順位が変わらなければ通信は起きない。
    """
    if not CONF.get("rank_roles") and not CONF.get("crown_emoji"):
        return
    guild = rank_guild()
    if guild is None:
        return

    state = _load_role_state()
    role_objs = await ensure_rank_roles(guild, state)

    depth = max(len(role_objs), 1)     # ロールが無くても1位は王冠のために要る
    ranks = roles.top_ranks(db.ranking(limit=200), depth=depth)
    want = {str(r.index): set(r.user_ids) for r in ranks}

    for i, role in enumerate(role_objs):
        key = str(i)
        target = want.get(key, set())
        have = set(state["holders"].get(key, []))

        # 記録するのは「実際にDiscordが受け付けたぶん」だけ。
        # 失敗を成功として書くと差分が消え、権限を直しても二度と再試行されない。
        now_has = set(have)

        for uid in have - target:
            m = await _member(guild, uid)
            if m is None:
                now_has.discard(uid)      # 抜けた人。追いかけても仕方ない
                continue
            try:
                await m.remove_roles(role, reason="ティア表の順位が下がったため")
                now_has.discard(uid)
                log.info("role - %s %s", role.name, uid)
            except discord.HTTPException as e:
                log.warning("ロールを外せませんでした (%s): %s", uid, e)

        for uid in target - have:
            m = await _member(guild, uid)
            if m is None:
                continue
            try:
                await m.add_roles(role, reason="ティア表の上位に入ったため")
                now_has.add(uid)
                log.info("role + %s %s", role.name, uid)
            except discord.Forbidden:
                # Botより上に並べ替えられると、ここだけが静かに落ち続ける
                log.warning("ロール %s を付けられません。"
                            "Botのロールをこれより上に置いてください", role.name)
                break
            except discord.HTTPException as e:
                log.warning("ロールを付けられませんでした (%s): %s", uid, e)

        state["holders"][key] = sorted(now_has)

    await sync_crown(guild, state, want.get("0", set()))
    _save_role_state(state)


# ---------------------------------------------------------------- 起動・復帰

async def heartbeat_loop():
    """生存時刻を刻む。落ちていた区間を後から判定するために使う。"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        db.beat()
        await asyncio.sleep(CONF.get("heartbeat_sec", 30))


async def role_sync_loop():
    """ロールを定期的に順位へ合わせ直す。

    時効があると、**誰も何もしなくても**古い記録が落ちて順位が変わる。
    投票のたびに合わせるだけでは、その変化を取りこぼす。
    差分がなければ何も起きないので、空振りしても負荷にならない。
    """
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(CONF.get("role_sync_sec", 600))
        try:
            write_summary()
            await sync_rank_roles()
        except Exception:
            log.exception("ロールの定期同期に失敗しました")


_announced = False        # on_ready は再接続でも呼ばれる。通知は起動1回だけ


async def notify_channel():
    """通知先を解決する。チャンネルIDでもサーバーIDでも受け付ける。"""
    raw = str(CONF.get("notify_channel_id") or "").strip()
    if not raw.isdigit():
        return None
    cid = int(raw)

    ch = bot.get_channel(cid)
    if ch is None:
        try:
            ch = await bot.fetch_channel(cid)
        except discord.HTTPException:
            ch = None
    if isinstance(ch, discord.abc.Messageable):
        return ch

    # サーバーIDだった場合は、書き込めるテキストチャンネルを選ぶ
    guild = bot.get_guild(cid)
    if guild is None:
        log.warning("notify_channel_id %s が見つかりません"
                    "（Botがそのサーバーに招待されていない可能性）", cid)
        return None
    me = guild.me
    candidates = [guild.system_channel] if guild.system_channel else []
    candidates += list(guild.text_channels)
    for c in candidates:
        if c and c.permissions_for(me).send_messages:
            log.info("notify: サーバーID指定のため #%s を選びました", c.name)
            return c
    log.warning("サーバー %s に書き込めるチャンネルがありません", guild.name)
    return None


async def announce_startup(restored: int, voided: int):
    ch = await notify_channel()
    if ch is None:
        return
    e = discord.Embed(title="ティア表Bot 起動しました", color=COLOR_FAIL)
    e.add_field(name="設定",
                value=(f"定足数 {CFG.quorum}票 ・ 投票 "
                       f"{CONF['vote_duration_sec']}秒\n"
                       f"月次リセット: "
                       f"{'あり' if db.monthly_reset() else 'なし（通算）'}"),
                inline=False)
    if restored or voided:
        e.add_field(name="停止中の投票",
                    value=f"再開 {restored}件 ・ 無効 {voided}件", inline=False)
    e.set_footer(text=f"{db.scope_label() or '通算'} ・ "
                      f"参加 {len(db.member_ids())}人")
    try:
        await ch.send(embed=e)
    except discord.HTTPException as err:
        log.warning("起動通知を送れませんでした: %s", err)


@bot.event
async def on_ready():
    global _announced
    log.info("logged in as %s", bot.user)
    log.info("参加サーバー: %s",
             ", ".join(f"{g.name}({g.id})" for g in bot.guilds) or "なし")
    restored, voided = await restore_polls()
    write_summary()
    await sync_rank_roles()
    if not _announced:
        _announced = True
        await announce_startup(restored, voided)


async def restore_polls():
    hb = db.last_heartbeat()
    now = db.now()
    restored = voided = 0

    for poll in db.open_polls():
        deadline = db.parse(poll["deadline"])

        if now >= deadline:
            # 締切をまたいで落ちていたなら、票が途中で切れている
            if hb is None or hb < deadline:
                await void_poll(poll, "Botが停止していたため、この投票は無効になりました。")
                voided += 1
            else:
                await close_poll(poll["message_id"])
            continue

        # まだ締切前。落ちていた時間ぶん締切を延ばして再開する
        if hb is not None:
            downtime = (now - hb).total_seconds()
            if downtime > 60:
                add = min(downtime, CONF["vote_duration_sec"])
                db.extend_poll(poll["message_id"],
                               deadline + timedelta(seconds=add),
                               poll["extended_sec"])
                log.info("poll %s extended by %ss (downtime)",
                         poll["message_id"], int(add))
        bot.loop.create_task(run_poll(poll["message_id"]))
        restored += 1

    return restored, voided


# ---------------------------------------------------------------- コマンド

@bot.tree.command(name="join", description="ティア表に参加する")
async def cmd_join(it: discord.Interaction):
    if db.join(str(it.user.id), "self"):
        remember(it.user)
        write_summary()
        await it.response.send_message(
            f"{it.user.mention} がティア表に参加しました。逃げても無駄です。")
    else:
        await it.response.send_message("もう参加しています。", ephemeral=True)


@bot.tree.command(name="leave", description="[オーナー] ティア表から抜ける")
async def cmd_leave(it: discord.Interaction):
    if not is_owner(it.user.id):
        return await it.response.send_message("逃がしません。", ephemeral=True)
    if db.leave(str(it.user.id)):
        await it.response.send_message(
            f"{it.user.mention} がティア表から抜けました。\n"
            "※ ポイントは消えません。戻ってきたらそのまま復活します。")
    else:
        await it.response.send_message("参加していません。", ephemeral=True)


@bot.tree.command(name="add", description="[オーナー] メンバーをティア表に追加する")
@app_commands.describe(user="追加する人")
async def cmd_add(it: discord.Interaction, user: discord.Member):
    if not is_owner(it.user.id):
        return await it.response.send_message("このコマンドは使えません。",
                                              ephemeral=True)
    if db.join(str(user.id), str(it.user.id)):
        remember(user)
        write_summary()
        await it.response.send_message(
            f"{user.mention} がティア表に参加しました。")
    else:
        await it.response.send_message("もう参加しています。", ephemeral=True)


@bot.tree.command(name="remove", description="[オーナー] メンバーをティア表から外す")
@app_commands.describe(user="外す人")
async def cmd_remove(it: discord.Interaction, user: discord.Member):
    if not is_owner(it.user.id):
        return await it.response.send_message("このコマンドは使えません。",
                                              ephemeral=True)
    if db.leave(str(user.id)):
        await it.response.send_message(f"{user.mention} をティア表から外しました。")
    else:
        await it.response.send_message("参加していません。", ephemeral=True)


@bot.tree.command(name="members", description="ティア表の参加者を見る")
async def cmd_members(it: discord.Interaction):
    ids = db.member_ids()
    if not ids:
        return await it.response.send_message(
            "まだ誰も参加していません。`/join` からどうぞ。")
    body = "\n".join(f"<@{i}>" for i in ids)
    e = discord.Embed(title=f"参加者 {len(ids)}人", description=body,
                      color=COLOR_FAIL)
    await it.response.send_message(embed=e)


@bot.tree.command(name="tier", description="今シーズンのティア表を見る")
async def cmd_tier(it: discord.Interaction):
    rows = db.ranking()
    season = db.scope_label()
    if not rows:
        head = f"**{season}**\n" if season else ""
        return await it.response.send_message(
            head + "まだ誰も問題を起こしていません。平和ですね。")

    lines = []
    for rank, row in enumerate(rows, 1):
        mark = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")
        lines.append(f"{mark} <@{row['target_id']}> — "
                     f"**{row['pt']}pt** ({row['cases']}件)")

    title = f"問題児ティア表 ・ {season}" if season else "問題児ティア表"
    e = discord.Embed(title=title, description="\n".join(lines),
                      color=COLOR_PASS)
    notes = []
    if db.expire_days():
        notes.append(f"{db.expire_days()}日で時効。それより古い記録は数えません")
    if db.monthly_reset():
        notes.append("月が変わるとリセットされます")
    if notes:
        e.set_footer(text=" ・ ".join(notes))
    await it.response.send_message(embed=e)


@bot.tree.command(name="adjust",
                  description="[オーナー] ポイントを手で足し引きする（基準値の設定・取り消し）")
@app_commands.describe(user="対象", points="足すポイント（マイナスで減らす）",
                       reason="理由（任意）", quiet="結果を自分だけに見せる")
async def cmd_adjust(it: discord.Interaction, user: discord.Member,
                     points: int, reason: str = "手動調整",
                     quiet: bool = False):
    if not is_owner(it.user.id):
        return await it.response.send_message("このコマンドは使えません。",
                                              ephemeral=True)
    if points == 0:
        return await it.response.send_message("0は指定できません。", ephemeral=True)

    if not db.is_member(str(user.id)):
        db.join(str(user.id), str(it.user.id))
    remember(user)

    total = db.adjust_points(user.id, it.user.id, points, reason)
    write_summary()
    await sync_rank_roles()
    sign = f"+{points}" if points > 0 else str(points)
    log.info("adjust %s %s by %s (%s)", user.id, sign, it.user.id, reason)

    e = discord.Embed(
        title="ポイントを調整しました",
        description=(f"{user.mention} に **{sign}pt**\n"
                     f"理由: {reason}\n"
                     f"今シーズンの累計: **{total}pt**"),
        color=COLOR_PASS if points > 0 else COLOR_FAIL,
    )
    await it.response.send_message(embed=e, ephemeral=quiet)


@bot.tree.command(name="roles",
                  description="[オーナー] 上位ロールを今の順位に合わせ直す")
async def cmd_roles(it: discord.Interaction):
    """普段は自動で合う。手で付け外ししてズレたときの復旧用。"""
    if not is_owner(it.user.id):
        return await it.response.send_message("このコマンドは使えません。",
                                              ephemeral=True)
    if not CONF.get("rank_roles"):
        return await it.response.send_message(
            "config.json の rank_roles が空です。", ephemeral=True)

    await it.response.defer(ephemeral=True)
    await sync_rank_roles()

    state = _load_role_state()
    lines = []
    for i, name in enumerate(CONF["rank_roles"]):
        who = state["holders"].get(str(i), [])
        holders = "、".join(f"<@{u}>" for u in who) or "なし"
        lines.append(f"**{name}**: {holders}")
    await it.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="reload", description="[オーナー] config.json を読み直す")
async def cmd_reload(it: discord.Interaction):
    """再起動せずに調整を反映する。進行中の投票はそのまま続く。"""
    global CFG
    if not is_owner(it.user.id):
        return await it.response.send_message("このコマンドは使えません。",
                                              ephemeral=True)
    try:
        with open("config.json", encoding="utf-8") as f:
            new = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return await it.response.send_message(
            f"読み込めませんでした。前の設定のままです。\n```{e}```", ephemeral=True)

    CONF.clear()
    CONF.update(new)
    CFG = Config.from_dict(CONF)
    db.set_monthly_reset(CONF.get("monthly_reset", True))
    db.set_expire_days(CONF.get("expire_days", 0),
                       CONF.get("expire_manual", True))
    write_summary()
    await sync_rank_roles()
    log.info("config reloaded")

    body = "\n".join(f"{k} = {v}" for k, v in CONF.items())
    e = discord.Embed(title="設定を読み直しました",
                      description=f"```\n{body}\n```", color=COLOR_FAIL)
    e.set_footer(text="進行中の投票はそのまま続きます")
    await it.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="hall", description="殿堂入り（満場一致で可決された案件）")
async def cmd_hall(it: discord.Interaction):
    rows = db.hall_of_fame()
    if not rows:
        return await it.response.send_message("まだ殿堂入りはありません。")
    lines = [f"<@{r['target_id']}> — {r['season']} ・ 全会一致 {r['yes']}票"
             for r in rows]
    e = discord.Embed(title="🏆 殿堂", description="\n".join(lines),
                      color=COLOR_PASS)
    e.set_footer(text="満場一致の記録はリセットされません")
    await it.response.send_message(embed=e)


# ---------------------------------------------------------------- entry

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            ".env に DISCORD_TOKEN がありません。"
            ".env.example をコピーして .env を作ってください。")
    _lock = single_instance(CONF.get("lock_port", 53997))
    bot.run(TOKEN, log_handler=None)
