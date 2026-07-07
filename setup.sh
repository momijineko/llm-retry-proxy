#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "   llm-retry-proxy  一键启动 (Linux/Mac)"
echo "============================================"
echo ""

PY=""
for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        PY="$cmd"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "[错误] 未检测到 python3，请先安装 Python 3.10+"
    exit 1
fi

if [ ! -f ".venv/bin/python" ]; then
    echo "[1/3] 创建虚拟环境 .venv ..."
    "$PY" -m venv .venv
else
    echo "[1/3] 虚拟环境已存在"
fi

echo "[2/3] 安装依赖..."
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

if [ ! -f ".env" ]; then
    echo "[3/3] 首次运行，配置 .env"
    echo ""
    read -rp "上游地址 [https://maas-coding-api.cn-huabei-1.xf-yun.com/v2]: " upstream
    upstream="${upstream:-https://maas-coding-api.cn-huabei-1.xf-yun.com/v2}"
    read -rp "供应商标签，如 xfyun [xfyun]: " provider
    provider="${provider:-xfyun}"
    read -rp "监听端口 [8080]: " port
    port="${port:-8080}"
    read -rp "重试间隔秒数 [1.0]: " interval
    interval="${interval:-1.0}"
    read -rp "最大重试次数 (0=无限重试直到成功) [60]: " maxretries
    maxretries="${maxretries:-60}"

    cat > .env <<EOF
UPSTREAM_URL=$upstream

LISTEN_HOST=0.0.0.0
LISTEN_PORT=$port

RETRY_INTERVAL=$interval
RETRY_INTERVAL_429=5.0
MAX_RETRIES=$maxretries
RETRY_STATUS_CODES=503,502,504,529,429

TIMEOUT=300
CONNECT_TIMEOUT=10

PROVIDER=$provider
LOG_DIR=logs
LOG_RETENTION_DAYS=30

LOG_LEVEL=INFO
EOF
    echo ""
    echo "[OK] 已生成 .env。如需调整其他项 (如 RETRY_STATUS_CODES) 可直接编辑该文件。"
    echo ""
    read -rp "按回车继续启动..."
else
    echo "[3/3] 使用已有 .env"
fi

echo ""
echo "============================================"
echo "   启动转发服务 (Ctrl+C 停止)"
echo "============================================"
.venv/bin/python main.py
