#!/bin/bash
# ==========================================
# RAG 智能客服 启动脚本 - 端口 9012
# ==========================================
set -e

cd "$(dirname "$0")"

PORT=9012
QDRANT_URL="http://localhost:6333"

# ---- 1. 检查 .env ----
if [ ! -f .env ]; then
    echo "⚠️  未找到 .env，请先执行: cp .env.example .env 并填入 LLM_API_KEY"
fi

# ---- 2. 启动 Qdrant（如果没跑） ----
if ! curl -s "${QDRANT_URL}/" > /dev/null 2>&1; then
    echo "🐳 启动 Qdrant..."
    if command -v docker-compose > /dev/null 2>&1; then
        docker-compose up -d
    else
        docker compose up -d
    fi
    echo "⏳ 等待 Qdrant 就绪..."
    for i in $(seq 1 30); do
        if curl -s "${QDRANT_URL}/" > /dev/null 2>&1; then
            echo "✅ Qdrant 已就绪"
            break
        fi
        sleep 1
    done
else
    echo "✅ Qdrant 已在运行"
fi

# ---- 3. 启动 FastAPI ----
echo "🚀 启动 RAG 智能客服 (端口 ${PORT})..."
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --reload
