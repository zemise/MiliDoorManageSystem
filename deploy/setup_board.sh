#!/bin/sh
# 在板子上以 root 运行：安装 gateway.py 所需依赖。
# 用法（板端）：
#   cd /opt/mili-gateway
#   sh setup_board.sh
#
# 说明：
# - paho-mqtt 是 gateway.py 的 MQTT 客户端库；
# - 推荐连接 Home Assistant 主机上的 Mosquitto Broker。
set -e

MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
MIRROR_HOST="pypi.tuna.tsinghua.edu.cn"

echo "== 系统信息 =="
uname -a
if [ -f /etc/os-release ]; then
    . /etc/os-release 2>/dev/null || true
    echo "OS: ${PRETTY_NAME:-unknown}"
fi

# 优先 python3，个别旧镜像只有 python
PY=python3
if ! command -v python3 >/dev/null 2>&1; then
    PY=python
fi
echo "Python: $($PY -V 2>&1 || echo '未安装')"

echo ""
echo "== 安装 paho-mqtt =="
installed=0
if command -v pip3 >/dev/null 2>&1; then
    pip3 install --no-cache-dir -i "$MIRROR" --trusted-host "$MIRROR_HOST" paho-mqtt && installed=1
elif command -v pip >/dev/null 2>&1; then
    pip install --no-cache-dir -i "$MIRROR" --trusted-host "$MIRROR_HOST" paho-mqtt && installed=1
elif command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y python3-paho-mqtt && installed=1
elif command -v opkg >/dev/null 2>&1; then
    opkg update || true
    opkg install python3-paho-mqtt && installed=1
fi

if [ "$installed" -ne 1 ]; then
    echo ""
    echo "!! 未能自动安装 paho-mqtt，请根据镜像类型手动安装："
    echo "   Debian:  apt-get install -y python3-paho-mqtt"
    echo "   Tina:    opkg install python3-paho-mqtt  （或先 opkg update）"
    echo "   有 pip:  pip install paho-mqtt"
    exit 1
fi

if [ "${INSTALL_LOCAL_MQTT_BROKER:-0}" = "1" ]; then
    echo ""
    echo "== 安装兼容用 aMQTT broker =="
    "$PY" -m pip install --no-cache-dir --no-build-isolation --no-deps \
        -i "$MIRROR" --trusted-host "$MIRROR_HOST" amqtt
    "$PY" -m pip install --no-cache-dir -i "$MIRROR" --trusted-host "$MIRROR_HOST" \
        "websockets==15.0.1" typer dacite pwdlib typing-extensions transitions
fi

echo ""
echo "== 验证依赖 =="
"$PY" - <<'PY'
try:
    import paho.mqtt
    print("paho-mqtt OK")
except Exception as e:
    raise SystemExit("依赖不可用: %s" % e)
PY

echo ""
echo "== RTSP（可选）=="
if command -v apt-get >/dev/null 2>&1; then
    echo "默认的 RTSP_BACKEND=light 无需额外依赖。"
    echo "如改用 RTSP_BACKEND=gstreamer，安装："
    echo "  apt-get install -y gstreamer1.0-tools gstreamer1.0-plugins-base \\"
    echo "      gstreamer1.0-plugins-good gstreamer1.0-rtsp python3-gi \\"
    echo "      gir1.2-gst-rtsp-server-1.0"
else
    echo "当前镜像未发现 apt-get；使用默认的纯 Python 轻量 RTSP。"
fi

echo ""
echo "依赖准备完成。下一步："
echo "  cp gateway.env.example gateway.env   # 修改 MQTT_HOST / DOOR_IP_*"
echo "  sh start_gateway.sh                  # 前台试运行 gateway"
echo "  或配置开机自启：cp S99mili /etc/init.d/S99mili && chmod +x /etc/init.d/S99mili"
