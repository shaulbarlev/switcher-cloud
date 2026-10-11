"""Regression tests for the ONLY hardware-verified protocol path (On Wall / S216).

Run:  python -m pytest tests/  (no Home Assistant needed, devices.py is pure Python)
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
    assert off.on is False and off.power_raw == 0


def test_onwall_remaining_from_live_bytes():
    # sampled right after a 180 s run started: remaining u16@21 = 179, time on i32@25 = 1
    b = bytes.fromhex("0201000000000000006e0a00000000000000000000b300000001000000000000")
    st = devices.parse_onwall(b)
    assert st.on and st.remaining == 179 and st.secs_on == 1 and st.power_raw == 2670


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


def test_auto_shutdown_write_matches_capture():
    # captured 0x0E01 tail when the app set 2 h: count=1, id=0x0C14, len=4, 7200
    body = devices.body_auto_shutdown(1, 2, b"\x00" * 32, 7200)
    assert body[40:] == bytes.fromhex("0100140c0400201c0000")
    # anything under 3660 s is raised to 3660 (1 h 1 m) like the app; there is no "off"
    assert devices.body_auto_shutdown(1, 2, b"\x00" * 32, 600)[46:50] == struct.pack("<i", 3660)
    assert devices.body_auto_shutdown(1, 2, b"\x00" * 32, 0)[46:50] == struct.pack("<i", 3660)


def test_auto_shutdown_read_and_push_payload():
    body = devices.body_read_params(1, 2, b"\x00" * 32, [devices.PARAM_AUTO_SHUTDOWN])
    assert body[40:] == bytes.fromhex("0100140c")                       # captured 0x0E02 tail
    vals = devices.parse_params_payload(bytes.fromhex("0100140c0400c0120000"))  # captured push
    assert struct.unpack("<i", vals[devices.PARAM_AUTO_SHUTDOWN])[0] == 4800
    assert devices.parse_params_payload(bytes.fromhex("0100140c04")) == {}   # truncated


def test_onwall_time_on_and_remaining_live_bytes():
    # sampled live 2026-10-10 during a 180 s timed run, then after turning off
    head = bytes.fromhex("0201000000000000" + "00" + "430a0000" + "00000000" + "00000000")
    during = devices.parse_onwall(head + bytes.fromhex("b400000050010000000000"))
    assert (during.remaining, during.secs_on) == (180, 336)
    off = devices.parse_onwall(bytes.fromhex("0100000000000000" + "00" * 13) + bytes.fromhex("000000008d010000000000"))
    assert (off.on, off.remaining, off.secs_on) == (False, 0, 397)


def test_next_seed_rules():
    ns = devices.next_seed
    assert ns(0, None, False, None, False) == 0            # off -> 0
    assert ns(179, None, True, None, False) == 179          # first value
    assert ns(179, 150, True, 179, False) is None           # stale repeat, keep counting
    assert ns(100, 150, True, 179, False) == 100            # real drop -> take it
    assert ns(179, 0, True, 179, False) is None             # ran out, same stale number
    assert ns(600, 0, True, 179, False) == 600              # ran out, new value -> new run
    assert ns(179, 150, True, 179, True) == 179             # hint newer -> re-seed


def test_dispatch_routes_param_push_to_waiter_not_push_callback():
    c = api.SwitcherClient("a", "b")
    seen = []
    c._on_push = seen.append
    q = __import__("queue").Queue()
    c._pending[1234] = q
    body = struct.pack("<HHIBH", 0x0E02, 1234, 0, 0, 10) + bytes.fromhex("0100140c0400c0120000")
    c._dispatch({"cmd": 0x0103, "serial": 99, "body": body, "errcode": 0})
    assert q.get_nowait()["payload"] == bytes.fromhex("0100140c0400c0120000")
    assert seen == []                                       # never reaches the refresh path
    other = struct.pack("<HHIBH", 0x0201, 5, 0, 0, 3) + b"\x23\x00\x00"
    c._dispatch({"cmd": 0x0103, "serial": 98, "body": other, "errcode": 0})
    assert len(seen) == 1                                   # real device pushes still do


# ---- cross-checks against aioswitcher (field-tested LAN library) -------------------
# aioswitcher's LAN control body is DID|uid|pwd[32]|sub|len|args, the same as the
# cloud 0x0201 body, so its packet templates pin our sub-op tails byte for byte.
# The "37" that precedes each sub-op in api/packets.py is the last pwd byte.
# Caveat for the token (S11/S12) forms: aioswitcher's GENERAL_TOKEN_COMMAND
# always writes length 0600 and pads the arguments to 6 bytes, whatever the
# real argument count. Ours carry the app's lengths (02 0300 for a 3 byte stop,
# 01 0200 for a 2 byte set-position). Sub-op and leading argument bytes match
# aioswitcher; the length field and padding do not.

def test_runner_tails_match_aioswitcher_packets():
    pwd = b"\x00" * 32
    # RUNNER_SET_POSITION: "3701" + "0100" + pos      (Runner / Runner Mini)
    assert devices.body_curtain_position(1, 2, pwd, 0x32)[40:] == bytes.fromhex("01010032")
    # token set_position: "3701" + "0600" + "0{index}{pos}" + pad; ours is len 2, no pad (S11 line 3)
    assert devices.body_curtain_position(1, 2, pwd, 100, line_no=3)[40:] == bytes.fromhex("0102000364")
    # RUNNER_STOP_COMMAND: "370202000000"
    assert devices.body_curtain_action(1, 2, pwd, 0)[40:] == bytes.fromhex("0202000000")
    # token stop_shutter: "3702" + "0600" + "0{index}0000" + pad; ours is len 3, no pad (S12 line 2)
    assert devices.body_curtain_action(1, 2, pwd, 0, line_no=2)[40:] == bytes.fromhex("020300020000")
    # RUNNER_SET_CHILD_LOCK: "3707" + "0100" + 01
    assert devices.body_curtain_lock(1, 2, pwd, True)[40:] == bytes.fromhex("07010001")
    assert devices.body_curtain_lock(1, 2, pwd, False, line_no=1)[40:] == bytes.fromhex("0702000100")


def test_mix_light_tail_matches_aioswitcher_set_light():
    # SET_LIGHT_PRECOMMAND "370a" + "0600" + "0{index}{01|00}" + "00000000"
    body = devices.body_plug(1, 2, b"\x00" * 32, True, line_no=1)
    assert body[40:] == bytes.fromhex("0a0600010100000000")
    assert devices.body_plug(1, 2, b"\x00" * 32, False, line_no=2)[40:] == bytes.fromhex("0a0600020000000000")


def test_type1_plug_tails_match_aioswitcher_packets():
    # SEND_CONTROL_PACKET: "00" + "01" + "0600" + "0{cmd}" + "00" + timer u32
    body = devices.body_plug_single(1, 2, "p", True, isdelay=15 * 60)
    assert body[40:] == bytes.fromhex("010600") + b"\x01\x00" + struct.pack("<I", 900)
    # SET_AUTO_OFF_SET_PACKET: "00" + "04" + "0400" + seconds u32
    body = devices.body_plug_auto_shutdown(1, 2, "p", 7200)
    assert len(body) == 47 and body[40:] == bytes.fromhex("040400") + struct.pack("<I", 7200)


def test_single_plug_workstatus_offsets_match_aioswitcher_state_parser():
    # StateMessageParser: state @0, power W u16 @2, time_left @14, time_on @18, auto_shutdown @22
    b = struct.pack("<HI", 1, 2300) + struct.pack("<Q", 0) + struct.pack("<III", 1234, 60, 3600) + b"\x00\x01"
    st = devices.parse_plug(b)
    assert st.on and st.power_raw == 2300 and st.remaining == 1234
    assert st.secs_on == 60 and st.autoclosing == 3600 and st.isdelay == 1


def test_heater_33b_matches_aioswitcher_heater_parser():
    # get_heater_*: state @1, power @9, auto_shutdown @21, time_on @25, time_left @29
    b = bytes([7, 1] + [0] * 7) + struct.pack("<III", 1500, 0, 0) + struct.pack("<III", 7200, 300, 2500)
    st = devices.parse_bath_heater(b)
    assert st.on and st.power_raw == 1500 and st.autoclosing == 7200
    assert st.secs_on == 300 and st.remaining == 2500
    # 32 B variant: lastTime u16 @21 is the remaining time too
    b32 = bytes([7, 1] + [0] * 7) + struct.pack("<III", 1500, 0, 0) + struct.pack("<H", 99) \
        + struct.pack("<II", 7200, 300) + b"\x01"
    assert devices.parse_bath_heater(b32).remaining == 99


def test_curtain_blocks_match_aioswitcher_direction_encoding():
    # 65 B panel status: wifi + 4 x 16 B blocks; direction "0100" = up, "0001" = down
    blk = lambda pos, lock, up, down: bytes([pos, lock, up, down, 1, 0, 0, 0]) + b"\x00" * 8
    b = bytes([60]) + blk(100, 0, 0, 0) + blk(40, 1, 1, 0) + blk(0, 0, 0, 1) + blk(0, 0, 0, 0)
    assert len(b) == 65
    l2 = devices.parse_curtain(b, 2)
    assert l2.position == 40 and l2.locked and l2.moving_up and not l2.moving_down
    l3 = devices.parse_curtain(b, 3)
    assert l3.position == 0 and l3.moving_down and not l3.on


def test_breeze_workstatus_matches_aioswitcher_thermostat_parser():
    # wifi, temp u16 tenths, ACData[power, mode, target, fan<<4|swing], unit, type, codeID[8]
    b = bytes([50]) + struct.pack("<H", 235) + bytes([1, 4, 24, 0x31, 0, 0]) + b"ELEC7022" \
        + struct.pack("<III", 0, 0, 0) + b"\x00"
    assert len(b) == 30
    st = devices.parse_aircon(b)
    assert st.on and st.temperature == 23.5 and st.mode == 4 and st.target_temp == 24
    assert st.fan == 3 and st.swing is True and st.remote_id == "ELEC7022"


def test_product_table_and_boilers():
    assert devices.classify("030B")[0] is devices.Family.PLUG          # Touch speaks OgeSPlugModel
    assert devices.product_name("030b", devices.Family.PLUG) == "Touch"
    assert devices.is_water_heater("030F") and devices.is_water_heater("0321")
    assert not devices.is_water_heater("01A8") and not devices.is_water_heater("031F")
    assert devices.is_timed("031F") and devices.is_timed("0321") and not devices.is_timed("01A8")
    assert devices.TIMED_TYPES == devices.WATER_HEATER_TYPES | {"031F"}


def test_auto_shutdown_range_by_family():
    ok, F = devices.auto_shutdown_valid, devices.Family
    # PLUG (V2/Touch/V4/Mini): the 1 h .. 23:59 window aioswitcher enforces, inclusive
    assert ok(F.PLUG, 3600) and ok(F.PLUG, 86340) and ok(F.PLUG, 7200)
    assert not ok(F.PLUG, 0) and not ok(F.PLUG, 3599) and not ok(F.PLUG, 86341) and not ok(F.PLUG, 86400)
    # ON_WALL: 61 min .. 23:59, the app's range; no 0 (the app cannot turn auto shutdown off)
    assert ok(F.ON_WALL, 3660) and ok(F.ON_WALL, 7200) and ok(F.ON_WALL, 86340)
    assert not ok(F.ON_WALL, 0) and not ok(F.ON_WALL, 3600) and not ok(F.ON_WALL, 3659) and not ok(F.ON_WALL, 86400)
    assert devices.classify("0F02")[0] is devices.Family.MIX           # Runner S12
    assert devices.product_name("0F06", devices.Family.MIX) == "Light SL03"
    assert devices.classify("0E01")[0] is devices.Family.AIRCON
    assert devices.product_name("0C02", devices.Family.CURTAIN) == "Runner Mini"
