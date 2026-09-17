#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import binascii
import socket
import subprocess
import sys
from datetime import datetime

from protocol import build_idle_unlock, hexlify


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


def ping_host(ip: str, *, timeout_s: float) -> bool:
    """
    用系统 `ping` 做最小连通性检查。

    这里只验证目标 IP 在局域网内是否可达，不验证门禁业务端口状态。
    """
    try:
        completed = subprocess.run(
            ["ping", "-c", "1", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError:
        _log("未找到 `ping` 命令，跳过连通性检查")
        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception as e:
        _log(f"ping 执行失败: {e}")
        return False
    return completed.returncode == 0


def send_udp(ip: str, port: int, payload: bytes) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sent = sock.sendto(payload, (ip, port))
    finally:
        sock.close()
    if sent != len(payload):
        raise OSError(f"UDP payload not fully sent: {sent}/{len(payload)}")


def parse_payload_hex(value: str) -> bytes:
    cleaned = "".join(value.split()).replace(":", "")
    try:
        return binascii.unhexlify(cleaned)
    except (binascii.Error, ValueError) as e:
        raise argparse.ArgumentTypeError(f"invalid hex payload: {e}") from e


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="局域网内直接向门禁发送空闲态开锁报文，不依赖 MQTT。",
    )
    parser.add_argument("door_ip", help="门禁 IP，例如 192.168.1.10")
    parser.add_argument(
        "--port",
        type=int,
        default=14301,
        help="门禁 UDP 控制端口，默认 14301",
    )
    parser.add_argument(
        "--skip-ping",
        action="store_true",
        help="跳过 ping 检查，直接发 UDP 开锁报文",
    )
    parser.add_argument(
        "--ping-timeout",
        type=float,
        default=2.0,
        help="ping 超时时间（秒），默认 2",
    )
    parser.add_argument(
        "--print-hex",
        action="store_true",
        help="发送前打印十六进制报文内容",
    )
    parser.add_argument(
        "--payload-hex",
        type=parse_payload_hex,
        help="发送指定十六进制 UDP payload；不填则使用内置空闲态开锁报文",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    _log(f"目标门禁: {args.door_ip}:{args.port}")
    if not args.skip_ping:
        _log("先做 ping 连通性检查")
        if not ping_host(args.door_ip, timeout_s=args.ping_timeout):
            _log("ping 失败，门禁 IP 当前不可达；停止发送开锁报文")
            return 2
        _log("ping 成功，继续发送开锁报文")

    payload = args.payload_hex if args.payload_hex is not None else build_idle_unlock()
    if args.print_hex:
        _log(f"unlock hex: {hexlify(payload)}")

    try:
        send_udp(args.door_ip, args.port, payload)
    except Exception as e:
        _log(f"发送失败: {e}")
        return 1

    _log(f"已发送空闲态开锁报文 ({len(payload)} bytes)")
    _log("如果协议一致且设备允许当前状态开锁，门禁应立即执行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
