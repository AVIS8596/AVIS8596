#!/usr/bin/env bash
# 一键安装（Ubuntu 22.04 / 24.04）。在项目根目录执行：bash deploy/install.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(whoami)"
cd "$DIR"

echo "==> 安装系统依赖"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip sqlite3 chrony unzip
sudo systemctl enable --now chrony   # 校准服务器时间，时间不准会导致交易签名过期

PYV=$(python3 -c 'import sys;print(sys.version_info[:2] >= (3,10))')
if [ "$PYV" != "True" ]; then echo "需要 Python 3.10 以上"; exit 1; fi

echo "==> 创建虚拟环境并安装 Python 包"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

mkdir -p data logs
if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> 已生成 .env，请先编辑填写：nano .env"
fi
chmod 600 .env
[ -f gmgn_private.pem ] && chmod 600 gmgn_private.pem

echo "==> 安装 systemd 服务（开机自启、崩溃自动重启）"
sed -e "s#__USER__#${USER_NAME}#g" -e "s#__DIR__#${DIR}#g" deploy/memebot.service | sudo tee /etc/systemd/system/memebot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable memebot >/dev/null

echo "==> 运行单元测试"
.venv/bin/python -m pytest -q tests || echo "（测试未全部通过，请把输出发给开发者）"

cat <<EOF

安装完成。接下来：
  1. nano .env                          填写各个 key
  2. .venv/bin/python main.py check     检查所有 key 是否可用
  3. sudo systemctl start memebot       启动（默认是模拟盘）
  4. tail -f logs/memebot.log           看日志
EOF
