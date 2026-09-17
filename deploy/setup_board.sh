#!/bin/sh
# 在板子上以 root 运行：安装 gateway.py + amqtt broker 所需依赖。
# 用法（板端）：
#   cd /opt/mili-gateway
#   sh setup_board.sh
#
# 说明：
# - paho-mqtt 是 gateway.py 的 MQTT 客户端库；
# - amqtt 是板载本地 MQTT broker（LicheeRV Nano 实测可用）。
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

echo ""
echo "== 安装 amqtt broker =="
# amqtt 0.12.1 是纯 Python wheel；跳过 build isolation 避免在无 gcc 的
# 板子上触发源码编译（LicheeRV Nano 无 gcc）。
pip3 install --no-cache-dir --no-build-isolation --no-deps \
    -i "$MIRROR" --trusted-host "$MIRROR_HOST" amqtt
# 运行依赖：全部为纯 Python wheel；psutil/pyyaml 官方镜像已预装旧版，
# 版本比 amqtt 要求略低但实测可用。
pip3 install --no-cache-dir -i "$MIRROR" --trusted-host "$MIRROR_HOST" \
    "websockets==15.0.1" typer dacite pwdlib typing-extensions transitions

echo ""
echo "== 验证依赖 =="
"$PY" - <<'PY'
try:
    import paho.mqtt
    from amqtt.broker import Broker
    print("paho-mqtt + amqtt OK")
except Exception as e:
    raise SystemExit("依赖不可用: %s" % e)
PY

echo ""
echo "== RTSP（可选）=="
if command -v apt-get >/dev/null 2>&1; then
    echo "如需 APP 看视频/音频，安装 GStreamer："
    echo "  apt-get install -y gstreamer1.0-tools gstreamer1.0-plugins-base \\"
    echo "      gstreamer1.0-plugins-good gstreamer1.0-rtsp python3-gi \\"
    echo "      gir1.2-gst-rtsp-server-1.0"
else
    echo "当前镜像未发现 apt-get。Tina/Buildroot 镜像通常没有 GStreamer，"
    echo "gateway.py 会在缺少 GStreamer 时自动禁用 RTSP（控制/开锁功能不受影响）。"
fi

echo ""
echo "依赖准备完成。下一步："
echo "  cp gateway.env.example gateway.env   # 修改 MQTT_HOST / DOOR_IP_*"
echo "  sh start_gateway.sh                  # 前台试运行 gateway"
echo "  或配置开机自启：cp S99mili /etc/init.d/S99mili && chmod +x /etc/init.d/S99mili"
