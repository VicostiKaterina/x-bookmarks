# X 书签 · 一键安装（Windows）
#
# 用法（任选一种）：
#   1. 在文件夹空白处 Shift + 右键 →「在此处打开 PowerShell 窗口」→ 输入： .\install.ps1
#   2. 已经打开了 PowerShell： powershell -ExecutionPolicy Bypass -File install.ps1
#
# 它会做完这些事：找一个 Python（3.9+）→ 建虚拟环境 .venv → 装依赖 → 启动。
# 只装一个必需依赖（websocket-client）。不想装虚拟环境就直接看 README 的「手动安装」。

param(
    [switch]$NoLaunch,          # 只安装，不启动
    [string]$Port = ""          # 指定端口，例如 -Port 8900
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $here

function Say($msg) { Write-Host $msg -ForegroundColor Cyan }
function Fail($msg) {
    Write-Host ""
    Write-Host "✗ $msg" -ForegroundColor Red
    Write-Host ""
    Write-Host "也可以手动来：" -ForegroundColor Yellow
    Write-Host "  python -m venv .venv"
    Write-Host "  .venv\Scripts\pip install -r requirements.txt"
    Write-Host "  .venv\Scripts\python serve.py --open"
    exit 1
}

if (-not (Test-Path 'serve.py')) { Fail "这个脚本要放在 X 书签的文件夹里运行（找不到 serve.py）" }

# ---------- 1. 找 Python ----------
Say "[1/3] 找 Python…"
$pyExe = $null
$pyArgs = @()

foreach ($cand in @(@('py', @('-3')), @('python', @()), @('python3', @()))) {
    $exe = $cand[0]; $pre = $cand[1]
    $cmd = Get-Command $exe -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
        $ver = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    } catch { continue }
    if (-not $ver) { continue }
    $parts = $ver.Trim().Split('.')
    if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 9)) {
        Write-Host "  $exe 是 Python $ver，太老了（要 3.9+），继续找…" -ForegroundColor Yellow
        continue
    }
    $pyExe = $exe; $pyArgs = $pre
    Say "  找到 Python $ver（$exe）"
    break
}

if (-not $pyExe) {
    Write-Host ""
    Write-Host "✗ 没找到 Python 3.9 以上版本。" -ForegroundColor Red
    Write-Host "  去 https://www.python.org/downloads/ 下载安装，安装时**一定要勾选 Add python.exe to PATH**，"
    Write-Host "  装完关掉这个窗口重开，再跑一次本脚本。"
    exit 1
}

# ---------- 2. 虚拟环境 ----------
$vpy = Join-Path $here '.venv\Scripts\python.exe'
if (Test-Path $vpy) {
    Say "[2/3] 虚拟环境已存在，跳过"
} else {
    Say "[2/3] 建虚拟环境 .venv…"
    & $pyExe @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $vpy)) { Fail "建虚拟环境失败（venv 模块缺失？某些精简版 Python 会删掉它）" }
}

# ---------- 3. 装依赖 ----------
Say "[3/3] 装依赖…"
& $vpy -m pip install --upgrade pip --quiet --disable-pip-version-check 2>$null | Out-Null
& $vpy -m pip install -r requirements.txt --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    Write-Host "  官方源没装上，换清华镜像再试一次…" -ForegroundColor Yellow
    & $vpy -m pip install -r requirements.txt --quiet --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple
    if ($LASTEXITCODE -ne 0) { Fail "依赖没装上（多半是网络/代理问题）" }
}

# 自检：真能 import 才算装好
& $vpy -c "import websocket" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "依赖装上了但 import 失败，试试删掉 .venv 重跑本脚本" }

Say ""
Say "✓ 装好了。"

if ($NoLaunch) {
    Say "启动方式：双击 start.bat，或 .venv\Scripts\python serve.py --open"
    exit 0
}

# ---------- 启动 ----------
$portArg = if ($Port) { @('--port', $Port) } else { @() }
Say "正在启动…浏览器会自动打开（关掉这个黑窗口不会退出，要停就在 App 里点「退出」或关掉窗口）"
& (Join-Path $here '.venv\Scripts\python.exe') serve.py --open @portArg
