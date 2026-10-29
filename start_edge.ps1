# 以调试模式启动 Edge（独立 profile），供后续 CDP 接管
#
# 参数原则：只保留必需的，任何多余的开关都可能触发 Chromium 的
# "不受支持的命令行标记" 横幅，进而被站点风控标记为异常实例。
# 特别地，不要加 --disable-blink-features=AutomationControlled。

$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) {
    Write-Output "Edge not found at $edge"
    exit 1
}

$profile = Join-Path $PSScriptRoot "browser-profile"

Start-Process -FilePath $edge -ArgumentList @(
    "--remote-debugging-port=9222",
    "--user-data-dir=$profile",
    "https://www.zhipin.com/"
)
Write-Output "edge launched, profile: $profile"
Write-Output "debug port: 9222"
