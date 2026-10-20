#!/usr/bin/env bash
# All Agent Config 一键启动
# 首次运行自动创建虚拟环境并安装依赖；缺 Python 时给出安装引导。
set -u
cd "$(dirname "$0")"

find_python() {
  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)' 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

PY="$(find_python || true)"
if [ -z "$PY" ]; then
  echo "未找到 Python 3.11 或更高版本。"
  if command -v brew >/dev/null 2>&1; then
    printf "是否用 Homebrew 安装 python@3.12？[Y/N] "
    read -r answer
    case "$answer" in
      [Yy]*) brew install python@3.12 && PY="$(find_python || true)" ;;
    esac
  fi
  if [ -z "$PY" ]; then
    echo "请先安装 Python 3.11+：https://www.python.org/downloads/"
    exit 1
  fi
fi

VPY=".venv/bin/python"
if [ -x "$VPY" ] && \
   ! "$VPY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)' 2>/dev/null; then
  echo "现有 .venv 不可用（可能被移动或对应 Python 已删除），正在重建…"
  mv .venv ".venv.broken-$(date +%s)"
fi

if [ ! -x "$VPY" ]; then
  echo "创建虚拟环境 .venv …"
  "$PY" -m venv .venv || { echo "创建虚拟环境失败"; exit 1; }
fi

if ! "$VPY" -c "import fastapi, uvicorn, yaml, cryptography, multipart" >/dev/null 2>&1; then
  echo "安装依赖（首次运行约 1 分钟）…"
  "$VPY" -m pip install -q --disable-pip-version-check -e . || {
    echo "依赖安装失败，请检查网络后重试："
    echo "  $VPY -m pip install -e ."
    exit 1
  }
fi

exec "$VPY" -m app.main "$@"
