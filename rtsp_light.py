#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量级 RTSP 服务器（纯 Python，无 GStreamer 依赖）。

把门禁通过 UDP 推来的 RTP（视频 31410 / 音频 31420）以
RTP-over-TCP（interleaved）方式转发给 RTSP 客户端。

设计目标：
  - Home Assistant / VLC / ffmpeg 可以直接拉流 rtsp://<板子>:8554/video
  - 单挂载点，支持多客户端同时观看（每个客户端一个转发线程）
  - 第一个客户端 PLAY 时回调 on_play（触发门禁推流），
    最后一个客户端断开时回调 on_teardown（停止门禁推流）
"""

from __future__ import annotations

import socket
import struct
import threading
from typing import Callable, Optional


class LightRtspMount:
    def __init__(
        self,
        *,
        port: int,
        udp_port: int,
        payload_type: int,
        codec: str,
        clock_rate: int,
        media: str,
        log: Callable[[str], None],
        on_play: Callable[[], None],
        on_teardown: Callable[[], None],
    ) -> None:
        self._port = int(port)
        self._udp_port = int(udp_port)
        self._payload_type = int(payload_type)
        self._codec = codec
        self._clock_rate = int(clock_rate)
        self._media = media
        self._log = log
        self._on_play = on_play
        self._on_teardown = on_teardown

        self._stop = threading.Event()
        self._clients_lock = threading.Lock()
        self._clients: dict[object, bool] = {}  # conn -> playing

        self._sdp = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 127.0.0.1\r\n"
            "s=door\r\n"
            "t=0 0\r\n"
            f"m={media} 0 RTP/AVP {self._payload_type}\r\n"
            f"a=rtpmap:{self._payload_type} {codec}/{self._clock_rate}\r\n"
            "a=control:*\r\n"
        )

        # UDP：接收门禁推来的 RTP
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.bind(("0.0.0.0", self._udp_port))
        self._udp.settimeout(0.5)

        # TCP：RTSP 监听
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind(("0.0.0.0", self._port))
        self._tcp.listen(4)
        self._tcp.settimeout(0.5)

        self._accept_thread = threading.Thread(
            target=self._accept_loop, name=f"RTSP-{media}-accept", daemon=True
        )
        self._accept_thread.start()
        self._log(f"[RTSP] light {media} listening: rtsp://0.0.0.0:{self._port} (UDP:{self._udp_port})")

    # --- lifecycle

    def close(self) -> None:
        self._stop.set()
        for conn in list(self._clients):
            try:
                conn.close()
            except OSError:
                pass
        try:
            self._tcp.close()
        except OSError:
            pass
        try:
            self._udp.close()
        except OSError:
            pass

    # --- internals

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._clients_lock:
                self._clients[conn] = False
            threading.Thread(
                target=self._handle_client, args=(conn, addr), name=f"RTSP-{self._media}-client", daemon=True
            ).start()

    def _handle_client(self, conn: socket.socket, addr) -> None:
        conn.settimeout(5.0)
        forward_stop = threading.Event()
        forward_thread: Optional[threading.Thread] = None
        playing = False
        buf = b""

        try:
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                buf += data
                while b"\r\n\r\n" in buf:
                    head, buf = buf.split(b"\r\n\r\n", 1)
                    response, should_play, should_teardown, close_after = self._handle_request(head)
                    if response:
                        try:
                            conn.sendall(response)
                        except OSError:
                            close_after = True
                    if should_play and not playing:
                        playing = True
                        with self._clients_lock:
                            was_any = any(self._clients.values())
                            self._clients[conn] = True
                        if not was_any:
                            try:
                                self._on_play()
                            except Exception as e:
                                self._log(f"[RTSP] on_play error: {e}")
                        forward_stop.clear()
                        forward_thread = threading.Thread(
                            target=self._forward_loop, args=(conn, forward_stop), daemon=True
                        )
                        forward_thread.start()
                    if should_teardown or close_after:
                        break
        finally:
            if forward_thread:
                forward_stop.set()
            with self._clients_lock:
                self._clients.pop(conn, None)
                any_left = any(self._clients.values())
            if playing and not any_left:
                try:
                    self._on_teardown()
                except Exception as e:
                    self._log(f"[RTSP] on_teardown error: {e}")
            try:
                conn.close()
            except OSError:
                pass

    def _handle_request(self, head: bytes):
        text = head.decode(errors="ignore")
        lines = text.split("\r\n")
        if not lines or not lines[0]:
            return None, False, False, True
        parts = lines[0].split(" ")
        if len(parts) < 2:
            return None, False, False, True
        method, path = parts[0].upper(), parts[1]

        cseq = ""
        transport = ""
        for line in lines[1:]:
            if line.lower().startswith("cseq:"):
                cseq = line.split(":", 1)[1].strip()
            elif line.lower().startswith("transport:"):
                transport = line

        base = f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n"

        if method == "OPTIONS":
            return (base + "Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN\r\n\r\n").encode(), False, False, False

        if method == "DESCRIBE":
            body = self._sdp.encode()
            resp = (
                base
                + "Content-Base: rtsp://0.0.0.0/\r\n"
                + "Content-Type: application/sdp\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n"
            ).encode() + body
            return resp, False, False, False

        if method == "SETUP":
            # RTP/AVP/TCP interleaved 0-1（或按客户端请求回显）
            if "interleaved" in transport.lower():
                ch = "0-1"
            else:
                ch = "0-1"
            resp = (
                base
                + f"Transport: RTP/AVP/TCP;unicast;interleaved={ch}\r\n"
                + "Session: 1\r\n\r\n"
            )
            return resp.encode(), False, False, False

        if method == "PLAY":
            return (base + "Session: 1\r\nRange: npt=0.000-\r\n\r\n").encode(), True, False, False

        if method == "TEARDOWN":
            return (base + "Session: 1\r\n\r\n").encode(), False, True, True

        return (f"RTSP/1.0 405 Method Not Allowed\r\nCSeq: {cseq}\r\n\r\n").encode(), False, False, False

    def _forward_loop(self, conn: socket.socket, stop: threading.Event) -> None:
        # RTP over TCP: $ + channel(1B) + length(2B BE) + RTP packet
        channel = 0
        while not stop.is_set() and not self._stop.is_set():
            try:
                data, _ = self._udp.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                continue
            frame = b"$" + bytes([channel]) + struct.pack(">H", len(data)) + data
            try:
                conn.sendall(frame)
            except OSError:
                break
