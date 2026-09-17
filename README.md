# AC Gateway

门禁网关程序，实现 MQTT ↔ UDP 控制协议转换和 RTP → RTSP 流媒体转发。

## 目录

- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [架构说明](#架构说明)
- [MQTT 协议](#mqtt-协议)
- [配置说明](#配置说明)
- [依赖安装](#依赖安装)
- [故障排查](#故障排查)

## 功能特性

- **MQTT 指令转发** - APP 通过 MQTT 控制门禁（开锁/视频/音频等）
- **来电自动处理** - 自动应答来电邀请，发布事件，启动视频流
- **双向通话** - 支持接听、挂断，自动维持心跳
- **本地 I/O** - 来电铃声播放，GPIO 按键自动开锁

## 快速开始

### 1. 安装依赖

```bash
pip install paho-mqtt
# RTSP 功能需要 GStreamer（可选）
```

### 2. 配置环境变量

```bash
export MQTT_HOST="your-mqtt-broker"   # 必填，无默认值
export MQTT_PORT="1883"               # 可选，默认 1883
export DOOR_IP_1="192.168.1.10"
```

### 3. 运行网关

```bash
python3 gateway.py
```

> 板端常驻部署（LicheeRV Nano，含本地 MQTT broker 和开机自启）见 [`deploy/README.md`](deploy/README.md)。

### 4. 测试命令

```bash
# 订阅事件
mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" -t '<prefix>/+/event' -v

# 发送开锁命令
mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -t '<prefix>/1' -m 'unlock'
```

## 局域网最小控制（不依赖 MQTT）

如果你不打算使用 APP 或 MQTT，而是希望在局域网电脑上直接控制门禁，可以先用最小开锁脚本验证协议是否可用。

### 1. 先检查 IP 连通性

```bash
ping 192.168.1.10
```

### 2. 直接发送开锁报文

```bash
python3 unlock_test.py 192.168.1.10
```

脚本行为：
- 先执行一次 `ping`
- `ping` 成功后，直接向门禁 `14301/UDP` 发送空闲态开锁报文
- 不依赖 MQTT、APP 或额外服务

常用参数：

```bash
# 指定控制端口
python3 unlock_test.py 192.168.1.10 --port 14301

# 跳过 ping，直接发送
python3 unlock_test.py 192.168.1.10 --skip-ping

# 打印十六进制报文，便于抓包核对
python3 unlock_test.py 192.168.1.10 --print-hex
```

注意：
- 这个脚本发送的是“空闲态开锁”固定报文，适用于门禁当前不在通话中的场景。
- 如果后续你还要做“接听/挂断/通话中动态开锁”，应继续复用 `protocol.py` 中的会话报文构造逻辑。

## 架构说明

### 端口分配

| 协议 | 端口 | 说明 |
|------|------|------|
| UDP 控制 | 14301 | 门禁控制协议 |
| RTP 视频 | 31410 | 门禁 → 网关 |
| RTP 音频 | 31420 | 双向音频流 |
| RTSP 视频 | 8554 | 网关 → APP (`/video`) |
| RTSP 音频 | 8555 | 网关 → APP (`/audio`) |

### 工作流程

```
APP → MQTT → 网关 → UDP/14301 → 门禁
                ↓
            RTSP 服务器 ← RTP 流 ← 门禁
                ↓
            APP (播放视频/音频)
```

## MQTT 协议

### Topic 规范

默认前缀：`${MQTT_TOPIC_PREFIX}`（通过环境变量配置，无内置默认值）

- **命令** (APP → 网关): `<prefix>/<door_id>`
- **事件** (网关 → APP): `<prefix>/<door_id>/event`

`door_id` 默认为 `1~4`，对应 `DOOR_IP_1~4` 环境变量。

### 命令列表

命令支持纯文本或 JSON 格式：`unlock` 或 `{"cmd":"unlock"}`

#### 基础控制

| 命令 | 说明 | 备注 |
|------|------|------|
| `unlock` / `open` | 开锁 | 通话中使用动态开锁，空闲时使用固定报文 |
| `video` | 打开视频 | 空闲模式，自动启动 RTSP，并自动以 1Hz 发送 `hb`（直到 `exit`） |
| `audio` | 打开音频 | 空闲模式 |
| `exit` / `stop` / `close` | 退出空闲模式 | 停止自动心跳 |

#### 通话控制

| 命令 | 说明 | 备注 |
|------|------|------|
| `answer` / `pickup` | 接听来电 | 仅响铃时有效 |
| `hangup` / `bye` / `end` | 挂断通话 | 仅通话中有效 |

**注意**：通话进行中会忽略 `video/audio/exit` 命令，避免干扰门禁状态机。

> 为避免用户正在查看某路门禁空闲视频/音频时，另一门禁突然来电导致两路门禁同时向网关同一 RTP 端口推流（混流/混音），网关在建立来电会话前会自动向其他门禁发送 `exit` 关闭指令。

### 事件列表

所有事件为 JSON 格式，包含 `type` 和 `ts` 字段。

| 事件类型 | 触发时机 | 关键字段 |
|---------|---------|---------|
| `incoming_call` | 检测到来电 | `door_ip`, `session_id`, `video_rtsp` |
| `incoming_call_ignored` | 忙线拒接 | `reason` |
| `answered` | 接听成功 | `door_ip`, `session_id`, `audio_rtsp` |
| `ended` | 通话结束 | `door_ip`, `session_id`, `reason` |
| `auto_unlock` | 按键自动开锁 | `door_ip` |

> 为避免 APP 在来电初期尚未连接 MQTT（或掉线重连）导致漏掉通知，网关在响铃阶段会按 `MQTT_INCOMING_CALL_REPEAT_S` 间隔重复发布 `incoming_call` 事件（默认 `1s`；`<=0` 表示不重复发布）。

#### 事件示例

```json
{
  "type": "incoming_call",
  "ts": "2026-01-01 12:34:56",
  "door_ip": "192.168.1.10",
  "session_id": "6f2a1b3c",
  "video_rtsp": "rtsp://192.168.1.100:8554/video"
}
```

### 使用建议

- 命令消息使用 `retain=false`，避免重启后误触发
- 命令建议 `qos=1`，事件可用 `qos=0/1`
- 如果 APP 不在同一网段，需设置 `RTSP_HOST` 为可达地址

## 配置说明

### MQTT 配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `MQTT_HOST` | **无默认值（必填）** | MQTT 服务器地址 |
| `MQTT_PORT` | `1883` | MQTT 服务器端口 |
| `MQTT_TOPIC_PREFIX` | **无默认值（必填）** | MQTT 主题前缀 |
| `MQTT_EVENT_SUFFIX` | `event` | 事件主题后缀 |
| `MQTT_INCOMING_CALL_REPEAT_S` | `1` | `incoming_call` 重复发布间隔（秒）；`<=0` 禁用 |

### 门禁 IP 映射

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `DOOR_IP_1` | *(无默认值)* | 门禁 1 的 IP 地址 |
| `DOOR_IP_2` | *(无默认值)* | 门禁 2 的 IP 地址 |
| `DOOR_IP_3` | *(无默认值)* | 门禁 3 的 IP 地址 |
| `DOOR_IP_4` | *(无默认值)* | 门禁 4 的 IP 地址 |

### RTSP 配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `RTSP_HOST` | 自动探测 | RTSP 服务器地址（用于事件中的 URL） |
| `RTSP_VIDEO_PORT` | `8554` | 视频 RTSP 端口 |
| `RTSP_AUDIO_PORT` | `8555` | 音频 RTSP 端口 |
| `RTSP_VIDEO_MOUNT` | `/video` | 视频挂载路径 |
| `RTSP_AUDIO_MOUNT` | `/audio` | 音频挂载路径 |

### 本地 I/O 配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `BELL_WAV_PATH` | 自动查找 | 门铃音频文件路径 |
| `BELL_DEVICE` | `hw:0,0` | ALSA 音频设备 |
| `GPIO_READ_PIN` | `2` | GPIO 按键引脚 |
| `BUTTON_PRESSED_LEVEL` | `0` | 按键按下电平（0=低电平） |

### 超时配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `RINGING_TIMEOUT_S` | `45` | 响铃超时（秒） |
| `ACTIVE_TIMEOUT_S` | `20` | 通话空闲超时（秒） |

### 高级选项

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `UDP_PORT` | `14301` | UDP 控制端口 |

## 依赖安装

### 必需依赖

```bash
pip install paho-mqtt
```

### RTSP 功能（推荐）

需要安装 GStreamer 和相关 Python 绑定：

**Ubuntu/Debian:**

```bash
sudo apt-get install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-rtsp \
    python3-gi \
    gir1.2-gst-rtsp-server-1.0
```

**其他系统:**

参考 GStreamer 官方文档安装对应平台的包。

### 本地 I/O 功能（可选）

**门铃播放:**

需要 `aplay` 命令（通常已预装）：

```bash
# 测试音频播放
aplay 门铃.wav
```

**GPIO 按键:**

需要 `gpio` 命令（WiringPi）：

```bash
# 树莓派等设备
sudo apt-get install wiringpi
```

## 故障排查

### RTSP 无法播放

1. 检查 GStreamer 是否正确安装：
   ```bash
   gst-launch-1.0 --version
   ```

2. 检查端口是否被占用：
   ```bash
   netstat -tuln | grep 8554
   ```

3. 检查防火墙规则：
   ```bash
   sudo ufw allow 8554/tcp
   sudo ufw allow 8555/tcp
   ```

### MQTT 连接失败

1. 检查网络连通性：
   ```bash
   ping $MQTT_HOST
   telnet $MQTT_HOST $MQTT_PORT
   ```

2. 检查 MQTT 服务器日志

3. 验证认证信息（如果需要）

### 门铃不响

1. 检查 WAV 文件是否存在：
   ```bash
   ls -l 门铃.wav
   ```

2. 测试音频设备：
   ```bash
   aplay -l  # 列出所有设备
   aplay -D hw:0,0 门铃.wav  # 测试播放
   ```

3. 检查日志中的 `[BELL]` 相关信息

### GPIO 按键无响应

1. 检查 WiringPi 是否安装：
   ```bash
   gpio -v
   ```

2. 测试 GPIO 读取：
   ```bash
   gpio mode 2 in
   gpio read 2
   ```

3. 检查引脚配置和电平设置

### 查看日志

网关会输出详细的运行日志，包含时间戳和模块标识：

```
[2026-01-01 12:34:56] [UDP] listen 0.0.0.0:14301
[2026-01-01 12:34:56] [MQTT] connecting <MQTT_HOST>:<MQTT_PORT> ...
[2026-01-01 12:34:57] [MQTT] connected rc=0; subscribe <prefix>/+
[2026-01-01 12:35:10] [CALL] incoming from 192.168.1.10 (door_id=1)
```

日志模块标识：
- `[UDP]` - UDP 通信
- `[MQTT]` - MQTT 通信
- `[RTSP]` - RTSP 服务器
- `[CALL]` - 呼叫处理
- `[BELL]` - 门铃播放
- `[BUTTON]` - 按键监测
- `[HB]` - 视频保活
