#!/usr/bin/env bash
# 一键启动脚本：创建虚拟环境、安装依赖、启动 Web 服务
set -e
cd "$(dirname "$0")"

VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
  echo "🔧 创建虚拟环境 $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi

PY="$VENV_DIR/bin/python"

echo "🔍 当前 Python：$($PY --version)"
echo "🔍 Python 路径：$PY"

echo "📦 确保 pip 可用 ..."
"$PY" -m ensurepip --upgrade

echo "📦 安装依赖 ..."
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r requirements.txt

export FLASK_APP=app.py
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

echo "🚀 启动服务： http://${HOST}:${PORT}"
echo "   默认登录名 / 密码： admin / admin123 （首次登录后请在「系统配置」修改）"
"$PY" app.py
