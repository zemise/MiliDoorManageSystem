#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass
from typing import Optional


_PREAMBLE = bytes.fromhex("08ff0108")
_MAGIC = b"SG\x00\x00"

# 空闲态固定开锁报文。
# 这是从现有抓包中整理出的 a0 指令，适用于“非通话中”的直接开锁。
_IDLE_UNLOCK_HEX = (
    "08ff0108000000000000000000000000534700001f0200002b000000000028001f020000010101003200010000000000"
    "46b4e41131000100000000000000000032aaf0a000010000"
)

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
    从 CtrlPacket 解析"来电"邀请，兼容两种实测格式。

    旧格式（早期抓包）：
      - kind == 0x17, body_len == 203 (0xCB)
      - body 包含 `aa f0 b0`
      - session_id 位于 `aa f0 b0 0001a300` 段之后的 4 字节

    新格式（现场门禁实测广播邀请）：
      - kind == 0x17, body_len == 50 (0x32)
      - body: msg_id(4) + 0101ff00 + 3100010000000000
              + door_id(4) + 00000100 + session_id(4) + session_id(4)
              + 31aaff56 + 00010a00 + "230020103_"
    """
    if pkt.kind != 0x17 or pkt.body_len not in (203, 50):
        return None

    body = pkt.body
    if len(body) < 50:
        return None

    if _u32_le(body[0:4]) != pkt.msg_id:
        # 在抓包中，body 以重复的 msg_id 开始；要求匹配以减少误报
        return None

    if pkt.body_len == 50:
        # 新格式
        door_id = body[16:20]
        session_id = body[24:28]
        # 构造 5 字节 id_type 字段，供构建器使用（保持旧构建器接口不变）
        door_id_type = door_id + b"\x31"
        user_id_type = bytes.fromhex("46b4e411") + b"\x32"
        return CallInvite(
            door_ip=pkt.src_ip,
            door_id_type=door_id_type,
            user_id_type=user_id_type,
            session_id=session_id,
            invite_msg_id=pkt.msg_id,
            raw_packet=pkt,
        )

    # 旧格式
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


# 以下构建器基于 2026-09-17 实测面板 ↔ 户外机抓包。
# 关键差异（相对早期抓包）：kind 一律 0x2b；面板 ID=46b4e411；户外机 ID=40b0e411。
_ID_PREFIX = bytes.fromhex("0101ff003200010000000000")  # msg_id 之后的固定前缀
_USER_GAP = bytes.fromhex("3100010000000000")            # 面板 ID 之后


def _body_with_ids(msg_id: int, invite: CallInvite) -> bytearray:
    """构建呼叫中消息的公共前缀：msg_id + 固定前缀 + 面板ID + gap + 户外ID。"""
    body = bytearray()
    body += struct.pack("<I", msg_id)
    body += _ID_PREFIX
    body += invite.user_id_type[:4]
    body += _USER_GAP
    body += invite.door_id_type[:4]
    return body


def build_connected_ack(invite: CallInvite) -> bytes:
    """
    来电邀请的应答（88 字节，实测面板"接听"前的应答）。

    body(56): msg_id + 前缀 + 面板ID + gap + 户外ID + 3200ff56 + 00011000
              + 3200010000000000 + 面板ID + 3114a8c0
    """
    msg_id = invite.invite_msg_id
    user_id = invite.user_id_type[:4]
    door_id = invite.door_id_type[:4]

    body = bytearray()
    body += struct.pack("<I", msg_id)
    body += _ID_PREFIX
    body += user_id
    body += _USER_GAP
    body += door_id
    body += bytes.fromhex("3200ff5600011000")
    body += bytes.fromhex("3200010000000000")
    body += user_id
    body += bytes.fromhex("3114a8c0")

    if len(body) != 56:
        raise ValueError(f"connected body len mismatch: {len(body)} != 56")
    return _build_outer_packet(msg_id=msg_id, kind=0x2b, body=bytes(body))


def build_call_video(*, msg_id: int, invite: CallInvite) -> bytes:
    """
    通话建立后打开视频（106 字节，实测）。

    body(74): msg_id + 前缀 + 面板ID + gap + 户外ID + 3200f0b0 00012200 b400000000
              + 0000bc7a b27a0114 05000040 1f000000 00000000 00000100 00000000 0000
    """
    body = _body_with_ids(msg_id, invite)
    body += bytes.fromhex("3200f0b000012200b40000000000bc7ab27a0114050000401f0000000000000000000100000000000000")
    if len(body) != 74:
        raise ValueError(f"call video body len mismatch: {len(body)} != 74")
    return _build_outer_packet(msg_id=msg_id, kind=0x2b, body=bytes(body))


def build_answer_action(*, msg_id: int, invite: CallInvite) -> bytes:
    """
    打开音频/通话动作 b1（77 字节，实测）。

    body(45): msg_id + 前缀 + 面板ID + gap + 户外ID + 32aaf0b1 00010500 + session + 04
    """
    body = _body_with_ids(msg_id, invite)
    body += bytes.fromhex("32aaf0b100010500")
    body += invite.session_id
    body += bytes.fromhex("04")
    if len(body) != 45:
        raise ValueError(f"answer action body len mismatch: {len(body)} != 45")
    return _build_outer_packet(msg_id=msg_id, kind=0x2b, body=bytes(body))


def build_answer_heartbeat(*, msg_id: int, invite: CallInvite) -> bytes:
    """
    通话心跳 b3（76 字节，实测，1Hz）。

    body(44): msg_id + 前缀 + 面板ID + gap + 户外ID + 32aaf0b3 00010400 + session
    """
    body = _body_with_ids(msg_id, invite)
    body += bytes.fromhex("32aaf0b300010400")
    body += invite.session_id
    if len(body) != 44:
        raise ValueError(f"answer heartbeat body len mismatch: {len(body)} != 44")
    return _build_outer_packet(msg_id=msg_id, kind=0x2b, body=bytes(body))


def build_hangup(*, msg_id: int, invite: CallInvite) -> bytes:
    """
    结束/挂断（72 字节，实测面板结束命令 a3）。

    body(40): msg_id + 前缀 + 面板ID + gap + 00000000 + 32aaf0a3 00010000
    """
    body = bytearray()
    body += struct.pack("<I", msg_id)
    body += _ID_PREFIX
    body += invite.user_id_type[:4]
    body += _USER_GAP
    body += bytes.fromhex("0000000032aaf0a300010000")
    if len(body) != 40:
        raise ValueError(f"hangup body len mismatch: {len(body)} != 40")
    return _build_outer_packet(msg_id=msg_id, kind=0x2b, body=bytes(body))


def build_unlock(*, msg_id: int, invite: CallInvite) -> bytes:
    """
    构建 a0（开锁），与新格式一致（kind=0x2b，面板 ID + a0）。
    """
    body = bytearray()
    body += struct.pack("<I", msg_id)
    # 注意：标志与其他消息不同（ff00 → 0101），基于抓包
    body += bytes.fromhex("010101003200010000000000")
    body += invite.user_id_type[:4]
    body += bytes.fromhex("3100010000000000")
    body += bytes.fromhex("0000000032aaf0a000010000")
    if len(body) != 40:
        raise ValueError(f"unlock body len mismatch: {len(body)} != 40")
    return _build_outer_packet(msg_id=msg_id, kind=0x2b, body=bytes(body))


def build_idle_unlock() -> bytes:
    """
    构建空闲态开锁报文。

    当门禁当前不在通话会话中时，可以直接发送此固定 UDP 负载到门禁的
    控制端口（默认 14301）执行开锁。
    """
    return binascii.unhexlify(_IDLE_UNLOCK_HEX)


def hexlify(b: bytes) -> str:
    return binascii.hexlify(b).decode()
