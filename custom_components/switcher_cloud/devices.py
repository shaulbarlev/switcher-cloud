"""Device families: classification, command builders, WorkStatus parsers.

Every entry here is reconstructed from the decompiled Switcher Smart Home 7.53
app (com.ogemray.data.model.Oge*Model + ei/b.java builders). Only ON_WALL (S216)
has been exercised against real hardware; the rest are byte-exact transcriptions
of what the official app sends and must be treated as UNVERIFIED.

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
    ON_WALL = "on_wall"            # OgeSPlugS216Model  (3/33)   VERIFIED
    BATH_HEATER = "bath_heater"    # OgeBathHeaterModel (3/31)
    PLUG = "plug"                  # OgeSPlugModel      (3/11,15,23; 1/161,167,168)
    SOCKET = "socket"              # OgeSocketModel     (1/273)
    SWITCH = "switch"              # OgeSwitchModel     (other 1/x, 3/x) multi-gang
    CURTAIN = "curtain"            # OgeCurtainModel    (12/x)
    LIGHT = "light"                # OgeLightModel      (8/x)
    AIRCON = "aircon"              # OgeAirConditionModel (14/x) — not supported
    UNKNOWN = "unknown"


VERIFIED = {Family.ON_WALL}


def classify(device_type: str) -> tuple[Family, int, int]:
    """Return (family, mainType, subType) for a 0x1343 DeviceType string."""
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
    if main == 1 and sub == 273:          # unreachable with 2-hex-digit sub; kept for parity
        return Family.SOCKET, main, sub
    if main in (1, 3):
        return Family.SWITCH, main, sub
    if main == 12:
        return Family.CURTAIN, main, sub
    if main == 14:
        return Family.AIRCON, main, sub
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
    """ei/b.w(): sub 0x23 len 5: state u8, isdelay u32.  (48 B)  VERIFIED."""
    b = _head(did, uid, pwd_block, 0x23, 5)
    b += bytes([1 if on else 0]) + struct.pack("<I", isdelay)
    return bytes(b)


def body_bath_heater(did, uid, pwd_block, on: bool, delay: int = 0) -> bytes:
    """OgeBathHeaterModel.getControlCMD: sub 0x23 len 5: state u8, delayTime u32. (48 B)"""
    b = _head(did, uid, pwd_block, 0x23, 5)
    b += bytes([1 if on else 0]) + struct.pack("<I", delay)
    return bytes(b)


def body_plug(did, uid, pwd_block, on: bool, line_no: int = 0, line_type: int = 0,
              isdelay: int = 0) -> bytes:
    """OgeSPlugModel.getControlCMD, cloud paths only:
       lineNo != 0 : sub 0x0A len 6: lineNo u8, state u8, isdelay u32     (49 B)
       lineType!=0 : sub 0x0A len 6: lineType u8, state u8, autoclosing u32 (49 B)
       else        : the app uses a LAN-only variant (plaintext pwd, sub 0x01);
                     for cloud we send the lineType form with lineType=0, which
                     the firmware treats as 'the only line'."""
    sel = line_no if line_no else line_type
    b = _head(did, uid, pwd_block, 0x0A, 6)
    b += bytes([sel & 0xFF, 1 if on else 0]) + struct.pack("<I", isdelay)
    return bytes(b)


body_socket = body_plug      # OgeSocketModel is byte-identical to OgeSPlugModel here


def body_switch(did, uid, pwd_block, index: int, on: bool) -> bytes:
    """ei/b.v(): sub 0x01 len 2: index u8 (0 = all gangs, 1..7 = gang), state u8. (45 B)"""
    b = _head(did, uid, pwd_block, 0x01, 2)
    b += bytes([index & 0xFF, 1 if on else 0])
    return bytes(b)


def body_curtain_action(did, uid, pwd_block, action: int, line_no: int = 0,
                        moving: bool = False) -> bytes:
    """OgeCurtainModel action cmd: sub 0x02 len 2|3: [lineNo] u8, moving u8, action u8.
       action: 0 = stop, 1 = up/open, 2 = down/close (ACTION_MANUAL_UP/DOWN)."""
    length = 3 if line_no else 2
    b = _head(did, uid, pwd_block, 0x02, length)
    if line_no:
        b += bytes([line_no & 0xFF])
    b += bytes([1 if moving else 0, action & 0xFF])
    return bytes(b)


def body_curtain_position(did, uid, pwd_block, open_ratio: int, line_no: int = 0) -> bytes:
    """OgeCurtainModel.getControlCMD: sub 0x01 len 1|2: [lineNo] u8, openRatio u8 (0-100)."""
    length = 2 if line_no else 1
    b = _head(did, uid, pwd_block, 0x01, length)
    if line_no:
        b += bytes([line_no & 0xFF])
    b += bytes([max(0, min(100, int(open_ratio)))])
    return bytes(b)


def body_light_toggle(did, uid, pwd_block, on: bool, delay: int = 0) -> bytes:
    """OgeLightModel sub 0x08 len 3: state u8 (1 = on, 2 = OFF — note 2, not 0), delay u16. (46 B)"""
    b = _head(did, uid, pwd_block, 0x08, 3)
    b += bytes([1 if on else 2]) + struct.pack("<H", delay & 0xFFFF)
    return bytes(b)


# ---- WorkStatus parsers --------------------------------------------------------

@dataclass
class State:
    on: bool | None = None
    power_raw: int | None = None       # /220 → A (vendor convention)
    energy_kwh: float | None = None
    secs_on: int | None = None
    lasttime: int | None = None
    autoclosing: int | None = None
    isdelay: int | None = None
    gangs: list[bool] | None = None    # multi-gang: index 1..n (index 0 = total)
    position: int | None = None        # curtain open ratio
    moving_up: bool | None = None
    moving_down: bool | None = None
    locked: bool | None = None
    wifi: int | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def _u64(b: bytes, o: int) -> int:
    return struct.unpack_from("<Q", b, o)[0]


def parse_onwall(b: bytes) -> State:
    """OgeSPlugS216Model.parseWorkStatus (>=32 B): wifi u8, switch u8 (first of 8),
    skip 7, power i32, voltage i32, elect i32, u16, lasttime i32, opentime i32, isdelay u8."""
    if len(b) < 32:
        return State()
    return State(wifi=b[0], on=b[1] == 1, power_raw=_u32(b, 9),
                 energy_kwh=round(_u32(b, 17) / 3_600_000 / 1000, 3),
                 lasttime=_u32(b, 23), secs_on=_u32(b, 27), isdelay=b[31])


def parse_bath_heater(b: bytes) -> State:
    """OgeBathHeaterModel.parseWorkStatus, 32 B: wifi, switch[8], power i32, voltage i32,
    current i32, lasttime u16, delay i32, opentime i32, isdelay u8.
    33 B variant: same without lasttime, +1 trailing byte."""
    if len(b) < 32:
        return State()
    st = State(wifi=b[0], on=b[1] == 1, power_raw=_u32(b, 9))
    if len(b) == 32:
        st.lasttime = _u16(b, 21); st.autoclosing = _u32(b, 23); st.secs_on = _u32(b, 27); st.isdelay = b[31]
    else:
        st.autoclosing = _u32(b, 21); st.secs_on = _u32(b, 25); st.isdelay = b[29]
    return st


def _parse_plug_line(b: bytes, o: int) -> State:
    """OgeSPlugModel.parse(f): switch u8, lasttime i32, opentime i32, autoclosing i32, isdelay u8 (14 B)."""
    return State(on=b[o] == 1, lasttime=_u32(b, o + 1), secs_on=_u32(b, o + 5),
                 autoclosing=_u32(b, o + 9), isdelay=b[o + 13])


def parse_plug(b: bytes, line_no: int = 0) -> State:
    """OgeSPlugModel.parseWorkStatus:
       65 B: wifi u8, then per-line 16 B blocks (line 1 @1, line 2 @17, line 3 @33)
       75 B: wifi u8, i64 i32 i64 (energy header, 20 B), then per-line blocks @21/37/53
       other: switch u16, power i32, elect i64, lasttime i32, opentime i32,
              autoclosing i32, direction u8, isdelay u8  (single plug, 28 B)."""
    n = len(b)
    if n in (65, 75):
        base = 1 if n == 65 else 21
        ln = max(1, line_no)
        if ln > 3:
            return State(wifi=b[0])
        st = _parse_plug_line(b, base + (ln - 1) * 16)
        st.wifi = b[0]
        return st
    if n >= 28 and n != 69:
        return State(on=_u16(b, 0) == 1, power_raw=_u32(b, 2),
                     energy_kwh=round(_u64(b, 6) / 3_600_000 / 1000, 3),
                     lasttime=_u32(b, 14), secs_on=_u32(b, 18),
                     autoclosing=_u32(b, 22), isdelay=b[27])
    return State()


parse_socket = parse_plug


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
    switchData: [0] opened/on, [1] lock, [2] moving up, [3] moving down, [4] calibrated."""
    if len(b) < 9:
        return State()
    off = 1
    if len(b) == 65 and line_no in (2, 3):
        off = 1 + (line_no - 1) * 16
    sd = b[off:off + 8]
    if len(sd) < 5:
        return State(wifi=b[0])
    return State(wifi=b[0], on=sd[0] > 0, locked=sd[1] == 1,
                 moving_up=sd[2] == 1, moving_down=sd[3] == 1)


def parse_light(b: bytes) -> State:
    """OgeLightModel.parseWorkStatus: switch u8 @0, RGBWT[10] @1, specialWay @11,
    delay u16 @12, wifi @14."""
    if len(b) < 14:
        return State()
    return State(on=b[0] == 1, wifi=b[14] if len(b) > 14 else None)


PARSERS = {
    Family.ON_WALL: lambda b, ln: parse_onwall(b),
    Family.BATH_HEATER: lambda b, ln: parse_bath_heater(b),
    Family.PLUG: parse_plug,
    Family.SOCKET: parse_socket,
    Family.SWITCH: lambda b, ln: parse_switch(b),
    Family.CURTAIN: parse_curtain,
    Family.LIGHT: lambda b, ln: parse_light(b),
}

# Families whose WorkStatus carries the S216-style power/energy fields.
ENERGY_FAMILIES = {Family.ON_WALL, Family.BATH_HEATER, Family.PLUG, Family.SOCKET, Family.SWITCH}
# Boiler-type devices → water_heater entity.
BOILER_FAMILIES = {Family.ON_WALL, Family.BATH_HEATER}
# Families with a simple on/off we can drive.
SWITCHABLE = {Family.ON_WALL, Family.BATH_HEATER, Family.PLUG, Family.SOCKET, Family.SWITCH, Family.LIGHT}

MODEL_NAMES = {
    Family.ON_WALL: "On Wall (S216)",
    Family.BATH_HEATER: "Bath heater / Touch",
    Family.PLUG: "Power plug",
    Family.SOCKET: "Socket",
    Family.SWITCH: "Wall switch",
    Family.CURTAIN: "Runner / shutter",
    Family.LIGHT: "Light",
    Family.AIRCON: "Breeze (unsupported)",
    Family.UNKNOWN: "Unknown device",
}
