#!/bin/bash
for name in MONGO_URI TARGET_GROUP_ID BOT_QQ; do
    if [ -z "${!name}" ]; then
        echo "❌ 缺少环境变量 $name，请在部署平台的环境变量里填写。"
        exit 1
    fi
done

export PORT=${PORT:-7860}
export WEBHOOK_TOKEN=$(python -c 'import secrets; print(secrets.token_hex(24))')

NAPCAT_CONFIG=/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config
mkdir -p "$NAPCAT_CONFIG"
cat <<JSON > "$NAPCAT_CONFIG/onebot11_${BOT_QQ}.json"
{"network":{"httpServers":[{"name":"flask","enable":true,"port":3000,"host":"127.0.0.1","enableCors":false,"enableWebsocket":false,"messagePostFormat":"array"}],"httpClients":[{"name":"webhook","enable":true,"url":"http://127.0.0.1:${PORT}/webhook?token=${WEBHOOK_TOKEN}","messagePostFormat":"array"}],"websocketServers":[],"websocketClients":[]}}
JSON

trap 'kill 0' TERM INT

echo "🚀 启动 QQ 机器人 ($BOT_QQ) 和网站 (端口 $PORT)..."
xvfb-run -a /root/Napcat/opt/QQ/qq --no-sandbox -q "$BOT_QQ" &
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 16 --timeout 120 &

wait -n
echo "❌ QQ 机器人或网站已退出，容器随之退出。"
kill 0
