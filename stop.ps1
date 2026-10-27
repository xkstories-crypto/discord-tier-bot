# Botを止める。手動起動でもタスクスケジューラ起動でも止まる。
#   .\stop.ps1

Set-Location $PSScriptRoot

$stopped = @()

# 1) 手動起動ぶん（bot.pid に記録がある）
$id = Get-Content bot.pid -ErrorAction SilentlyContinue
if ($id -and (Get-Process -Id $id -ErrorAction SilentlyContinue)) {
    Stop-Process -Id $id -Force
    $stopped += $id
}

# 2) タスクスケジューラ起動ぶん（pythonw で bot.py を実行しているもの）
#    タスクからの起動は bot.pid を書かないので、コマンドラインで探す
$procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*bot.py*" }
foreach ($p in $procs) {
    if ($stopped -notcontains $p.ProcessId) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped += $p.ProcessId
    }
}

if ($stopped.Count -gt 0) {
    Write-Host "停止しました (PID $($stopped -join ', '))"
} else {
    Write-Host "動いていません。"
}
