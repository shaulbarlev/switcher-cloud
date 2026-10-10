"""Regression tests for the ONLY hardware-verified protocol path (On Wall / S216).

Run:  python -m pytest tests/  (no Home Assistant needed — devices.py is pure Python)
"""
import importlib.util
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "switcher_cloud"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


devices = _load("devices")
api = _load("api")

# Layout verified against a packet captured from the official app controlling a
# real On Wall (2026-10-10). Fixture values below are synthetic; the assertion
# is on structure (offsets, sub-op, length, state byte, 48 B total).
FIXTURE_DID, FIXTURE_UID, FIXTURE_PWD = 1234567, 987654321, "secret"
# WorkStatus samples (device-identifying fields are not part of WorkStatus):
# ON sample: current raw 2627 (= 11.9 A), OFF sample: everything zero.
WS_ON = bytes.fromhex("020100000000000000430a00000000000000000000c01200002e000000000000")
WS_OFF = bytes.fromhex("010000000000000000000000000000000000000000000000005c000000000000")


def test_onwall_body_layout():
    pwd_block = api._b_c_encrypt(FIXTURE_PWD)
    body = devices.body_onwall(FIXTURE_DID, FIXTURE_UID, pwd_block, True)
    assert len(body) == 48
    assert struct.unpack_from("<I", body, 0)[0] == FIXTURE_DID
    assert struct.unpack_from("<I", body, 4)[0] == FIXTURE_UID
    assert body[8:40] == pwd_block and len(pwd_block) == 32
    assert body[40] == 0x23 and struct.unpack_from("<H", body, 41)[0] == 5
    assert body[43] == 1 and struct.unpack_from("<I", body, 44)[0] == 0


def test_onwall_off_byte():
    body = devices.body_onwall(1, 2, api._b_c_encrypt("x"), False)
    assert body[40] == 0x23 and struct.unpack_from("<H", body, 41)[0] == 5
    assert body[43] == 0


def test_onwall_parse_on_off():
    on = devices.parse_onwall(WS_ON)
    assert on.on is True and on.power_raw == 2627
    off = devices.parse_onwall(WS_OFF)
    assert off.on is False and off.power_raw == 0 and off.secs_on == 0


def test_classify():
    assert devices.classify("0321")[0] is devices.Family.ON_WALL
    assert devices.classify("031F")[0] is devices.Family.BATH_HEATER
    assert devices.classify("0C01")[0] is devices.Family.CURTAIN
    assert devices.classify("zz")[0] is devices.Family.UNKNOWN


def test_other_bodies_have_app_sizes():
    pwd = api._b_c_encrypt("x")
    assert len(devices.body_bath_heater(1, 2, pwd, True)) == 48
    assert len(devices.body_plug(1, 2, pwd, True)) == 49
    assert len(devices.body_switch(1, 2, pwd, 1, True)) == 45
    assert len(devices.body_curtain_action(1, 2, pwd, 1)) == 45
    assert len(devices.body_curtain_position(1, 2, pwd, 50)) == 44
    assert len(devices.body_light_toggle(1, 2, pwd, False)) == 46


def test_frame_crc_roundtrip():
    pkt = api.build_packet(0x1343, b"", 1000, 0x11223344)
    p = api.parse_packet(pkt)
    assert p["cmd"] == 0x1343 and p["serial"] == 1000 and p["session"] == 0x11223344
    assert len(pkt) == 44


def test_light_rgbwt_roundtrip():
    # default constant {0,5,0,5,0,5,30,0,30,0} from OgeLightModel.java:60
    st = devices.parse_light(bytes([1, 0, 5, 0, 5, 0, 5, 30, 0, 30, 0, 0, 0, 0, 77]))
    assert st.on and st.rgb == (159, 159, 159) and st.wifi == 77
    body = devices.body_light_color(1, 7, b"\x00" * 32, True,
                                    devices.rgbwt_bytes(255, 128, 0, 100, 50), 3)
    assert len(body) == 55 and body[41:43] == b"\x0c\x00" and body[54] == 3
    rt = devices.parse_light(body[43:] + b"\x00\x00")
    assert rt.rgb == (255, 127, 0) and rt.white_pct == 100 and rt.temp_pct == 50


def test_boost_isdelay_seconds():
    body = devices.body_onwall(1, 2, b"\x00" * 32, True, isdelay=1800)
    assert body[43] == 1 and struct.unpack_from("<I", body, 44)[0] == 1800


def test_bath_heater_33b_layout():
    # OgeBathHeaterModel.java:200-209: delay@21, openTime@25, lastTime@29
    b = bytes([9, 1] + [0] * 7) + struct.pack("<III", 2200, 230000, 10000) \
        + struct.pack("<III", 600, 120, 99)
    assert len(b) == 33
    st = devices.parse_bath_heater(b)
    assert st.on and st.autoclosing == 600 and st.secs_on == 120 and st.lasttime == 99
    assert st.isdelay is None
    assert devices.parse_bath_heater(b + b"\x00").on is None   # len 34 rejected like Java


def test_plug_69b_four_lines():
    blocks = b"".join(bytes([i % 2]) + struct.pack("<IIIB", 1, 2, 3, 0) + b"\x00\x00"
                      for i in range(1, 5))
    b = bytes([50]) + blocks + b"\x00" * 4
    assert len(b) == 69
    assert devices.parse_plug(b, 4).on is False and devices.parse_plug(b, 1).on is True


def test_plug_single_body_and_curtain_position():
    body = devices.body_plug_single(0x01020304, 7, "abc", True, 0)
    assert len(body) == 49 and body[8:11] == b"abc" and body[40] == 0x01
    assert struct.unpack_from("<H", body, 41)[0] == 6 and struct.unpack_from("<H", body, 43)[0] == 1
    cur = devices.parse_curtain(bytes([80, 37, 0, 0, 1, 1, 0, 0, 0]))
    assert cur.position == 37 and cur.moving_down and cur.on


def test_classify_mix_and_no_socket():
    assert devices.classify("0F01")[0] is devices.Family.MIX
    assert not hasattr(devices.Family, "SOCKET")


def test_push_parse_offsets():
    body = struct.pack("<HHIBH", 0x0201, 46193, 1791644867, 0, 3) + b"\x23\x00\x00"
    p = api.parse_push(body)
    assert p["cmd"] == 0x0201 and p["status"] == 0 and p["payload"] == b"\x23\x00\x00"
