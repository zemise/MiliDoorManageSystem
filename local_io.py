#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

"""
本地 I/O 辅助模块（可选功能）。

本模块刻意保持轻量级依赖：
  - 门铃：使用 `aplay` 在 ALSA 设备上播放 WAV 文件
  - 按钮：使用 `gpio read <pin>` 轮询 GPIO 电平（WiringPi 风格的 CLI）

如果命令不存在（例如在笔记本电脑上运行），调用者仍然可以工作：
辅助函数会返回 None 或记录日志并禁用该功能。
"""

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


def _now_s() -> float:
    return time.monotonic()


def gpio_read_level(pin: int, *, timeout_s: float = 1.0) -> Optional[int]:
    """
    通过 `gpio read <pin>` 读取 GPIO 电平。

    我们使用子进程调用而不是 Python GPIO 库的原因：
      - 匹配现有部署环境（许多开发板自带 `gpio` 命令）
      - 避免硬绑定到特定 SoC / GPIO Python 包
      - 保持网关"复制一个文件夹即可运行"的友好性

    返回值:
      - 成功时返回 0/1
      - 命令失败或输出无法解析时返回 None
    """
    try:
        completed = subprocess.run(
            ["gpio", "read", str(pin)],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError:
        return None
    except Exception:
        return None

    if completed.returncode != 0:
        return None

    raw = (completed.stdout or "").strip()
    if not raw:
        return None

    token = raw.split()[0].strip().lower()
    if token in {"1", "high", "hi", "true", "on"}:
        return 1
    if token in {"0", "low", "lo", "false", "off"}:
        return 0
    try:
        return 1 if int(token) != 0 else 0
    except ValueError:
        return None


@dataclass(frozen=True)
class BellConfig:
    wav_path: str
    device: str = "hw:0,0"


class BellPlayer:
    """
    使用 `aplay` 的简单门铃播放器。

    - 启动：循环播放 `aplay` 直到调用 stop()
    - 停止：终止当前的 `aplay` 进程
    """

    def __init__(self, config: BellConfig, *, log: Callable[[str], None]):
        self._config = config
        self._log = log
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc_lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker, name="BellPlayer", daemon=True)
        self._thread.start()

    def stop(self, reason: str = "") -> None:
        self._stop_event.set()
        with self._proc_lock:
            proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass
        if reason:
            self._log(f"[BELL] stop ({reason})")

    def _worker(self) -> None:
        wav = self._config.wav_path
        if not os.path.exists(wav):
            self._log(f"[BELL] wav not found: {wav!r}; bell disabled")
            return

        self._log(f"[BELL] start: aplay -D {self._config.device} {wav}")
        devnull = subprocess.DEVNULL

        while not self._stop_event.is_set():
            try:
                proc = subprocess.Popen(
                    ["aplay", "-D", self._config.device, wav],
                    stdout=devnull,
                    stderr=devnull,
                )
            except FileNotFoundError:
                self._log("[BELL] aplay not found; bell disabled")
                return
            except Exception as e:
                self._log(f"[BELL] failed to start aplay: {e}")
                time.sleep(0.5)
                continue

            with self._proc_lock:
                self._proc = proc

            while proc.poll() is None and not self._stop_event.is_set():
                time.sleep(0.1)

            with self._proc_lock:
                if self._proc is proc:
                    self._proc = None

            # 如果自然结束，再次循环（保持响铃）


@dataclass(frozen=True)
class ButtonConfig:
    pin: int = 2
    pressed_level: int = 0  # active-low by default
    released_level: int = 1
    poll_s: float = 0.02
    debounce_s: float = 0.06


class ButtonMonitor:
    """
    基于轮询的 GPIO 按钮监控器。

    当检测到稳定的按下边沿时调用一次 `on_pressed()`。
    """

    def __init__(
        self,
        config: ButtonConfig,
        *,
        on_pressed: Callable[[], None],
        enabled_predicate: Callable[[], bool],
        log: Callable[[str], None],
    ):
        self._config = config
        self._on_pressed = on_pressed
        self._enabled_predicate = enabled_predicate
        self._log = log
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker, name="ButtonMonitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _worker(self) -> None:
        last_level: Optional[int] = None
        stable_level: Optional[int] = None
        stable_since = _now_s()
        fired_for_press = False

        while not self._stop_event.is_set():
            if not self._enabled_predicate():
                # 禁用时重置边沿检测
                fired_for_press = False
                last_level = None
                stable_level = None
                time.sleep(0.1)
                continue

            level = gpio_read_level(self._config.pin)
            if level is None:
                time.sleep(0.2)
                continue

            now = _now_s()
            if last_level is None or level != last_level:
                last_level = level
                stable_since = now

            if now - stable_since >= self._config.debounce_s:
                # 防抖逻辑：
                # - 等待电平稳定 debounce_s 时间
                # - 检测稳定的转换（按下/释放）
                # - 每次按下边沿触发一次 on_pressed()
                if stable_level != level:
                    stable_level = level
                    if stable_level == self._config.pressed_level:
                        fired_for_press = False
                    if stable_level == self._config.released_level:
                        fired_for_press = False

                if stable_level == self._config.pressed_level and not fired_for_press:
                    fired_for_press = True
                    try:
                        self._log("[GPIO] button pressed")
                        self._on_pressed()
                    except Exception as e:
                        self._log(f"[GPIO] on_pressed error: {e}")

            time.sleep(self._config.poll_s)
