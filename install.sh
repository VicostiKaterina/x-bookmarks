#!/usr/bin/env bash
# X 书签 · 一键安装（macOS / Linux）
#
# 用法：
#   cd 到本文件夹，然后： bash install.sh
#   或： chmod +x install.sh && ./install.sh
#
# 它会做完这些事：找一个 Python（3.9+）→ 建虚拟环境 .venv → 装依赖 → 启动。
# 只装一个必需依赖（websocket-client）。不想装虚拟环境就直接看 README 的「手动安装」。

set -euo pipefail
cd "$(dirname "$0")"

NO_LAUNCH=0
PORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-launch) NO_LAUNCH=1; shift ;;
    --port) PORT="${2:-}"; shift 2 ;;
    *) echo "未知参数：$1"; exit 1 ;;
  esac
done

say()  { printf '\033[36m%s\033[0m\n' "$*"; }
fail() {
  printf '\n\033[31m✗ %s\033[0m\n\n' "$*"
  printf '\033[33m也可以手动来：\033[0m\n'
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/pip install -r requirements.txt"
  echo "  .venv/bin/python serve.py --open"
  exit 1
}

[ -f serve.py ] || fail "这个脚本要放在 X 书签的文件夹里运行（找不到 serve.py）"

# ---------- 1. 找 Python ----------
say "[1/3] 找 Python…"
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver="$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
    [ -n "$ver" ] || continue
    major="${ver%%.*}"; minor="${ver#*.}"
    if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 9 ]; }; then
      echo "  $cand 是 Python $ver，太老了（要 3.9+），继续找…"
      continue
    fi
    PY="$cand"
    say "  找到 Python $ver（$cand）"
    break
  fi
done
[ -n "$PY" ] || fail "没找到 Python 3.9 以上版本。macOS 可以用 brew install python；Ubuntu 用 sudo apt install python3 python3-venv"

# ---------- 2. 虚拟环境 ----------
VPY=".venv/bin/python"
if [ -x "$VPY" ]; then
  say "[2/3] 虚拟环境已存在，跳过"
else
  say "[2/3] 建虚拟环境 .venv…"
  "$PY" -m venv .venv || fail "建虚拟环境失败（Debian/Ubuntu 需要先 sudo apt install python3-venv）"
fi

# ---------- 3. 装依赖 ----------
say "[3/3] 装依赖…"
"$VPY" -m pip install --upgrade pip --quiet --disable-pip-version-check || true
if ! "$VPY" -m pip install -r requirements.txt --quiet --disable-pip-version-check; then
  echo "  官方源没装上，换清华镜像再试一次…"
  "$VPY" -m pip install -r requirements.txt --quiet --disable-pip-version-check \
    -i https://pypi.tuna.tsinghua.edu.cn/simple || fail "依赖没装上（多半是网络/代理问题）"
fi

"$VPY" -c "import websocket" 2>/dev/null || fail "依赖装上了但 import 失败，试试删掉 .venv 重跑本脚本"

printf '\n\033[32m✓ 装好了。\033[0m\n'

if [ "$NO_LAUNCH" -eq 1 ]; then
  say "启动方式： .venv/bin/python serve.py --open"
  exit 0
fi

say "正在启动…浏览器会自动打开（关掉这个终端就会退出）"
if [ -n "$PORT" ]; then
  exec "$VPY" serve.py --open --port "$PORT"
else
  exec "$VPY" serve.py --open
fi
