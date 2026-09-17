"""Protocol regression tests that do not require board hardware."""

import struct
import unittest

from protocol import (
    CallInvite,
    CtrlPacket,
    build_answer_action,
    build_answer_heartbeat,
    build_call_video,
    build_connected_ack,
    build_hangup,
    build_idle_unlock,
    build_unlock,
    parse_call_invite,
    parse_ctrl_packet,
)


def make_invite() -> CallInvite:
    packet = CtrlPacket("172.168.4.2", 14301, 3, 0x17, 50, b"", b"")
    return CallInvite(
        door_ip="172.168.4.2",
        door_id_type=bytes.fromhex("40b0e41131"),
        user_id_type=bytes.fromhex("46b4e41132"),
        session_id=bytes.fromhex("01020304"),
        invite_msg_id=3,
        raw_packet=packet,
    )


class ProtocolTests(unittest.TestCase):
    def test_control_builders_have_expected_lengths(self) -> None:
        invite = make_invite()
        self.assertEqual(len(build_connected_ack(invite)), 88)
        self.assertEqual(len(build_call_video(msg_id=4, invite=invite)), 106)
        self.assertEqual(len(build_answer_action(msg_id=5, invite=invite)), 77)
        self.assertEqual(len(build_answer_heartbeat(msg_id=6, invite=invite)), 76)
        self.assertEqual(len(build_hangup(msg_id=7, invite=invite)), 72)
        self.assertEqual(len(build_unlock(msg_id=8, invite=invite)), 72)
        self.assertEqual(len(build_idle_unlock()), 72)

    def test_parse_new_call_invite(self) -> None:
        msg_id = 9
        session_id = bytes.fromhex("11223344")
        body = bytearray(50)
        body[0:4] = struct.pack("<I", msg_id)
        body[16:20] = bytes.fromhex("40b0e411")
        body[24:28] = session_id
        raw = (
            bytes.fromhex("08ff0108")
            + b"\x00" * 12
            + b"SG\x00\x00"
            + struct.pack("<I", msg_id)
            + struct.pack("<I", 0x17)
            + b"\x00\x00"
            + struct.pack("<H", len(body))
            + body
        )
        packet = parse_ctrl_packet(raw, src_ip="172.168.4.2", src_port=14301)
        self.assertIsNotNone(packet)
        invite = parse_call_invite(packet)  # type: ignore[arg-type]
        self.assertIsNotNone(invite)
        self.assertEqual(invite.session_id, session_id)  # type: ignore[union-attr]
        self.assertEqual(invite.door_id_type, bytes.fromhex("40b0e41131"))  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
