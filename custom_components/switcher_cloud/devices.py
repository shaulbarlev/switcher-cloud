"""Device families: classification, command builders, WorkStatus parsers.

Everything here was read out of the decompiled Switcher Smart Home 7.53 app
(com.ogemray.data.model.Oge*Model and the ei/b.java builders) and then
cross-checked against TomerFi/aioswitcher, the field-tested LAN library behind
Home Assistant's switcher_kis. The LAN control body has the same layout as the
cloud 0x0201 body (DID | uid | pwd[32] | sub-op | len | args), and the LAN
state reply carries the same structure the cloud base64s as WorkStatus, so
aioswitcher's packet templates and parsers are strong evidence for the
sub-ops and offsets below. Only ON_WALL (S216) has been run against real
hardware through this integration.

Common cloud control body (businessCode 0x0201), from p5.r():
    DID u32 | uid u32 | native-AES(pwd)[32] | sub-op u8 | len u16 | args...
Only the pwd block is encrypted; the body goes on the wire plaintext.

DeviceType string in the 0x1343 JSON = "%02X%02X" % (mainType, subType)
(DataParser0x1343: parseInt(substring(0,2),16), parseInt(substring(2,4),16)).
Family dispatch mirrors OgeCommonDeviceModel.getNewInstance(mainType, subType).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum


class Family(str, Enum):
    ON_WALL = "on_wall"            # OgeSPlugS216Model  (3/33), verified on hardware
    BATH_HEATER = "bath_heater"    # OgeBathHeaterModel (3/31)
    PLUG = "plug"                  # OgeSPlugModel      (3/11,15,23; 1/161,167,168)
    SWITCH = "switch"              # OgeSwitchModel     (other 1/x, 3/x) multi-gang
    CURTAIN = "curtain"            # OgeCurtainModel    (12/x)
    LIGHT = "light"                # OgeLightModel      (8/x)
    MIX = "mix"                    # OgeMixDeviceModel  (15/x) multi-line panels: plug/light lines + runner lines
    AIRCON = "aircon"              # OgeAirConditionModel (14/x), not supported
    UNKNOWN = "unknown"


VERIFIED = {Family.ON_WALL}

# Product names per DeviceType, from aioswitcher DeviceType (device/__init__.py)
# plus the two models only the app knows. Anything else gets a family name.
PRODUCTS = {
    "0321": "On Wall (S216)",
    "031F": "Heater",
    "030B": "Touch",
    "030F": "Mini",
    "0317": "V4",
    "01A1": "V2 (qualcomm)",
    "01A7": "V2 (esp)",
    "01A8": "Power Plug",
    "0E01": "Breeze",
    "0C01": "Runner",
    "0C02": "Runner Mini",
    "0F01": "Runner S11",
    "0F02": "Runner S12",
    "0F04": "Light SL01",
    "0F07": "Light SL01 Mini",
    "0F05": "Light SL02",
    "0F08": "Light SL02 Mini",
    "0F06": "Light SL03",
}
# aioswitcher DeviceCategory.WATER_HEATER plus the app's two boiler models:
# these get water_heater, run timer and auto-shutdown entities. 01A8 (Power
# Plug) speaks the same OgeSPlugModel protocol but is a plain outlet.
BOILER_TYPES = {"0321", "031F", "030B", "030F", "0317", "01A1", "01A7"}


def product_name(device_type: str, family: Family) -> str:
    return PRODUCTS.get(device_type.upper(), MODEL_NAMES.get(family, "Unknown device"))


def is_boiler(device_type: str) -> bool:
    return device_type.upper() in BOILER_TYPES


def classify(device_type: str) -> tuple[Family, int, int]:
    """Return (family, mainType, subType) for a 0x1343 DeviceType string.
    Mirrors OgeCommonDeviceModel.getNewInstance. (The Java 1/273 "socket" branch
    is dead code: subType is a 2-hex-digit field, so 273 = 0x111 can never occur.)"""
    try:
        main, sub = int(device_type[0:2], 16), int(device_type[2:4], 16)
    except (ValueError, TypeError, IndexError):
        return Family.UNKNOWN, 0, 0
    if main == 8:
        return Family.LIGHT, main, sub
    if main == 3 and sub == 31:
        return Family.BATH_HEATER, main, sub
    if main == 3 and sub == 33:
        return Family.ON_WALL, main, sub
    if (main == 3 and sub in (11, 15, 23)) or (main == 1 and sub in (161, 167, 168)):
        return Family.PLUG, main, sub
    if main in (1, 3):
        return Family.SWITCH, main, sub
    if main == 12:
        return Family.CURTAIN, main, sub
    if main == 14:
        return Family.AIRCON, main, sub
    if main == 15:
        return Family.MIX, main, sub
    return Family.UNKNOWN, main, sub




# ---- body builders -------------------------------------------------------------

def _head(did: int, uid: int, pwd_block: bytes, sub: int, length: int) -> bytearray:
    b = bytearray()
    b += struct.pack("<I", did)
    b += struct.pack("<I", uid)
    b += pwd_block
    b += bytes([sub])
    b += struct.pack("<H", length)
    return b


def body_onwall(did, uid, pwd_block, on: bool, isdelay: int = 0) -> bytes:
    """ei/b.w(): sub 0x23 len 5: state u8, isdelay u32. (48 B) Verified on hardware.
    isdelay is a number of seconds for a timed run (app UI a1/e2.java:437-456,
    10 s steps, at most autoclosingtime). 0 means a plain on/off."""
    b = _head(did, uid, pwd_block, 0x23, 5)
    b += bytes([1 if on else 0]) + struct.pack("<I", isdelay)
    return bytes(b)


def body_bath_heater(did, uid, pwd_block, on: bool, delay: int = 0) -> bytes:
    """OgeBathHeaterModel.getControlCMD: sub 0x23 len 5: state u8, delayTime u32 (seconds). (48 B)"""
    b = _head(did, uid, pwd_block, 0x23, 5)
    b += bytes([1 if on else 0]) + struct.pack("<I", delay)
    return bytes(b)


def body_plug(did, uid, pwd_block, on: bool, line_no: int = 0, line_type: int = 0,
              isdelay: int = 0, autoclosing: int = 0) -> bytes:
    """OgeSPlugModel.getControlCMD cloud forms (OgeSPlugModel.java:111-143):
       lineNo != 0 : sub 0x0A len 6: lineNo u8,   state u8, isdelay u32      (49 B)
       lineType!=0 : sub 0x0A len 6: lineType u8, state u8, autoclosing u32  (49 B)
                     autoclosing has to echo the device's current value, otherwise
                     the command wipes its auto-off.
    For lineNo==0 && lineType==0 the app uses a different body (sub 0x01 with a
    plaintext pwd, AES-wrapped by key2_50, relation=DID). See body_plug_single."""
    b = _head(did, uid, pwd_block, 0x0A, 6)
    if line_no:
        b += bytes([line_no & 0xFF, 1 if on else 0]) + struct.pack("<I", isdelay)
    else:
        b += bytes([line_type & 0xFF, 1 if on else 0]) + struct.pack("<I", autoclosing)
    return bytes(b)


def body_plug_single(did, uid, pwd_plain: str, on: bool, isdelay: int = 0) -> bytes:
    """OgeSPlugModel.getControlCMD lineNo==0 && lineType==0 (OgeSPlugModel.java:145-173),
    identical in OgeSocketModel: 49 B plaintext body = DID u32, uid u32, pwd l(32)
    (not the native AES block), sub 0x01, len u16 6, state u16, isdelay u32.
    On the cloud path the whole body is then AES-ECB'd with key2_50 (= BODY_KEY)
    and sent with control=0x0100 and relation_id = DID LE u32 + 00 00.
    Caller must send via SwitcherClient.send_control_single()."""
    e = pwd_plain.encode()[:32].ljust(32, b"\x00")
    b = struct.pack("<II", did, uid) + e + bytes([0x01]) + struct.pack("<H", 6)
    b += struct.pack("<H", 1 if on else 0) + struct.pack("<I", isdelay)
    assert len(b) == 49
    return b




def body_plug_auto_shutdown(did, uid, pwd_plain: str, seconds: int) -> bytes:
    """OgeSPlugModel.getAutoTimeCMD lineNo==0 (OgeSPlugModel.java:60-98): 47 B
    plaintext body = DID u32, uid u32, pwd l(32), sub 0x04, len 4, seconds u32.
    Same wrapping as body_plug_single (send via send_control_single).
    aioswitcher SET_AUTO_OFF_SET_PACKET (api/packets.py) sends the identical
    tail `04 0400 <u32>` to V2/Touch/V4/Mini over the LAN, field-tested."""
    e = pwd_plain.encode()[:32].ljust(32, b"\x00")
    b = struct.pack("<II", did, uid) + e + bytes([0x04]) + struct.pack("<H", 4)
    b += struct.pack("<I", max(0, int(seconds)))
    assert len(b) == 47
    return b


def body_switch(did, uid, pwd_block, index: int, on: bool) -> bytes:
    """ei/b.v(): sub 0x01 len 2: index u8 (0 = all gangs, 1..7 = gang), state u8. (45 B)"""
    b = _head(did, uid, pwd_block, 0x01, 2)
    b += bytes([index & 0xFF, 1 if on else 0])
    return bytes(b)


def body_curtain_action(did, uid, pwd_block, action: int, line_no: int = 0,
                        moving: bool = False) -> bytes:
    """OgeCurtainModel.getMunualControlCMD: sub 0x02 len 2|3: [lineNo] u8, moving u8, action u8.
       action: 0 = stop, 1 = up/open, 2 = down/close (ACTION_MANUAL_UP/DOWN).
       aioswitcher stop_shutter sends the same `02 0200 0000` (Runner) and
       `02 0600 0{line}0000` (S11/S12), field-tested. Only stop is cross-checked;
       manual up/down (1/2) are app-only, the cover uses set-position instead."""
    length = 3 if line_no else 2
    b = _head(did, uid, pwd_block, 0x02, length)
    if line_no:
        b += bytes([line_no & 0xFF])
    b += bytes([1 if moving else 0, action & 0xFF])
    return bytes(b)


def body_curtain_position(did, uid, pwd_block, open_ratio: int, line_no: int = 0) -> bytes:
    """OgeCurtainModel.getControlCMD: sub 0x01 len 1|2: [lineNo] u8, openRatio u8 (0-100).
    aioswitcher set_position: RUNNER_SET_POSITION tail `01 0100 <pos>` and
    GENERAL_TOKEN_COMMAND `01 .. 0{line}<pos>`; 0 = closed, 100 = open."""
    length = 2 if line_no else 1
    b = _head(did, uid, pwd_block, 0x01, length)
    if line_no:
        b += bytes([line_no & 0xFF])
    b += bytes([max(0, min(100, int(open_ratio)))])
    return bytes(b)


def body_curtain_lock(did, uid, pwd_block, locked: bool, line_no: int = 0) -> bytes:
    """OgeCurtainModel.getControlLockCMD: sub 0x07 len 1|2: [lineNo] u8, lock u8.
    aioswitcher set_shutter_child_lock: RUNNER_SET_CHILD_LOCK tail `07 0100 <01|00>`."""
    length = 2 if line_no else 1
    b = _head(did, uid, pwd_block, 0x07, length)
    if line_no:
        b += bytes([line_no & 0xFF])
    b += bytes([1 if locked else 0])
    return bytes(b)


def body_light_toggle(did, uid, pwd_block, on: bool, delay: int = 0) -> bytes:
    """OgeLightModel sub 0x08 len 3: state u8 (1 = on, 2 = off; note off is 2, not 0), delay u16. (46 B)"""
    b = _head(did, uid, pwd_block, 0x08, 3)
    b += bytes([1 if on else 2]) + struct.pack("<H", delay & 0xFFFF)
    return bytes(b)


def next_seed(device: int, local: int | None, on: bool, last_device: int | None,
              hint_newer: bool) -> int | None:
    """Decide the new countdown seed, or None to keep counting from the old one.

    The cloud repeats a stale (higher) remaining value from an earlier refresh,
    so a real re-sync can only move the countdown DOWN. Accept the device value
    when: there is no local countdown yet, the device is off (seed 0), a timed
    run was just requested (hint_newer), the device value dropped below the
    local one by more than 3 s, or the local countdown ran out AND the device
    reports something other than the same stale number.
    """
    if not on:
        return 0
    if local is None or hint_newer:
        return device
    if device < local - 3:
        return device
    if local <= 0 and device != last_device:
        return device
    return None


# ---- parameter writes (businessCode 0x0E01, ei/b.z via OgeCommonDeviceModel.getWriteParamsCMD) ----
PARAM_AUTO_SHUTDOWN = 0x0C14     # 3092: S216 auto-shutdown limit in seconds (ag/z1.java:219-225, vf/o0 case 8)
AUTO_SHUTDOWN_MIN = 3660         # app clamps 1..3659 up to 3660 (61 min); 0 = unlimited


def body_write_params(did, uid, pwd_block, params: dict[int, bytes]) -> bytes:
    """ei/b.z(): DID u32, uid u32, pwd[32], count u16, ids[] u16, lens[] u16, values.
    Sent as businessCode 0x0E01 (3585), plaintext body like 0x0201."""
    b = bytearray(struct.pack("<II", did, uid)) + pwd_block
    b += struct.pack("<H", len(params))
    for pid in params:
        b += struct.pack("<H", pid)
    for v in params.values():
        b += struct.pack("<H", len(v))
    for v in params.values():
        b += v
    return bytes(b)


def body_auto_shutdown(did, uid, pwd_block, seconds: int) -> bytes:
    """Set the S216 auto-shutdown limit. 0 = unlimited; otherwise at least 3660 s.
    Capture-verified 2026-10-10: 0x0E01 body tail `0100 140c 0400 <i32 LE>`."""
    s = 0 if seconds <= 0 else max(AUTO_SHUTDOWN_MIN, int(seconds))
    return body_write_params(did, uid, pwd_block, {PARAM_AUTO_SHUTDOWN: struct.pack("<i", s)})


def body_read_params(did, uid, pwd_block, ids: list[int]) -> bytes:
    """0x0E02 parameter read (capture-verified): DID, uid, pwd[32], count u16, ids[] u16.
    The ack is empty; the value comes in a following 0x0103 push whose inner cmd is
    0x0E02 and payload = count u16, id u16, len u16, value."""
    b = bytearray(struct.pack("<II", did, uid)) + pwd_block + struct.pack("<H", len(ids))
    for pid in ids:
        b += struct.pack("<H", pid)
    return bytes(b)


def parse_params_payload(payload: bytes) -> dict[int, bytes]:
    """Decode a 0x0E02 push payload: count u16, then per-param id u16, len u16, value."""
    out: dict[int, bytes] = {}
    if len(payload) < 2:
        return out
    n = struct.unpack_from("<H", payload, 0)[0]
    o = 2
    for _ in range(n):
        if o + 4 > len(payload):
            break
        pid, ln = struct.unpack_from("<HH", payload, o)
        o += 4
        out[pid] = payload[o:o + ln]
        o += ln
    return out


# ---- light colour (OgeLightModel.getControlCMD sub 0x01, 55 B) ------------------
# RGBWT = five u16 LE, each 0..2048 (setRGB v*2048/255; setW/setT pct*2048/100;
# getRGB v*255/2048; getW/getT v*101/2048). No flags/mode byte. specialWay =
# effect: 0 none, 1 stream, 2 gradient, 3 breathe, 4 flashing, 7 colorful.
# This APK ships no colour-light UI at all (Switcher never sold one). The layout
# comes from the model's setters and getters, so what W and T mean, and which
# way T runs, is a best guess until someone tests a real light.
LIGHT_FULL = 2048
LIGHT_EFFECTS = {0: "none", 1: "stream", 2: "gradient", 3: "breathe", 4: "flashing", 7: "colorful"}


def rgbwt_bytes(r: int = 0, g: int = 0, b: int = 0, w_pct: int = 0, t_pct: int = 0) -> bytes:
    return struct.pack("<5H",
                       max(0, min(255, r)) * LIGHT_FULL // 255,
                       max(0, min(255, g)) * LIGHT_FULL // 255,
                       max(0, min(255, b)) * LIGHT_FULL // 255,
                       max(0, min(100, w_pct)) * LIGHT_FULL // 100,
                       max(0, min(100, t_pct)) * LIGHT_FULL // 100)


def body_light_color(did, uid, pwd_block, on: bool, rgbwt: bytes,
                     special_way: int = 0, light_type: int = 0) -> bytes:
    """sub 0x01 len 12: switch u8 (1 on / 2 off; 0xFF when lightType==4 strip),
    RGBWT[10], specialWay u8.  (55 B)"""
    sw = 0xFF if light_type == 4 else (1 if on else 2)
    b = _head(did, uid, pwd_block, 0x01, 12)
    b += bytes([sw]) + bytes(rgbwt[:10]).ljust(10, b"\x00") + bytes([special_way & 0xFF])
    return bytes(b)


# ---- WorkStatus parsers --------------------------------------------------------

@dataclass
class State:
    on: bool | None = None
    power_raw: int | None = None       # /220 → A (vendor convention)
    energy_kwh: float | None = None
    secs_on: int | None = None
    remaining: int | None = None       # seconds until auto-off (valid while on)
    lasttime: int | None = None
    autoclosing: int | None = None
    isdelay: int | None = None
    gangs: list[bool] | None = None    # multi-gang: index 1..n (index 0 = total)
    position: int | None = None        # curtain open ratio
    moving_up: bool | None = None
    moving_down: bool | None = None
    locked: bool | None = None
    wifi: int | None = None
    rgb: tuple[int, int, int] | None = None   # light: 0..255 each
    white_pct: int | None = None              # light: W channel 0..100
    temp_pct: int | None = None               # light: T channel 0..100 (direction unverified)
    effect: int | None = None                 # light: specialWay
    temperature: float | None = None          # breeze: room temperature C
    mode: int | None = None                   # breeze: 1 auto 2 dry 3 fan 4 cool 5 heat
    target_temp: int | None = None            # breeze: C
    fan: int | None = None                    # breeze: 0 auto 1 low 2 med 3 high
    swing: bool | None = None                 # breeze
    remote_id: str | None = None              # breeze: IRSet id (8 chars)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def _u64(b: bytes, o: int) -> int:
    return struct.unpack_from("<Q", b, o)[0]


def parse_onwall(b: bytes) -> State:
    """OgeSPlugS216Model.parseWorkStatus, checked against live bytes 2026-10-10.
    wifi u8@0, switch u8@1 (first of 8), power int32@9 (A = /220), voltage
    int32@13, elect int32@17, remaining u16@21 (seconds until auto-off; the
    device keeps the last value while off, so only meaningful when on),
    time on i32@25 (seconds since switched on, counts up while on and holds
    the last value while off; the app shows 00:00:00 then). The device does
    not report an isdelay flag in this layout (always 0 at @31)."""
    if len(b) < 32:
        return State()
    return State(wifi=b[0], on=b[1] == 1, power_raw=_u32(b, 9),
                 energy_kwh=round(_u32(b, 17) / 3_600_000 / 1000, 3),
                 remaining=_u16(b, 21), secs_on=_u32(b, 25))


def parse_bath_heater(b: bytes) -> State:
    """OgeBathHeaterModel.parseWorkStatus (Java accepts len 32 or 33 only):
    32 B: wifi, switch[8], power i32@9, voltage i32@13, current i32@17,
          lastTime u16@21, delay i32@23, openTime i32@27, isDelay u8@31.
    33 B: wifi, switch[8], power@9, voltage@13, current@17, delay i32@21,
          openTime i32@25, lastTime i32@29. (no isdelay byte)
    aioswitcher's Heater (031f) state parser (api/messages.py get_heater_*)
    reads the 33 B layout at the same offsets and names them: @1 state,
    @9 power (W), @21 auto shutdown, @25 time on, @29 time left. So Java's
    "lastTime" is the remaining run time, like S216's u16@21."""
    if len(b) == 32:
        rem = _u16(b, 21)
        return State(wifi=b[0], on=b[1] == 1, power_raw=_u32(b, 9),
                     lasttime=rem, remaining=rem, autoclosing=_u32(b, 23),
                     secs_on=_u32(b, 27), isdelay=b[31])
    if len(b) == 33:
        rem = _u32(b, 29)
        return State(wifi=b[0], on=b[1] == 1, power_raw=_u32(b, 9),
                     autoclosing=_u32(b, 21), secs_on=_u32(b, 25),
                     lasttime=rem, remaining=rem)
    return State()


def _parse_plug_line(b: bytes, o: int) -> State:
    """OgeSPlugModel.parse(f): switch u8, lasttime i32, opentime i32, autoclosing i32, isdelay u8 (14 B)."""
    return State(on=b[o] == 1, lasttime=_u32(b, o + 1), secs_on=_u32(b, o + 5),
                 autoclosing=_u32(b, o + 9), isdelay=b[o + 13])


def parse_plug(b: bytes, line_no: int = 0) -> State:
    """OgeSPlugModel.parseWorkStatus (OgeSPlugModel.java:285-361):
       65 B: wifi u8, per-line 16 B blocks @1/17/33 (lineNo 1..3)
       69 B: wifi u8, per-line 16 B blocks @1/17/33/49 (lineNo 1..4), trailing i32
       75 B: wifi u8, totalPowers i64@1, totalUsageTimes i32@9, currentPowers i64@13,
             then per-line blocks @21/37/53
       other (>=28): switch u16, power i32, elect i64, lasttime i32, opentime i32,
             autoclosing i32, direction u8, isdelay u8  (single plug).
    The single-plug layout is what aioswitcher parses for V2/Touch/V4/Mini/
    Power Plug (api/messages.py StateMessageParser, LAN reply byte 75 = our
    byte 0): state @0, power W u16 @2, time left @14, time on @18, auto
    shutdown @22. So "lasttime" = remaining run time; power raw = watts."""
    n = len(b)
    if n in (65, 69, 75):
        base = 21 if n == 75 else 1
        max_lines = 4 if n == 69 else 3
        ln = max(1, line_no)
        if ln > max_lines:
            return State(wifi=b[0])
        st = _parse_plug_line(b, base + (ln - 1) * 16)
        st.wifi = b[0]
        if n == 75:
            st.energy_kwh = round(_u64(b, 1) / 3_600_000 / 1000, 3)
            st.power_raw = int(_u64(b, 13))
        return st
    if n >= 28:
        rem = _u32(b, 14)
        return State(on=_u16(b, 0) == 1, power_raw=_u32(b, 2),
                     energy_kwh=round(_u64(b, 6) / 3_600_000 / 1000, 3),
                     lasttime=rem, remaining=rem, secs_on=_u32(b, 18),
                     autoclosing=_u32(b, 22), isdelay=b[27])
    return State()



def parse_mix_line(b: bytes, line_no: int, line_type: int) -> State:
    """OgeMixDeviceModel (main 15): one 0x1343 row per line sharing the DID.
    lineType 12 → curtain 8 B switchData block; else plug 16 B block
    (OgeSPlugAfterPanS11Model.java:316-334, OgeCurtainAfterPanS11Model.java:280-341)."""
    if line_type == 12:
        return parse_curtain(b, line_no)
    return parse_plug(b, line_no)


def parse_switch(b: bytes) -> State:
    """OgeSwitchModel.parseWorkStatus: wifi u8, switchData[8] (index 0 = total,
    1..7 = gangs, one byte each), power i32, voltage i32, electric i32,
    [23 B: delay u16 | 25 B: temperature u16]."""
    if len(b) < 21:
        return State()
    gangs = [b[1 + i] == 1 for i in range(8)]
    return State(wifi=b[0], on=gangs[0] or any(gangs[1:]), gangs=gangs,
                 power_raw=_u32(b, 9),
                 energy_kwh=round(_u32(b, 17) / 3_600_000 / 1000, 3))


def parse_curtain(b: bytes, line_no: int = 0) -> State:
    """OgeCurtainModel.parseWorkStatus: wifi u8, switchData[8] (65 B: per-line blocks).
    switchData (UI a1/e2.java:335): [0] position % (0..100), [1] lock, [2] moving up,
    [3] moving down, [4] calibrated, [5] mode (0 auto / 1 manual).
    aioswitcher reads the same 16 B stride: position @+0, child lock @+1,
    direction u16 @+2 ("0100" up, "0001" down, "0000" stop), block index =
    lineNo - 1 (device/tools.py get_shutter_*_packet_index). Field-tested."""
    if len(b) < 9:
        return State()
    off = 1
    if len(b) in (65, 69) and line_no in (2, 3, 4):
        off = 1 + (line_no - 1) * 16
    elif len(b) == 75 and line_no >= 1:
        off = 21 + (line_no - 1) * 16
    sd = b[off:off + 8]
    if len(sd) < 6:
        return State(wifi=b[0])
    pos = sd[0]
    return State(wifi=b[0], on=pos > 0, position=min(100, pos), locked=sd[1] == 1,
                 moving_up=sd[2] == 1, moving_down=sd[3] == 1)


def parse_light(b: bytes) -> State:
    """OgeLightModel.parseWorkStatus: switch u8 @0, RGBWT 5×u16 @1..10 (0..2048),
    specialWay @11, delay u16 @12, wifi @14. Read-back per getRGB/getW/getT."""
    if len(b) < 12:
        return State()
    r, g, bl, w, t = struct.unpack_from("<5H", b, 1)
    return State(on=b[0] == 1,
                 rgb=(r * 255 // LIGHT_FULL, g * 255 // LIGHT_FULL, bl * 255 // LIGHT_FULL),
                 white_pct=min(100, w * 101 // LIGHT_FULL),
                 temp_pct=min(100, t * 101 // LIGHT_FULL),
                 effect=b[11],
                 wifi=b[14] if len(b) > 14 else None)


AC_MODES = {1: "auto", 2: "dry", 3: "fan_only", 4: "cool", 5: "heat"}


def parse_aircon(b: bytes) -> State:
    """OgeAirConditionModel.parseWorkStatus (30 or 32 B): wifi u8, temperature
    u16 (tenths of C), ACData[4] = power, mode, target temp, fan<<4 | swing,
    tempUnit u8, deviceType u8, codeID str[8] (IRSet remote id), then
    lasttime/opentime/autoclosing i32, isdelay u8.
    aioswitcher's Breeze parser reads the identical layout (api/messages.py
    get_thermostat_*: temp u16, state, mode, target, fan nibble, swing nibble,
    remote id at +9..+17), field-tested."""
    if len(b) < 17:
        return State()
    return State(wifi=b[0], temperature=_u16(b, 1) / 10, on=b[3] == 1, mode=b[4],
                 target_temp=b[5], fan=b[6] >> 4, swing=bool(b[6] & 0x0F),
                 remote_id=b[9:17].decode("ascii", "replace").rstrip("\x00"))


PARSERS = {
    Family.ON_WALL: lambda b, ln, lt: parse_onwall(b),
    Family.BATH_HEATER: lambda b, ln, lt: parse_bath_heater(b),
    Family.PLUG: lambda b, ln, lt: parse_plug(b, ln),
    Family.SWITCH: lambda b, ln, lt: parse_switch(b),
    Family.CURTAIN: lambda b, ln, lt: parse_curtain(b, ln),
    Family.LIGHT: lambda b, ln, lt: parse_light(b),
    Family.MIX: parse_mix_line,
    Family.AIRCON: lambda b, ln, lt: parse_aircon(b),
}

# Families whose WorkStatus carries the S216-style power/energy fields.
ENERGY_FAMILIES = {Family.ON_WALL, Family.BATH_HEATER, Family.PLUG, Family.SWITCH, Family.MIX}
# Families whose boiler members (see BOILER_TYPES) have an auto-shutdown write path:
# ON_WALL via parameter 0x0C14 (capture-verified), PLUG via sub 0x04 (app + aioswitcher).
# BATH_HEATER has no write path in either source, so it only reports the value.
AUTO_SHUTDOWN_WRITABLE = {Family.ON_WALL, Family.PLUG}
# Families with a simple on/off we can drive (MIX: light lines only).
SWITCHABLE = {Family.ON_WALL, Family.BATH_HEATER, Family.PLUG, Family.SWITCH, Family.LIGHT, Family.MIX}

MODEL_NAMES = {
    Family.ON_WALL: "On Wall (S216)",
    Family.BATH_HEATER: "Heater",
    Family.PLUG: "Power plug",
    Family.SWITCH: "Wall switch",
    Family.CURTAIN: "Runner / shutter",
    Family.LIGHT: "Colour light",
    Family.MIX: "Runner / Light panel",
    Family.AIRCON: "Breeze (sensor only)",
    Family.UNKNOWN: "Unknown device",
}
