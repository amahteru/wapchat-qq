#!/bin/bash
export TERM=xterm-256color

REAL_PORT=${PORT:-7860}

export WEBHOOK_TOKEN=$(python -c 'import secrets; print(secrets.token_hex(24))')

echo "🚀 正在将 Webhook 动态绑定到端口: $REAL_PORT"

cat <<EOF > /tmp/onebot_config.json
{"network":{"httpServers":[{"name":"flask","enable":true,"port":3000,"host":"127.0.0.1","enableCors":false,"enableWebsocket":false,"messagePostFormat":"array"}],"httpClients":[{"name":"webhook","enable":true,"url":"http://127.0.0.1:${REAL_PORT}/webhook?token=${WEBHOOK_TOKEN}","messagePostFormat":"array"}],"websocketServers":[],"websocketClients":[]}}
EOF

for dir in "/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config" "/root/.config/QQ/NapCat/config" "/root/.config/QQ/napcat/config"; do
    mkdir -p "$dir" && cp /tmp/onebot_config.json "$dir/onebot11_${BOT_QQ}.json"
done
rm -f /tmp/onebot_config.json

echo "🚀 启动服务..."
napcat start ${BOT_QQ}
sleep 5
napcat log ${BOT_QQ} &
python -u app.py
