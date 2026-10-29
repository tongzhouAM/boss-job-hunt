# 清理上一次强杀 Python 留下的浏览器孤儿进程与 profile 锁文件
# 只杀命令行里带 job-hunt 路径的进程，不会碰用户自己开着的 Edge / Chrome
$killed = 0
foreach ($name in @('msedge.exe', 'chrome.exe')) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='$name'" |
        Where-Object { $_.CommandLine -like '*job-hunt*' }
    foreach ($p in $procs) {
        Write-Output "kill $name PID $($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Continue
        $killed++
    }
}
if ($killed -eq 0) { Write-Output "no leftover browser process using job-hunt profile" }

foreach ($l in @('SingletonLock', 'SingletonCookie', 'SingletonSocket')) {
    $path = Join-Path $PSScriptRoot "browser-profile\$l"
    if (Test-Path $path) {
        Remove-Item $path -Force
        Write-Output "removed lock: $l"
    }
}
Write-Output "cleanup done"
