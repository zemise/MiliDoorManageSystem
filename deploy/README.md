# 板端部署手册（LicheeRV Nano WE，实测版）

目标：让 `gateway.py` 在 LicheeRV Nano 上**常驻运行**，通过
Home Assistant 的 Mosquitto Broker 作为门禁网关。

实测环境：LicheeRV Nano WE + Sipeed 官方 Buildroot 镜像（Linux 5.10.4 riscv64，自带 Python 3.11.6 和 paho-mqtt）。

---

## 0. 板子启动前提

LicheeRV Nano 从 **TF 卡**启动。没插卡/卡里没系统时，bootrom 会进入 USB 下载模式，Mac 上表现为 `CVITEK USB Com Port`（一打开就重枚举，不是控制台）。

1. 下载 Sipeed 官方镜像：https://github.com/sipeed/LicheeRV-Nano-Build/releases （选 `.img.xz`，配置 `sg2002_licheervnano_sd`）
2. 刷 TF 卡（balenaEtcher 或 `dd`）
3. 插卡上电，等 ~30 秒，板上 LED 闪烁、Mac 出现 **`sipeed` USB 网络设备**（`licheervnano`）即成功。

## 1. 进入板子

官方镜像会通过 USB 虚拟出网卡（RNDIS/NCM），Mac 侧自动拿到 IP：

```text
Mac  en10（licheervnano）: 10.139.158.100
板子 usb1               : 10.139.158.1
```

SSH 登录（默认 `root`/`root`）：

```bash
ssh root@10.139.158.1
```

> 串口控制台在 115200，但本板 USB 串口在 macOS 上打开即重枚举，**优先用 USB 网卡 SSH**。

## 2. 配置 WiFi 静态 IP（重启后仍生效）

板子 WiFi 只支持 2.4G。官方镜像通过 `/boot` 下的标志文件和 `/etc/network/interfaces` 管理网络。

```bash
# 1. WiFi 凭据（开机自动复制到 /etc/wpa_supplicant.conf）
cat > /boot/wpa_supplicant.conf <<'EOF'
ctrl_interface=/var/run/wpa_supplicant
ap_scan=1

network={
  ssid="你的WiFi名"
  psk="密码"
  key_mgmt=WPA-PSK
}
EOF

# 2. 关闭 wlan0 的 DHCP（改静态）
touch /boot/wifi.nodhcp

# 3. 静态 IP（示例：<板子WiFiIP>）
cat >> /etc/network/interfaces <<'EOF'

auto wlan0
iface wlan0 inet static
    address <板子WiFiIP>
    netmask 255.255.255.0
    gateway <家庭网关IP>
EOF
```

## 3. 配置门禁网络 eth0：伪装成室内面板（关键）

**户外机只接受来自原室内面板 IP/MAC 的接听**，所以板子必须伪装成面板：

- 面板 IP：`<面板IP>`
- 面板 MAC：`<面板MAC>`

```bash
# 3.1 关闭 eth0 的 DHCP
touch /boot/eth.nodhcp

# 3.2 开机先把 eth0 MAC 改成面板 MAC
cat > /etc/init.d/S29ethmac <<'EOF'
#!/bin/sh
ip link set dev eth0 down 2>/dev/null
ip link set dev eth0 address <面板MAC> 2>/dev/null
EOF
chmod +x /etc/init.d/S29ethmac

# 3.3 eth0 静态 IP 用面板的 .49
cat >> /etc/network/interfaces <<'EOF'

auto eth0
iface eth0 inet static
    address <面板IP>
    netmask 255.255.255.0
EOF
```

验证：`ping <门禁IP>` 应通，`ip link show eth0` 的 MAC 应为 `<面板MAC>`。

> 若原面板还接在网络上，先把它断开，避免 IP/MAC 冲突。

## 4. 传输代码

Mac 上打包：

```bash
sh deploy/make_bundle.sh
# 生成 仓库同级目录/mili-gateway-bundle.tar.gz
```

传到板子（scp 或 U 盘），解压：

```bash
mkdir -p /opt/mili-gateway
cd /tmp
# 注意：busybox tar 不认 -z，需要先 gzip -dc
gzip -dc mili-gateway-bundle.tar.gz > m.tar
tar xf m.tar -C /opt/mili-gateway --strip-components=1
rm -f m.tar mili-gateway-bundle.tar.gz
```

## 5. 安装依赖

```bash
cd /opt/mili-gateway
sh setup_board.sh
```

脚本会安装：
- `paho-mqtt`（gateway.py 客户端库）

## 6. 配置并试运行

```bash
cd /opt/mili-gateway
cp gateway.env.example gateway.env
vi gateway.env
```

在 `gateway.env` 填写：

```dotenv
MQTT_HOST=<Home Assistant/Mosquitto IP>
MQTT_PORT=1883
MQTT_USERNAME=<荔枝派专用 MQTT 用户>
MQTT_PASSWORD=<密码>
MQTT_TOPIC_PREFIX=mili/door
RTSP_HOST=<板子 WiFi IP>
RTSP_BACKEND=light
DOOR_IP_1=<门禁 IP>
```

直接启动 gateway：

```bash
cd /opt/mili-gateway
sh start_gateway.sh
```

正常日志：

```text
[UDP] listen 0.0.0.0:14301 (tx also uses this source port)
[MQTT] connecting <Home Assistant IP>:1883 ...
[MQTT] connected rc=0; subscribe mili/door/+
```

## 7. 开机自启（BusyBox init，实测）

```bash
cp /opt/mili-gateway/S99mili /etc/init.d/S99mili
chmod +x /etc/init.d/S99mili
```

`S99mili` 会挂载 swap、准备网络后启动 `gateway.py`。日志在
`/var/log/mili-gateway.log`。仅在 `LOCAL_MQTT_BROKER=1` 时才会启动兼容用的板端 aMQTT。
新板如确实需要该兼容模式，先执行
`INSTALL_LOCAL_MQTT_BROKER=1 sh setup_board.sh`。

重启验证：

```bash
reboot
# 重启后 ssh root@10.139.158.1 检查：
ps | grep gateway
tail /var/log/mili-gateway.log   # 应看到 connected rc=0
```

## 8. 验证开锁

```bash
cd /opt/mili-gateway
python3 unlock_test.py <门禁IP> --skip-ping --print-hex
```

或走 MQTT：

```bash
python3 - <<'PY'
import paho.mqtt.client as m
c = m.Client()
c.username_pw_set("<MQTT_USERNAME>", "<MQTT_PASSWORD>")
c.connect("<Home Assistant IP>", 1883, 10)
c.publish("mili/door/1", "unlock")
c.loop(1)
c.disconnect()
PY
```

`gateway.py` 的空闲开锁报文已替换为**现场抓包并实测开门成功**的 payload。

## 9. 已知问题与注意事项

- **WiFi 跨频段互访**：板子只能连 2.4G；确保板子能访问 Mosquitto，且 HA 能访问板子的 `8554/tcp`。必要时关闭主路由/AP 的“AP 隔离/用户隔离”。
- **GPIO 按键未适配**：`local_io.py` 目前调 `gpio read`（WiringPi，树莓派专用），本板没有。要用物理按键自动开锁，需把 GPIO 读取改成 sysfs（`/sys/class/gpio/.../value`）。不影响 MQTT 控制。
- **门铃 `aplay`**：镜像里没有 ALSA 时 `BellPlayer` 会自动禁用。
- **RTSP**：默认 `RTSP_BACKEND=light` 是纯 Python 实现。只在镜像安装了 GI/GStreamer 时才改为 `gstreamer`。
- **内存小（128MB）**：`S99mili` 会自动挂 128MB swap（`/swapfile`），别在板子上再跑重服务。
