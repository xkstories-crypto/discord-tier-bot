# Botを起動する。すでに動いていれば止めてから入れ替える。
#   .\run.ps1        起動して、そのままログを流す
#   .\run.ps1 -Quiet 起動だけして抜ける

param([switch]$Quiet)

Set-Location $PSScriptRoot

$old = Get-Content bot.pid -ErrorAction SilentlyContinue
if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
    Stop-Process -Id $old -Force
    Write-Host "既存のBotを停止しました (PID $old)"
}

Remove-Item bot.out, bot.err -ErrorAction SilentlyContinue
$env:PYTHONUNBUFFERED = "1"

$p = Start-Process -FilePath python -ArgumentList "-u", "bot.py" `
    -RedirectStandardOutput bot.out -RedirectStandardError bot.err `
    -NoNewWindow -PassThru
$p.Id | Out-File -Encoding ascii bot.pid

Write-Host "起動しました (PID $($p.Id))"

if (-not $Quiet) {
    Write-Host "ログを表示します。Ctrl+C で抜けても Bot は動き続けます。"
    Write-Host ""
    Get-Content bot.err -Wait -Encoding UTF8
}
