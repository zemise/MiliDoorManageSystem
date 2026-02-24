#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass
from typing import Optional


_PREAMBLE = bytes.fromhex("08ff0108")
_MAGIC = b"SG\x00\x00"

# 本文件集中管理协议原语：
# - 基于抓包知识解析 UDP/14301 控制帧
# - 构建网关使用的控制帧（空闲和通话中）
#
# 协议并未完全逆向工程；部分字段保持为从抓包/示例中提取的常量，
# 而 ID/端口/命令则是结构化组装的。
#
# 本文件使用的术语：
# - 外层数据包：0x08ff0108 ... 'SG\\0\\0' + msg_id/kind/body_len + body
# - kind：外层"消息类型"字段（观察到：0x17 表示呼叫邀请，0x33 表示控制应答）
# - body：负载，通常包含标记 `aa f0 <cmd>`


@dataclass(frozen=True)
class CtrlPacket:
    src_ip: str
    src_port: int
    msg_id: int
    kind: int  # outer type (u32), e.g. 0x17 invite / 0x33 ack+actions
    body_len: int
    body: bytes
    raw: bytes


@dataclass(frozen=True)
class CallInvite:
    door_ip: str
    door_id_type: bytes  # 5 bytes, endswith 0x31 (seen in capture)
    user_id_type: bytes  # 5 bytes, endswith 0x32 (seen in capture)
    session_id: bytes  # 4 bytes
    invite_msg_id: int  # outer msg_id (u32 LE), equals body[0:4]
    raw_packet: CtrlPacket


def _u32_le(b: bytes) -> int:
    return struct.unpack("<I", b)[0]


def _u16_le(b: bytes) -> int:
    return struct.unpack("<H", b)[0]


def parse_ctrl_packet(data: bytes, *, src_ip: str, src_port: int) -> Optional[CtrlPacket]:
    """
    将 UDP/14301 负载解析为 CtrlPacket。

    布局（数值字段为小端序，偏移量以字节为单位）：
      0x00: 08ff0108
      0x04: 12 字节保留
      0x10: 'SG\\0\\0'
      0x14: msg_id (u32)
      0x18: kind   (u32)
      0x1C: reserved (u16)
      0x1E: body_len (u16)
      0x20: body[body_len]
    """
    if len(data) < 0x20:
        return None
    if data[0:4] != _PREAMBLE:
        return None
    if data[0x10:0x14] != _MAGIC:
        return None

    msg_id = _u32_le(data[0x14:0x18])
    kind = _u32_le(data[0x18:0x1C])
    body_len = _u16_le(data[0x1E:0x20])

    end = 0x20 + body_len
    if end > len(data):
        return None
    body = data[0x20:end]

    return CtrlPacket(
        src_ip=src_ip,
        src_port=src_port,
        msg_id=msg_id,
        kind=kind,
        body_len=body_len,
        body=body,
        raw=data,
    )


def extract_cmd(body: bytes) -> Optional[int]:
    """
    使用标记 `aa f0 <cmd>` 从 body 中提取命令字节。
    当标记不存在时返回 None。

    这是一个用于调试工具的小辅助函数；网关本身主要匹配完整消息
    （邀请 vs 非邀请）。
    """
    idx = body.find(b"\xaa\xf0")
    if idx == -1 or idx + 2 >= len(body):
        return None
    return body[idx + 2]


def _extract_id_types_from_body(body: bytes) -> Optional[tuple[bytes, bytes]]:
    """
    尽力提取抓包中看到的 5 字节 id_type 字段。

    在呼叫邀请样本中：
      body[0x10:0x15] = <id><type>
      body[0x1C:0x21] = <id><type>
    """
    if len(body) < 0x21:
        return None
    a = body[0x10:0x15]
    b = body[0x1C:0x21]
    if len(a) != 5 or len(b) != 5:
        return None
    return a, b


def parse_call_invite(pkt: CtrlPacket) -> Optional[CallInvite]:
    """
    从 CtrlPacket 解析"来电"邀请。

    基于提供的抓包的启发式规则：
      - kind == 0x17
      - body_len == 203 (0xCB)
      - body 包含 `aa f0 b0`
      - session_id 位于 `aa f0 b0 0001a300` 段之后的 4 字节
    """
    if pkt.kind != 0x17 or pkt.body_len != 203:
        return None

    body = pkt.body
    if len(body) < 60:
        return None

    if _u32_le(body[0:4]) != pkt.msg_id:
        # 在抓包中，body 以重复的 msg_id 开始；要求匹配以减少误报
        return None

    marker = body.find(b"\xaa\xf0\xb0")
    if marker == -1:
        return None
    # 期望格式：`aa f0 b0 00 01 a3 00 <session_id:4>`
    sid_off = marker + 3 + 4
    if sid_off + 4 > len(body):
        return None
    session_id = body[sid_off : sid_off + 4]

    id_types = _extract_id_types_from_body(body)
    if not id_types:
        return None
    id1, id2 = id_types

    # 根据抓包中看到的尾部"类型字节"（0x31 vs 0x32）判断哪个是门禁/用户
    door_id_type = None
    user_id_type = None
    for candidate in (id1, id2):
        if candidate[-1] == 0x31:
            door_id_type = candidate
        elif candidate[-1] == 0x32:
            user_id_type = candidate

    if door_id_type is None or user_id_type is None:
        # 回退：仍然返回但保持原始顺序？最好拒绝；构建器依赖于交换
        return None

    return CallInvite(
        door_ip=pkt.src_ip,
        door_id_type=door_id_type,
        user_id_type=user_id_type,
        session_id=session_id,
        invite_msg_id=pkt.msg_id,
        raw_packet=pkt,
    )


def _build_outer_packet(*, msg_id: int, kind: int, body: bytes) -> bytes:
    # 所有观察到的 UDP 控制数据包通用的外层帧封装
    header = bytearray()
    header += _PREAMBLE
    header += b"\x00" * 12
    header += _MAGIC
    header += struct.pack("<I", msg_id)
    header += struct.pack("<I", kind)
    header += struct.pack("<H", 0)  # reserved
    header += struct.pack("<H", len(body))
    return bytes(header) + body


_ID_GAP = bytes.fromhex("00010000000000")  # 7 bytes between id_type fields in samples


# "已连接/端口协商"消息使用的尾部段（kind=0x33, body_len=74）
_CONNECTED_TAIL = bytes.fromhex(
    "00f0b000012200b40000000000"
    "bc7ab27a"  # 音频/视频端口：31420/31410（小端序，匹配 AUDIO_UDP_PORT/VIDEO_UDP_PORT）
    "010f050000a00f0000000000000000000100000000000000"
)


def build_connected_ack(invite: CallInvite) -> bytes:
    """
    为给定的呼叫邀请构建"已连接"响应。

    此消息在抓包中没有命令标记（`aa f0`）；通过 kind=0x33, body_len=0x4A 识别。

    body 中的大多数字节是从抓包中提取的常量，但以下内容从邀请中动态填充：
      - msg_id (u32)
      - door_id_type (5 字节)
      - user_id_type (4+1 字节)
    """
    msg_id = invite.invite_msg_id
    door_id_type = invite.door_id_type
    user_id = invite.user_id_type[:4]
    user_type = invite.user_id_type[4:5]  # single byte

    body = bytearray()
    body += struct.pack("<I", msg_id)
    body += bytes.fromhex("0101ff003200010000000000")  # 固定头部段（标志/保留；含义未知）
    body += door_id_type
    body += _ID_GAP
    body += user_id
    body += user_type
    body += _CONNECTED_TAIL

    if len(body) != 74:
        raise ValueError(f"connected body len mismatch: {len(body)} != 74")

    return _build_outer_packet(msg_id=msg_id, kind=0x33, body=bytes(body))


def build_answer_action(*, msg_id: int, invite: CallInvite) -> bytes:
    """
    构建 b1（接听动作）。

    body 中的关键标记是：`aa f0 b1 ... <session_id>`
    """
    door_id_type = invite.door_id_type
    user_id = invite.user_id_type[:4]
    user_type = invite.user_id_type[4:5]
    session_id = invite.session_id

    body = bytearray()
    body += struct.pack("<I", msg_id)
    body += bytes.fromhex("0101ff003200010000000000")  # 固定头部段（未知字段）
    body += door_id_type
    body += _ID_GAP
    body += user_id
    body += user_type
    body += bytes.fromhex("aaf0b100010500")
    body += session_id
    body += bytes.fromhex("04")

    return _build_outer_packet(msg_id=msg_id, kind=0x33, body=bytes(body))


def build_answer_heartbeat(*, msg_id: int, invite: CallInvite) -> bytes:
    """
    构建 b3（接听心跳 1Hz）。

    门禁设备期望在接听后每秒收到此数据包，否则可能会在其侧挂断通话。
    """
    door_id_type = invite.door_id_type
    user_id = invite.user_id_type[:4]
    user_type = invite.user_id_type[4:5]
    session_id = invite.session_id

    body = bytearray()
    body += struct.pack("<I", msg_id)
    body += bytes.fromhex("0101ff003200010000000000")  # 固定头部段（未知字段）
    body += door_id_type
    body += _ID_GAP
    body += user_id
    body += user_type
    body += bytes.fromhex("aaf0b300010400")
    body += session_id

    return _build_outer_packet(msg_id=msg_id, kind=0x33, body=bytes(body))


def build_hangup(*, msg_id: int, invite: CallInvite) -> bytes:
    """
    构建 b2（结束/挂断）。

    标记：`aa f0 b2 ... <session_id>`
    """
    door_id_type = invite.door_id_type
    user_id = invite.user_id_type[:4]
    user_type = invite.user_id_type[4:5]
    session_id = invite.session_id

    body = bytearray()
    body += struct.pack("<I", msg_id)
    body += bytes.fromhex("0101ff003200010000000000")  # 固定头部段（未知字段）
    body += door_id_type
    body += _ID_GAP
    body += user_id
    body += user_type
    body += bytes.fromhex("aaf0b200010400")
    body += session_id

    return _build_outer_packet(msg_id=msg_id, kind=0x33, body=bytes(body))


def build_unlock(*, msg_id: int, invite: CallInvite) -> bytes:
    """
    构建 a0（开锁），格式与通话流程中观察到的相同。
    """
    door_id_type = invite.door_id_type
    user_id = invite.user_id_type[:4]
    user_type = invite.user_id_type[4:5]

    body = bytearray()
    body += struct.pack("<I", msg_id)
    # 注意：标志与其他消息不同（ff00 → 0101），基于抓包
    body += bytes.fromhex("010101003200010000000000")
    body += door_id_type
    body += _ID_GAP
    body += user_id
    body += user_type
    body += bytes.fromhex("aaf0a000010000")

    return _build_outer_packet(msg_id=msg_id, kind=0x33, body=bytes(body))


def hexlify(b: bytes) -> str:
    return binascii.hexlify(b).decode()
