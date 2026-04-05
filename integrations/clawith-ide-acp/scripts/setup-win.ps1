# Windows: 创建 .venv 并安装依赖。
# PowerShell:
#   cd <本目录>\integrations\clawith-ide-acp\scripts
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force   # 若提示无法运行脚本，仅需一次
#   .\setup-win.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m venv "$Root\.venv"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -m venv "$Root\.venv"
} else {
    Write-Error "未找到 Python。请安装 https://www.python.org/downloads/ 并勾选 Add to PATH，或安装 Windows Python Launcher (py)。"
}

$reqLock = Join-Path $Root "requirements.lock.txt"
$reqTxt = Join-Path $Root "requirements.txt"
$reqFile = if (Test-Path $reqLock) { $reqLock } else { $reqTxt }

& "$Root\.venv\Scripts\pip.exe" install -U pip
& "$Root\.venv\Scripts\pip.exe" install --no-cache-dir -r $reqFile
Write-Host "已安装依赖: $reqFile"

$envFile = Join-Path $Root ".env"
$example = Join-Path $Root "env.example"
if (-not (Test-Path $envFile)) {
    Copy-Item $example $envFile
    Write-Host "已创建 .env ，请编辑 CLAWITH_URL / CLAWITH_API_KEY 等。"
}

Write-Host "完成。运行: .\scripts\run-win.ps1"
Write-Host "JetBrains: 将 jetbrains\acp.json.example 中的路径改为本机绝对路径后写入 %USERPROFILE%\.jetbrains\acp.json"
