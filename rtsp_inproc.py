#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
在同一进程内启动 GStreamer RTSP Server（后台线程跑 GLib loop）。

为什么需要这个模块？
- `GstRtspServer` 需要 GLib main loop（`GLib.MainLoop().run()`），它会阻塞当前线程。
- 网关本身已经在用线程（UDP worker / watchdog / paho-mqtt loop），因此可以额外开一个线程专门跑 GLib loop。

注意：
- GI/GStreamer 依赖是可选的；缺失时会记录错误并保持“不可用”状态。
- 该实现尽量只在需要时启动对应的 video/audio RTSP server（避免某一路插件缺失影响另一路）。
"""

from __future__ import annotations

import threading
from typing import Callable, Optional


class InProcRtspServers:
    def __init__(
        self,
        *,
        video_rtsp_port: int,
        video_mount: str,
        video_udp_port: int,
        audio_rtsp_port: int,
        audio_mount: str,
        audio_udp_port: int,
        log: Callable[[str], None],
    ) -> None:
        self._video_rtsp_port = int(video_rtsp_port)
        self._video_mount = str(video_mount)
        self._video_udp_port = int(video_udp_port)

        self._audio_rtsp_port = int(audio_rtsp_port)
        self._audio_mount = str(audio_mount)
        self._audio_udp_port = int(audio_udp_port)

        self._log = log
        self._lock = threading.Lock()

        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

        self._stop_requested = False
        self._error: Optional[str] = None

        # Lazy GI/GStreamer objects; set by RTSP thread after import succeeds.
        self._GLib = None
        self._Gst = None
        self._GstRtspServer = None
        self._loop = None

        # state: "stopped" | "starting" | "started"
        self._video_state = "stopped"
        self._audio_state = "stopped"

        self._video_server = None
        self._audio_server = None

    def ensure_video(self) -> None:
        with self._lock:
            if self._video_state in {"starting", "started"}:
                return
            self._video_state = "starting"
            thread_alive = bool(self._thread and self._thread.is_alive())
            ready = self._ready.is_set()
            GLib = self._GLib
            error = self._error

        if not thread_alive:
            self._start_thread()
            return  # thread will create server once ready

        if error:
            self._log(f"[RTSP] in-proc unavailable: {error}")
            return

        if ready and GLib:
            try:
                GLib.idle_add(self._create_video_server)
            except Exception as e:
                self._log(f"[RTSP] in-proc schedule video failed: {e}")

    def ensure_audio(self) -> None:
        with self._lock:
            if self._audio_state in {"starting", "started"}:
                return
            self._audio_state = "starting"
            thread_alive = bool(self._thread and self._thread.is_alive())
            ready = self._ready.is_set()
            GLib = self._GLib
            error = self._error

        if not thread_alive:
            self._start_thread()
            return  # thread will create server once ready

        if error:
            self._log(f"[RTSP] in-proc unavailable: {error}")
            return

        if ready and GLib:
            try:
                GLib.idle_add(self._create_audio_server)
            except Exception as e:
                self._log(f"[RTSP] in-proc schedule audio failed: {e}")

    def stop(self, *, join_timeout_s: float = 1.5) -> None:
        with self._lock:
            self._stop_requested = True
            thread = self._thread
            ready = self._ready.is_set()
            GLib = self._GLib
            loop = self._loop

        if ready and GLib and loop:
            try:
                GLib.idle_add(loop.quit)
            except Exception:
                try:
                    loop.quit()
                except Exception:
                    pass

        if thread and thread.is_alive():
            thread.join(timeout=join_timeout_s)

        with self._lock:
            # allow restart
            if self._thread and not self._thread.is_alive():
                self._thread = None
                self._ready.clear()
                self._loop = None
                self._GLib = None
                self._Gst = None
                self._GstRtspServer = None

                self._video_state = "stopped"
                self._audio_state = "stopped"
                self._video_server = None
                self._audio_server = None
                self._stop_requested = False
                self._error = None

    # --- internal

    def _start_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._ready.clear()
            self._stop_requested = False
            self._error = None
            self._thread = threading.Thread(target=self._thread_main, name="RTSP-GLib", daemon=True)
            self._thread.start()

    def _thread_main(self) -> None:
        try:
            import gi

            gi.require_version("Gst", "1.0")
            gi.require_version("GstRtspServer", "1.0")
            from gi.repository import GLib, Gst, GstRtspServer  # type: ignore
        except Exception as e:
            with self._lock:
                self._error = str(e)
                self._video_state = "stopped"
                self._audio_state = "stopped"
            self._log(f"[RTSP] in-proc init failed: {e}")
            self._ready.set()
            return

        try:
            Gst.init(None)
        except Exception as e:
            with self._lock:
                self._error = str(e)
                self._video_state = "stopped"
                self._audio_state = "stopped"
            self._log(f"[RTSP] in-proc Gst.init failed: {e}")
            self._ready.set()
            return

        loop = GLib.MainLoop()
        with self._lock:
            self._GLib = GLib
            self._Gst = Gst
            self._GstRtspServer = GstRtspServer
            self._loop = loop

        self._ready.set()

        with self._lock:
            if self._stop_requested:
                return

        # Start any streams that were requested before GI was ready.
        # (Do not snapshot flags before `_ready.set()`; otherwise requests can be lost.)
        self._create_video_server()
        self._create_audio_server()

        with self._lock:
            if self._stop_requested:
                return

        try:
            loop.run()
        except Exception as e:
            self._log(f"[RTSP] in-proc GLib loop error: {e}")
        finally:
            self._log("[RTSP] in-proc GLib loop exited")

    def _create_video_server(self) -> bool:
        with self._lock:
            if self._video_state == "started":
                return False
            if self._video_state != "starting":
                return False
            GLib = self._GLib
            Gst = self._Gst
            GstRtspServer = self._GstRtspServer

        if not (GLib and Gst and GstRtspServer):
            return False

        try:
            payload = 98
            clock_rate = 90000
            caps = (
                "application/x-rtp,"
                "media=video,"
                "encoding-name=H264,"
                f"payload={payload},"
                f"clock-rate={clock_rate}"
            )
            launch_string = "( " + " ! ".join(
                [
                    f'udpsrc port={self._video_udp_port} caps="{caps}"',
                    "rtph264depay",
                    "h264parse",
                    f"rtph264pay config-interval=1 name=pay0 pt={payload}",
                ]
            ) + " )"

            log = self._log

            class Factory(GstRtspServer.RTSPMediaFactory):
                def __init__(self) -> None:
                    super().__init__()
                    self.launch_string = launch_string

                def do_create_element(self, url):
                    log(f"[RTSP] video request, launch={self.launch_string}")
                    return Gst.parse_launch(self.launch_string)

                def do_configure(self, rtsp_media):
                    rtsp_media.set_shared(True)

            server = GstRtspServer.RTSPServer()
            server.set_service(str(self._video_rtsp_port))
            mounts = server.get_mount_points()

            factory = Factory()
            factory.set_shared(True)
            try:
                factory.set_eos_shutdown(True)
            except Exception:
                pass

            mounts.add_factory(self._video_mount, factory)
            server.attach(None)

            self._log(
                f"[RTSP] in-proc video listening: rtsp://0.0.0.0:{self._video_rtsp_port}{self._video_mount} (UDP:{self._video_udp_port})"
            )
            with self._lock:
                self._video_server = server
                self._video_state = "started"
        except Exception as e:
            self._log(f"[RTSP] in-proc video start failed: {e}")
            with self._lock:
                self._video_state = "stopped"
        return False

    def _create_audio_server(self) -> bool:
        with self._lock:
            if self._audio_state == "started":
                return False
            if self._audio_state != "starting":
                return False
            GLib = self._GLib
            Gst = self._Gst
            GstRtspServer = self._GstRtspServer

        if not (GLib and Gst and GstRtspServer):
            return False

        try:
            payload = 8
            clock_rate = 8000
            channels = 1
            caps = (
                "application/x-rtp,"
                "media=(string)audio,"
                f"clock-rate=(int){clock_rate},"
                "encoding-name=(string)PCMA,"
                f"payload=(int){payload}"
            )
            launch_string = "( " + " ! ".join(
                [
                    f'udpsrc port={self._audio_udp_port} caps="{caps}"',
                    "rtppcmadepay",
                    f"audio/x-alaw,rate={clock_rate},channels={channels}",
                    f"rtppcmapay name=pay0 pt={payload}",
                ]
            ) + " )"

            log = self._log

            class Factory(GstRtspServer.RTSPMediaFactory):
                def __init__(self) -> None:
                    super().__init__()
                    self.launch_string = launch_string

                def do_create_element(self, url):
                    log(f"[RTSP] audio request, launch={self.launch_string}")
                    return Gst.parse_launch(self.launch_string)

                def do_configure(self, rtsp_media):
                    rtsp_media.set_shared(True)

            server = GstRtspServer.RTSPServer()
            server.set_service(str(self._audio_rtsp_port))
            mounts = server.get_mount_points()

            factory = Factory()
            factory.set_shared(True)
            try:
                factory.set_eos_shutdown(True)
            except Exception:
                pass

            mounts.add_factory(self._audio_mount, factory)
            server.attach(None)

            self._log(
                f"[RTSP] in-proc audio listening: rtsp://0.0.0.0:{self._audio_rtsp_port}{self._audio_mount} (UDP:{self._audio_udp_port})"
            )
            with self._lock:
                self._audio_server = server
                self._audio_state = "started"
        except Exception as e:
            self._log(f"[RTSP] in-proc audio start failed: {e}")
            with self._lock:
                self._audio_state = "stopped"
        return False
