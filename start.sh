#!/bin/bash
export TERM=xterm-256color

for name in MONGO_URI TARGET_GROUP_ID BOT_QQ; do
    if [ -z "${!name}" ]; then
        echo "❌ 缺少环境变量 $name，请在部署平台的环境变量里填写。"
        exit 1
    fi
done

export PORT=${PORT:-7860}

export WEBHOOK_TOKEN=$(python -c 'import secrets; print(secrets.token_hex(24))')

echo "🚀 Webhook 绑定到端口: $PORT"

cat <<JSON > /tmp/onebot_config.json
{"network":{"httpServers":[{"name":"flask","enable":true,"port":3000,"host":"127.0.0.1","enableCors":false,"enableWebsocket":false,"messagePostFormat":"array"}],"httpClients":[{"name":"webhook","enable":true,"url":"http://127.0.0.1:${PORT}/webhook?token=${WEBHOOK_TOKEN}","messagePostFormat":"array"}],"websocketServers":[],"websocketClients":[]}}
JSON

NAPCAT_CONFIG=/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config
mkdir -p "$NAPCAT_CONFIG"
mv /tmp/onebot_config.json "$NAPCAT_CONFIG/onebot11_${BOT_QQ}.json"

echo "🚀 启动服务..."
napcat start ${BOT_QQ}
sleep 5
napcat log ${BOT_QQ} &
exec gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 16 --timeout 120
