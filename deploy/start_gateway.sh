#!/bin/sh
# 前台运行 gateway.py。
# 用法（板端）：
#   sh start_gateway.sh
# 也可以指定配置文件：
#   sh start_gateway.sh /path/to/gateway.env
#
# 常驻运行请使用 systemd 或 init.d 自启动（见 README），
# 本脚本也用于 procd 的 command 入口。

DIR=$(cd "$(dirname "$0")" && pwd)
ENV_FILE="${1:-$DIR/gateway.env}"

if [ ! -f "$ENV_FILE" ]; then
    echo "缺少配置文件: $ENV_FILE" >&2
    echo "请先执行: cp $DIR/gateway.env.example $DIR/gateway.env" >&2
    echo "然后填写 MQTT_HOST / MQTT_TOPIC_PREFIX / DOOR_IP_*" >&2
    exit 1
fi

# 将 .env 内容导出为环境变量后启动
set -a
. "$ENV_FILE"
set +a

exec python3 "$DIR/gateway.py"
