#!/usr/bin/env bash
# All Agent Config 一键启动
# 首次运行自动创建虚拟环境并安装依赖，之后直接启动。
cd "$(dirname "$0")"

if [ ! -x .venv/bin/aac ]; then
  echo "首次运行，正在准备环境…"
  python3 -m venv .venv || { echo "需要 Python 3.11+，请先安装：https://www.python.org/"; exit 1; }
  ./.venv/bin/pip install -q -e . || { echo "依赖安装失败"; exit 1; }
fi

exec ./.venv/bin/aac
