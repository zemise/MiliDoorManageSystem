#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AC 网关主程序（MQTT ↔ UDP 控制，RTP → RTSP 转发）。

主要职责：
  - 订阅 MQTT 命令（`unlock/video/audio/exit/answer/hangup`）
  - 向门禁设备发送相应的 UDP/14301 控制帧
  - 监听 UDP/14301 上的"来电"邀请并自动应答"已连接"
  - 一次管理一个活动呼叫会话（响铃 → 接听 → 结束）
  - 在同一进程内启动轻量级 RTSP 转发服务器（视频/音频）
  - 可选的本地 I/O：来电时播放门铃 WAV；GPIO 按钮自动开锁

代码刻意采用"脚本优先"风格：单个 `Gateway` 进程拥有套接字/线程，
并提供 `main()` 供直接执行。
"""

from __future__ import annotations

import binascii
import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt
except Exception as e:  # pragma: no cover
    raise SystemExit(f"Missing dependency: paho-mqtt ({e}). Install it first.")

# 独立运行的导入风格：
# 预期的运行方式是：
#   cd ac_gateway
#   python3 gateway.py
#
# 因此我们直接导入同级模块（非包相对导入）
from local_io import BellConfig, BellPlayer, ButtonConfig, ButtonMonitor
from protocol import (
    CallInvite,
    build_answer_action,
    build_answer_heartbeat,
    build_call_video,
    build_connected_ack,
    build_hangup,
    build_unlock,
    parse_call_invite,
    parse_ctrl_packet,
)
from rtsp_inproc import InProcRtspServers
from rtsp_light import LightRtspMount


UDP_PORT = 14301
VIDEO_UDP_PORT = 31410
AUDIO_UDP_PORT = 31420

DEFAULT_TOPIC_PREFIX = ""


# 固定控制消息（用于空闲模式）。
#
# 注意：
# - 这些是从抓包中整理的 UDP/14301 负载。
# - 当没有活动呼叫会话时使用（无 session_id/user_id）。
# - 在活动呼叫期间，网关优先使用 `protocol.py` 中的结构化构建器，
#   以便重用门禁协商的呼叫中 ID。
HEX_VIDEO = "08ff0108000000000000000000000000534700009b0500003300000000006a009b0500000101ff003200010000000000b43c900631000100000000000000000032aaf0a100014200b27a31010114050000401f00000000000000000001000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
HEX_HB = (
    "08ff0108000000000000000000000000534700008b0600003300000000002a008b0600000101ff003200010000000000"
    "b43c900631000100000000000000000032aaf0a400010200b27a"
)
HEX_AUDIO = (
    "08ff0108000000000000000000000000534700009e0900003300000000002a009e0900000101ff003200010000000000"
    "b43c900631000100000000000000000032aaf0a200010200bc7a"
)
HEX_EXIT = (
    "08ff010800000000000000000000000053470000da0700003300000000002800da0700000101ff003200010000000000"
    "b43c900631000100000000000000000032aaf0a300010000"
)
HEX_UNLOCK_IDLE = (
    "08ff0108000000000000000000000000534700001f0200002b000000000028001f020000010101003200010000000000"
    "46b4e41131000100000000000000000032aaf0a000010000"
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


def _detect_local_ip(target_ip: str) -> str:
    # 检测用于到达 `target_ip` 的本地接口的技巧：
    # 创建 UDP 套接字并"连接"到远程；不发送任何数据包
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 9))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass


def _default_bell_wav() -> str:
    """
    选择合理的默认门铃 WAV 路径。

    目标：保持 `ac_gateway/` 可作为独立文件夹运行，同时与现有仓库布局兼容，
    其中 `门铃.wav` 位于上一级目录。
    """
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, "门铃.wav"),
        os.path.join(base, "..", "门铃.wav"),
    ]
    for p in candidates:
        ap = os.path.abspath(p)
        if os.path.exists(ap):
            return ap
    # 即使缺失也默认使用"本地"路径（BellPlayer 会记录日志并禁用）
    return os.path.abspath(candidates[0])


@dataclass
class Config:
    """
    通过环境变量进行运行时配置。

    此网关通常作为单个脚本部署在小型设备上，
    因此大多数选项通过环境变量而非配置文件控制。
    完整列表请参见 `README.md`。
    """

    mqtt_host: str = os.environ["MQTT_HOST"]
    mqtt_port: int = int(os.getenv("MQTT_PORT", "1883"))
    topic_prefix: str = os.environ["MQTT_TOPIC_PREFIX"]

    # MQTT 主题：
    # - 命令：<prefix>/<id>  负载：unlock/video/audio/exit/answer/hangup
    # - 事件：<prefix>/<id>/event 负载：JSON
    event_suffix: str = os.getenv("MQTT_EVENT_SUFFIX", "event")
    mqtt_incoming_call_repeat_s: float = float(os.getenv("MQTT_INCOMING_CALL_REPEAT_S", "1"))
    # 测试用：检测到来电后自动接听（AUTO_ANSWER=1）
    auto_answer: bool = os.getenv("AUTO_ANSWER", "0") == "1"

    udp_port: int = int(os.getenv("UDP_PORT", str(UDP_PORT)))

    rtsp_video_port: int = int(os.getenv("RTSP_VIDEO_PORT", "8554"))
    rtsp_audio_port: int = int(os.getenv("RTSP_AUDIO_PORT", "8555"))
    rtsp_video_mount: str = os.getenv("RTSP_VIDEO_MOUNT", "/video")
    rtsp_audio_mount: str = os.getenv("RTSP_AUDIO_MOUNT", "/audio")

    ring_wav: str = os.getenv(
        "BELL_WAV_PATH",
        _default_bell_wav(),
    )
    ring_device: str = os.getenv("BELL_DEVICE", "hw:0,0")

    button_pin: int = int(os.getenv("GPIO_READ_PIN", "2"))
    button_pressed_level: int = int(os.getenv("BUTTON_PRESSED_LEVEL", "0"))

    # 呼叫行为
    ringing_timeout_s: float = float(os.getenv("RINGING_TIMEOUT_S", "45"))
    active_timeout_s: float = float(os.getenv("ACTIVE_TIMEOUT_S", "20"))

    # 门禁 IP 映射（与现有脚本相同的默认值）
    door_ip_by_id: dict[str, str] = field(default_factory=lambda: {
        "1": os.getenv("DOOR_IP_1", "172.168.4.2"),
        "2": os.getenv("DOOR_IP_2", "172.168.4.3"),
        "3": os.getenv("DOOR_IP_3", "172.168.5.150"),
        "4": os.getenv("DOOR_IP_4", "172.168.6.103"),
    })

    @property
    def cmd_subscribe_topic(self) -> str:
        # 仅单级通配符；避免接收我们自己的 `<id>/event` 发布
        return f"{self.topic_prefix}/+"

    def event_topic(self, door_id: str) -> str:
        return f"{self.topic_prefix}/{door_id}/{self.event_suffix}"


class RtspManager:
    def __init__(self, config: Config):
        self._cfg = config
        self._inproc = InProcRtspServers(
            video_rtsp_port=self._cfg.rtsp_video_port,
            video_mount=self._cfg.rtsp_video_mount,
            video_udp_port=VIDEO_UDP_PORT,
            audio_rtsp_port=self._cfg.rtsp_audio_port,
            audio_mount=self._cfg.rtsp_audio_mount,
            audio_udp_port=AUDIO_UDP_PORT,
            log=_log,
        )

    def ensure_video(self) -> None:
        self._inproc.ensure_video()

    def ensure_audio(self) -> None:
        self._inproc.ensure_audio()

    def stop(self) -> None:
        self._inproc.stop()


@dataclass
class CallSession:
    door_id: str
    door_ip: str
    invite: CallInvite
    state: str = "ringing"  # 响铃|接听

    created_ts: float = field(default_factory=time.monotonic)
    last_seen_ts: float = field(default_factory=time.monotonic)
    last_incoming_call_event_ts: float = 0.0

    replied_invite_msg_ids: set[int] = field(default_factory=set)
    tx_msg_id: int = 4  # b1/b3/a0/b2 序列的出站 msg_id

    heartbeat_stop: threading.Event = field(default_factory=threading.Event)
    heartbeat_thread: Optional[threading.Thread] = None


class Gateway:
    def __init__(self, config: Config):
        self._cfg = config
        self._rtsp = RtspManager(config)

        self._stop_event = threading.Event()
        self._udp_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None

        self._session_lock = threading.Lock()
        self._session: Optional[CallSession] = None
        # 空闲媒体状态（视频/音频）。用于避免多个门禁同时向同一 RTP 端口推流
        # （31410/31420），导致 APP 端“同时播放两路/花屏/混音”。
        #
        # - _idle_open_doors: 最近通过 MQTT 打开的空闲媒体 door_id（video/audio）
        # - _idle_video_hb: 空闲视频保活线程（按 door_id）
        self._idle_open_doors: set[str] = set()
        self._idle_video_hb: dict[str, tuple[threading.Event, threading.Thread]] = {}

        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp_sock.bind(("0.0.0.0", self._cfg.udp_port))
        self._udp_sock.settimeout(0.5)
        self._udp_send_lock = threading.Lock()

        # 本地 I/O 是可选的（门铃 + 按钮）。如果缺少依赖项
        # （无 `aplay`/`gpio`、缺少 WAV 等），辅助函数会记录日志并禁用
        self._bell = BellPlayer(BellConfig(wav_path=self._cfg.ring_wav, device=self._cfg.ring_device), log=_log)
        self._button = ButtonMonitor(
            ButtonConfig(pin=self._cfg.button_pin, pressed_level=self._cfg.button_pressed_level),
            on_pressed=self._on_button_pressed,
            enabled_predicate=self._button_enabled,
            log=_log,
        )

        self._mqtt = mqtt.Client()
        self._mqtt.on_connect = self._on_mqtt_connect
        self._mqtt.on_message = self._on_mqtt_message

        # 为来电映射 ip -> id
        self._id_by_ip = {ip: door_id for door_id, ip in self._cfg.door_ip_by_id.items()}
        # 门禁中心控制器（面板会定期向它注册；REGISTRAR_IP 配置）
        self._registrar_ip = os.getenv("REGISTRAR_IP", "")

        # 轻量 RTSP 服务（无 GStreamer 依赖）：客户端播放时自动触发门禁推流
        self._rtsp_light_video = LightRtspMount(
            port=self._cfg.rtsp_video_port,
            udp_port=VIDEO_UDP_PORT,
            payload_type=98,
            codec="H264",
            clock_rate=90000,
            media="video",
            log=_log,
            on_play=lambda: self._light_play("video"),
            on_teardown=lambda: self._light_teardown("video"),
        )
        self._rtsp_light_audio = LightRtspMount(
            port=self._cfg.rtsp_audio_port,
            udp_port=AUDIO_UDP_PORT,
            payload_type=8,
            codec="PCMA",
            clock_rate=8000,
            media="audio",
            log=_log,
            on_play=lambda: self._light_play("audio"),
            on_teardown=lambda: self._light_teardown("audio"),
        )

    # --- 生命周期

    def start(self) -> None:
        _log(f"[UDP] listen 0.0.0.0:{self._cfg.udp_port} (tx also uses this source port)")
        self._button.start()
        threading.Thread(target=self._registrar_worker, name="Registrar", daemon=True).start()

        # 两个后台线程：
        # - UDPWorker：解析门禁控制帧 + 呼叫邀请
        # - Watchdog：强制执行响铃/活动超时
        self._udp_thread = threading.Thread(target=self._udp_worker, name="UDPWorker", daemon=True)
        self._udp_thread.start()

        self._watchdog_thread = threading.Thread(target=self._watchdog_worker, name="Watchdog", daemon=True)
        self._watchdog_thread.start()

        _log(f"[MQTT] connecting {self._cfg.mqtt_host}:{self._cfg.mqtt_port} ...")
        self._mqtt.connect(self._cfg.mqtt_host, self._cfg.mqtt_port, keepalive=30)
        self._mqtt.loop_start()

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self._rtsp.stop()
        except Exception:
            pass
        try:
            self._rtsp_light_video.close()
        except Exception:
            pass
        try:
            self._rtsp_light_audio.close()
        except Exception:
            pass
        try:
            self._mqtt.loop_stop()
        except Exception:
            pass
        try:
            self._mqtt.disconnect()
        except Exception:
            pass
        try:
            self._udp_sock.close()
        except Exception:
            pass

    # --- MQTT

    def _on_mqtt_connect(self, client: mqtt.Client, _userdata, _flags, rc, *args) -> None:
        _log(f"[MQTT] connected rc={rc}; subscribe {self._cfg.cmd_subscribe_topic}")
        client.subscribe(self._cfg.cmd_subscribe_topic)

    def _publish_event(self, door_id: str, event_type: str, **fields: Any) -> None:
        payload = {
            "type": event_type,
            "ts": _now(),
            **fields,
        }
        topic = self._cfg.event_topic(door_id)
        try:
            self._mqtt.publish(topic, json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            _log(f"[MQTT] publish failed: {e}")

    def _on_mqtt_message(self, _client: mqtt.Client, _userdata, msg) -> None:
        topic = msg.topic or ""
        parts = topic.split("/")
        if len(parts) != 3:
            return
        # 主题：<prefix>/<id>
        door_id = parts[-1].strip()
        if not door_id:
            return

        raw = (msg.payload or b"").decode(errors="ignore").strip()
        if not raw:
            return

        cmd = raw
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict) and "cmd" in obj:
                    cmd = str(obj["cmd"])
            except Exception:
                cmd = raw

        self._handle_cmd(door_id, cmd.strip().lower())

    # --- UDP

    def _udp_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                data, (src_ip, src_port) = self._udp_sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as e:
                _log(f"[UDP] recv error: {e}")
                time.sleep(0.2)
                continue

            pkt = parse_ctrl_packet(data, src_ip=src_ip, src_port=src_port)
            if not pkt:
                continue

            invite = parse_call_invite(pkt)
            if invite:
                self._handle_call_invite(invite)
                continue

            # 来自当前门禁的任何控制数据包都会更新活跃状态
            with self._session_lock:
                if self._session and src_ip == self._session.door_ip:
                    self._session.last_seen_ts = time.monotonic()

    def _udp_send(self, dst_ip: str, payload: bytes) -> None:
        # 门禁设备可能期望源端口保持稳定（14301），
        # 因此使用与接收路径相同的绑定套接字发送
        with self._udp_send_lock:
            self._udp_sock.sendto(payload, (dst_ip, self._cfg.udp_port))

    def _registrar_worker(self) -> None:
        """向门禁中心控制器注册/保活（实测面板的 aa ff 61 + aa ff 54 报文）。

        启动时先发一次完整注册（若存在 register250.bin），之后每 60s 发保活。
        """
        import struct as _struct

        if not self._registrar_ip:
            return

        msg_id = 1
        reg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "register250.bin")
        if os.path.exists(reg_path):
            try:
                reg = bytearray(open(reg_path, "rb").read())
                reg[0x14:0x18] = _struct.pack("<I", msg_id)
                reg[0x20:0x24] = _struct.pack("<I", msg_id)
                self._udp_send(self._registrar_ip, bytes(reg))
                _log(f"[REG] sent full registration msg_id={msg_id}")
                msg_id += 1
            except Exception as e:
                _log(f"[REG] full registration failed: {e}")

        while not self._stop_event.is_set():
            try:
                body = _struct.pack("<I", msg_id) + bytes.fromhex(
                    "0101ff00320001000000000046b4e41120000100000000000000000032aaff5400010000"
                )
                pkt = (
                    bytes.fromhex("08ff0108")
                    + b"\x00" * 12
                    + b"SG\x00\x00"
                    + _struct.pack("<I", msg_id)
                    + _struct.pack("<I", 0x2B)
                    + _struct.pack("<H", 0)
                    + _struct.pack("<H", len(body))
                    + body
                )
                self._udp_send(self._registrar_ip, pkt)
                _log(f"[REG] sent registration msg_id={msg_id}")
            except Exception as e:
                _log(f"[REG] registration failed: {e}")
            msg_id += 1
            time.sleep(60.0)

    def _light_play(self, media: str) -> None:
        _log(f"[RTSP] client play {media}; triggering door stream")
        self._handle_cmd("1", media)

    def _light_teardown(self, media: str) -> None:
        _log(f"[RTSP] client teardown {media}; stopping door stream")
        self._handle_cmd("1", "exit")

    # --- 呼叫处理

    def _handle_call_invite(self, invite: CallInvite) -> None:
        door_ip = invite.door_ip
        door_id = self._id_by_ip.get(door_ip)
        if door_id is None:
            # 门禁网络上还有其他设备（192.168.21.21 / 192.168.16.11 等）也在
            # 广播同类邀请；只处理已配置 DOOR_IP_* 的门禁，避免误接。
            _log(f"[CALL] ignore invite from unconfigured ip {door_ip} (expected DOOR_IP_*)")
            return

        now = time.monotonic()
        publish_ignored = False
        publish_incoming = False
        start_new = False
        with self._session_lock:
            if self._session is None:
                start_new = True
            elif self._session.door_ip != door_ip:
                # 单呼叫策略：忙碌时忽略其他门禁
                publish_ignored = True
            else:
                # 同一门禁；刷新会话信息并避免对同一 msg_id 重复应答
                self._session.last_seen_ts = now
                if invite.invite_msg_id not in self._session.replied_invite_msg_ids:
                    self._session.replied_invite_msg_ids.add(invite.invite_msg_id)
                # 保持最新的 session_id/ids
                self._session.invite = invite
                if self._session.state == "ringing":
                    repeat_s = self._cfg.mqtt_incoming_call_repeat_s
                    if repeat_s > 0 and (now - self._session.last_incoming_call_event_ts) >= repeat_s:
                        self._session.last_incoming_call_event_ts = now
                        publish_incoming = True

        if publish_ignored:
            self._publish_event(door_id, "incoming_call_ignored", reason="busy_with_other_call")
            return

        if start_new:
            # 如果用户正在查看其他门禁的空闲视频/音频，来电门禁与之不同时需要
            # 自动发送关闭指令，避免两路门禁同时推流到同一 RTP 端口导致混流。
            self._auto_close_idle_media(keep_door_id=door_id, reason="incoming_call")
            with self._session_lock:
                self._session = CallSession(
                    door_id=door_id,
                    door_ip=door_ip,
                    invite=invite,
                    replied_invite_msg_ids={invite.invite_msg_id},
                )
                self._session.last_incoming_call_event_ts = now
            _log(f"[CALL] incoming from {door_ip} (door_id={door_id}) session_id={invite.session_id.hex()}")
            self._rtsp.ensure_video()
            self._bell.start()
            publish_incoming = True

        if publish_incoming:
            rtsp_host = os.getenv("RTSP_HOST", _detect_local_ip(door_ip))
            self._publish_event(
                door_id,
                "incoming_call",
                door_ip=door_ip,
                session_id=invite.session_id.hex(),
                video_rtsp=f"rtsp://{rtsp_host}:{self._cfg.rtsp_video_port}{self._cfg.rtsp_video_mount}",
            )

        # 对每个邀请 msg_id 回复"已连接"（仅在未忙于其他呼叫时）
        try:
            ack = build_connected_ack(invite)
            self._udp_send(door_ip, ack)
        except Exception as e:
            _log(f"[CALL] failed to send connected ack: {e}")

        # 测试用：AUTO_ANSWER=1 时，检测到来电后自动接听（面板真实应答格式）。
        # 实测面板在来电应答(88B)后约 1.2s 才发开视频(106B)，这里模拟该节奏。
        if self._cfg.auto_answer:
            with self._session_lock:
                should_answer = (
                    self._session is not None
                    and self._session.door_ip == door_ip
                    and self._session.state == "ringing"
                )
            if should_answer:
                threading.Thread(target=self._delayed_answer, name="AutoAnswer", daemon=True).start()

    def _delayed_answer(self) -> None:
        time.sleep(1.2)
        with self._session_lock:
            session = self._session
            if not session or session.state != "ringing":
                return
        self._answer_call()

    def _auto_close_idle_media(self, *, keep_door_id: str, reason: str) -> None:
        with self._session_lock:
            open_doors = set(self._idle_open_doors) | set(self._idle_video_hb.keys())
            to_close = sorted(d for d in open_doors if d != keep_door_id)
            for d in to_close:
                self._idle_open_doors.discard(d)

        if not to_close:
            return

        _log(f"[MEDIA] auto exit idle media: close={to_close} keep={keep_door_id} reason={reason}")
        for d in to_close:
            # 停止空闲视频心跳，避免 exit 后仍被 hb 维持在推流状态
            self._stop_idle_video_hb(d, join_timeout_s=0.0)

            door_ip = self._cfg.door_ip_by_id.get(d)
            if not door_ip:
                _log(f"[MEDIA] auto exit skipped (unknown door id): {d!r}")
                continue
            self._send_idle_hex(door_ip, HEX_EXIT, tag="exit(auto)")

    def _answer_call(self) -> None:
        with self._session_lock:
            session = self._session
            if not session or session.state != "ringing":
                return
            session.state = "answered"
            invite = session.invite
            door_id = session.door_id
            door_ip = session.door_ip

        _log(f"[CALL] answer door_id={door_id} door_ip={door_ip}")
        self._bell.stop("answered")
        self._rtsp.ensure_audio()

        # 实测节奏：开视频(106B) → 约 1.2s → 开音频 b1(77B) → b3 心跳
        self._send_call_video()
        time.sleep(1.2)
        self._send_answer_action()
        self._start_answer_heartbeat()

        rtsp_host = os.getenv("RTSP_HOST", _detect_local_ip(door_ip))
        self._publish_event(
            door_id,
            "answered",
            door_ip=door_ip,
            session_id=invite.session_id.hex(),
            audio_rtsp=f"rtsp://{rtsp_host}:{self._cfg.rtsp_audio_port}{self._cfg.rtsp_audio_mount}",
        )

    def _hangup_call(self, *, reason: str, send_b2: bool = True) -> None:
        with self._session_lock:
            session = self._session
            if not session:
                return
            door_id = session.door_id
            door_ip = session.door_ip
            invite = session.invite
            self._session = None

        _log(f"[CALL] hangup door_id={door_id} door_ip={door_ip} reason={reason}")
        self._bell.stop("hangup")

        # 停止心跳
        session.heartbeat_stop.set()
        if session.heartbeat_thread and session.heartbeat_thread.is_alive():
            session.heartbeat_thread.join(timeout=0.5)

        if send_b2:
            try:
                msg_id = session.tx_msg_id
                session.tx_msg_id += 1
                pkt = build_hangup(msg_id=msg_id, invite=invite)
                self._udp_send(door_ip, pkt)
            except Exception as e:
                _log(f"[CALL] failed to send hangup: {e}")

        self._publish_event(door_id, "ended", door_ip=door_ip, session_id=invite.session_id.hex(), reason=reason)

    def _send_answer_action(self) -> None:
        with self._session_lock:
            session = self._session
            if not session:
                return
            invite = session.invite
            door_ip = session.door_ip
            msg_id = session.tx_msg_id
            session.tx_msg_id += 1

        try:
            pkt = build_answer_action(msg_id=msg_id, invite=invite)
            self._udp_send(door_ip, pkt)
        except Exception as e:
            _log(f"[CALL] send answer action failed: {e}")

    def _send_call_video(self) -> None:
        with self._session_lock:
            session = self._session
            if not session:
                return
            invite = session.invite
            door_ip = session.door_ip
            msg_id = session.tx_msg_id
            session.tx_msg_id += 1

        try:
            pkt = build_call_video(msg_id=msg_id, invite=invite)
            self._udp_send(door_ip, pkt)
        except Exception as e:
            _log(f"[CALL] send call video failed: {e}")

    def _start_answer_heartbeat(self) -> None:
        with self._session_lock:
            session = self._session
            if not session:
                return
            if session.heartbeat_thread and session.heartbeat_thread.is_alive():
                return
            session.heartbeat_stop.clear()
            session.heartbeat_thread = threading.Thread(
                target=self._answer_heartbeat_worker,
                name="AnswerHeartbeat",
                daemon=True,
            )
            session.heartbeat_thread.start()

    def _answer_heartbeat_worker(self) -> None:
        while not self._stop_event.is_set():
            with self._session_lock:
                session = self._session
                if not session or session.state != "answered":
                    return
                if session.heartbeat_stop.is_set():
                    return
                invite = session.invite
                door_ip = session.door_ip
                msg_id = session.tx_msg_id
                session.tx_msg_id += 1
            try:
                pkt = build_answer_heartbeat(msg_id=msg_id, invite=invite)
                self._udp_send(door_ip, pkt)
            except Exception as e:
                _log(f"[CALL] send heartbeat failed: {e}")
            time.sleep(1.0)

    # --- 本地 I/O

    def _button_enabled(self) -> bool:
        with self._session_lock:
            return bool(self._session and self._session.state == "ringing")

    def _on_button_pressed(self) -> None:
        # 响铃期间自动开锁 + 挂断
        with self._session_lock:
            session = self._session
            if not session or session.state != "ringing":
                return
            door_id = session.door_id
            door_ip = session.door_ip
            invite = session.invite
            msg_id = session.tx_msg_id
            session.tx_msg_id += 1

        _log(f"[CALL] button pressed -> auto unlock + hangup door_id={door_id} door_ip={door_ip}")
        try:
            unlock = build_unlock(msg_id=msg_id, invite=invite)
            self._udp_send(door_ip, unlock)
            self._publish_event(door_id, "auto_unlock", door_ip=door_ip)
        except Exception as e:
            _log(f"[CALL] auto unlock failed: {e}")

        self._hangup_call(reason="button_auto_unlock", send_b2=True)

    # --- 命令处理

    def _handle_cmd(self, door_id: str, cmd: str) -> None:
        door_ip = self._cfg.door_ip_by_id.get(door_id)
        if not door_ip:
            _log(f"[MQTT] unknown door id: {door_id!r}")
            return

        with self._session_lock:
            active = self._session is not None
            active_door_id = self._session.door_id if self._session else None

        # 注意：在呼叫活动期间，我们刻意限制某些命令
        # 以避免混淆门禁状态机（video/audio/exit）
        if cmd in {"answer", "pickup"}:
            if not active or door_id != active_door_id:
                _log(f"[MQTT] answer ignored (no matching call) door_id={door_id}")
                return
            self._answer_call()
            return

        if cmd in {"hangup", "bye", "end"}:
            if not active or door_id != active_door_id:
                _log(f"[MQTT] hangup ignored (no matching call) door_id={door_id}")
                return
            self._hangup_call(reason="mqtt_hangup", send_b2=True)
            return

        if cmd in {"unlock", "open"}:
            # 在活动呼叫期间，优先使用动态开锁（使用当前会话 ID）
            with self._session_lock:
                session = self._session
                can_dynamic = bool(session and session.door_id == door_id)
                if can_dynamic:
                    invite = session.invite
                    msg_id = session.tx_msg_id
                    session.tx_msg_id += 1
            if can_dynamic:
                try:
                    pkt = build_unlock(msg_id=msg_id, invite=invite)
                    self._udp_send(door_ip, pkt)
                    _log(f"[UDP] unlock(dynamic) -> {door_ip}:{self._cfg.udp_port}")
                except Exception as e:
                    _log(f"[UDP] unlock(dynamic) failed: {e}; fallback to idle unlock")
                    self._send_idle_hex(door_ip, HEX_UNLOCK_IDLE, tag="unlock")
            else:
                self._send_idle_hex(door_ip, HEX_UNLOCK_IDLE, tag="unlock")
            return

        # 呼叫期间，不转发打开视频/音频命令
        if active and cmd in {"video", "audio", "exit"}:
            _log(f"[MQTT] cmd ignored during call: {cmd}")
            return

        if cmd == "video":
            self._rtsp.ensure_video()
            self._send_idle_hex(door_ip, HEX_VIDEO, tag="video")
            self._start_idle_video_hb(door_id, door_ip)
            with self._session_lock:
                self._idle_open_doors.add(door_id)
            return

        if cmd == "audio":
            self._rtsp.ensure_audio()
            self._send_idle_hex(door_ip, HEX_AUDIO, tag="audio")
            # 实测面板在音频模式下同样发 a4 心跳保活
            self._start_idle_video_hb(door_id, door_ip)
            with self._session_lock:
                self._idle_open_doors.add(door_id)
            return

        if cmd in {"exit", "stop", "close"}:
            self._send_idle_hex(door_ip, HEX_EXIT, tag="exit")
            self._stop_idle_video_hb(door_id)
            with self._session_lock:
                self._idle_open_doors.discard(door_id)
            return

        _log(f"[MQTT] unknown cmd: {cmd!r}")

    def _send_idle_hex(self, door_ip: str, hexstr: str, *, tag: str) -> None:
        try:
            payload = binascii.unhexlify(hexstr)
        except Exception as e:
            _log(f"[UDP] invalid hex for {tag}: {e}")
            return
        try:
            self._udp_send(door_ip, payload)
            _log(f"[UDP] sent {tag} -> {door_ip}:{self._cfg.udp_port} ({len(payload)} bytes)")
        except Exception as e:
            _log(f"[UDP] send {tag} failed: {e}")

    # --- 可选：空闲视频保活

    def _start_idle_video_hb(self, door_id: str, door_ip: str) -> None:
        stop: threading.Event
        thread: threading.Thread
        with self._session_lock:
            session = self._session
            if session and session.door_id == door_id:
                return
            # 使用专用的轻量级心跳线程，不绑定到呼叫会话
            if door_id in self._idle_video_hb:
                return
            stop = threading.Event()
            thread = threading.Thread(
                target=self._idle_video_hb_worker,
                args=(door_id, door_ip, stop),
                name=f"IdleVideoHB-{door_id}",
                daemon=True,
            )
            self._idle_video_hb[door_id] = (stop, thread)
        thread.start()
        _log(f"[HB] idle video heartbeat started for door_id={door_id}")

    def _stop_idle_video_hb(self, door_id: str, *, join_timeout_s: float = 0.5) -> None:
        with self._session_lock:
            item = self._idle_video_hb.pop(door_id, None)
        if not item:
            return
        stop, thread = item
        stop.set()
        if join_timeout_s > 0 and thread.is_alive():
            thread.join(timeout=join_timeout_s)
        _log(f"[HB] idle video heartbeat stopped for door_id={door_id}")

    def _idle_video_hb_worker(self, door_id: str, door_ip: str, stop: threading.Event) -> None:
        payload = binascii.unhexlify(HEX_HB)
        while not self._stop_event.is_set() and not stop.is_set():
            with self._session_lock:
                if self._session is not None:
                    # 呼叫期间不发送
                    time.sleep(0.5)
                    continue
            try:
                self._udp_send(door_ip, payload)
            except Exception:
                pass
            time.sleep(1.0)

    # --- 看门狗

    def _watchdog_worker(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(1.0)

            hangup_reason: Optional[str] = None
            publish_incoming = False
            door_ip = ""
            door_id = ""
            session_id = ""

            now = time.monotonic()
            with self._session_lock:
                session = self._session
                if not session:
                    continue
                age = now - session.created_ts
                idle = now - session.last_seen_ts
                state = session.state

                if state == "ringing" and age > self._cfg.ringing_timeout_s:
                    hangup_reason = "ringing_timeout"
                elif state == "answered" and idle > self._cfg.active_timeout_s:
                    hangup_reason = "active_timeout"
                elif state == "ringing":
                    repeat_s = self._cfg.mqtt_incoming_call_repeat_s
                    if repeat_s > 0 and (now - session.last_incoming_call_event_ts) >= repeat_s:
                        session.last_incoming_call_event_ts = now
                        publish_incoming = True
                        door_ip = session.door_ip
                        door_id = session.door_id
                        session_id = session.invite.session_id.hex()

            # 超时在锁外强制执行，以便挂断可以执行 I/O、
            # 发布 MQTT、停止门铃等，而不持有会话互斥锁
            if hangup_reason:
                self._hangup_call(reason=hangup_reason, send_b2=True)
                continue

            if publish_incoming:
                rtsp_host = os.getenv("RTSP_HOST", _detect_local_ip(door_ip))
                self._publish_event(
                    door_id,
                    "incoming_call",
                    door_ip=door_ip,
                    session_id=session_id,
                    video_rtsp=f"rtsp://{rtsp_host}:{self._cfg.rtsp_video_port}{self._cfg.rtsp_video_mount}",
                )


def main() -> None:
    cfg = Config()
    gw = Gateway(cfg)
    gw.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        _log("exit (KeyboardInterrupt)")
    finally:
        gw.stop()


if __name__ == "__main__":
    main()
