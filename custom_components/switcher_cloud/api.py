"""Switcher Cloud cloud client — blocking TCP protocol, asyncio-wrapped.

Protocol (reverse-engineered from Switcher 7.53, verified by live capture 2026-10-10):
- TCP to appserver.switcher.co.il:9091, fef0 frames, two-step CRC-CCITT.
- Auth packets (0x1102) use header version b"\x02\x32"; business b"\x03\x05",
  control=0x0000, relation_id = uid LE u32 + 2 zero bytes. Login and business
  commands share ONE TCP connection.
- Bodies AES-128/ECB/PKCS5 key "OGEseetime201800"; replies too (decrypt first).
- Device list 0x1343 JSON; VisitPwd base64 -> AES-256/ECB decrypt w/ commKey.
- On/off 0x0201 sub 0x23, 48B body, pwd block native AES-192 key "OGE2016…".
"""
from __future__ import annotations

import base64
import json
import logging
import socket
import struct
import time
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

_LOGGER = logging.getLogger(__name__)

HOST, PORT = "appserver.switcher.co.il", 9091
BODY_KEY = b"OGEseetime201800"                      # AES-128 login-body key
NATIVE_KEY = b"OGE201600000000000000000"            # AES-192 device-command key
DUID = b"\x00" * 6

START_FLAG = b"\xfe\xf0"
END_FLAG = b"\xf0\xfe"
HEADER_LEN = 40


class SwitcherApiError(Exception):
    pass


class SwitcherAuthError(SwitcherApiError):
    pass


# ---- framing ------------------------------------------------------------------

def _crc16(data: bytes, init: int = 0x1021) -> int:
    s = init
    for b in data:
        s ^= b << 8
        for _ in range(8):
            s = ((s << 1) ^ 0x1021) & 0xFFFF if s & 0x8000 else (s << 1) & 0xFFFF
    return s


def _crc_trailer(hb: bytes, session_key: bytes) -> bytes:
    crc1 = _crc16(hb)
    crc2 = _crc16(struct.pack("<H", crc1) + session_key)
    return struct.pack("<HH", crc1, crc2)


def build_packet(cmd, body, serial, session, *, session_key=b"\x00" * 32,
                 msg_type=1, control=0, relation_id=b"\x00" * 6,
                 timestamp=0, version=b"\x03\x05") -> bytes:
    h = bytearray(HEADER_LEN)
    h[0:2] = START_FLAG
    h[2:4] = struct.pack("<H", len(body) + 44)
    h[4:6] = version
    h[6:8] = struct.pack("<H", cmd)
    h[8:12] = struct.pack("<I", session & 0xFFFFFFFF)
    h[12:14] = struct.pack("<H", serial & 0xFFFF)
    h[14] = msg_type
    h[16:18] = struct.pack("<H", control)
    h[18:24] = relation_id
    h[24:28] = struct.pack("<I", timestamp & 0xFFFFFFFF)
    h[38:40] = END_FLAG
    hb = bytes(h) + body
    return hb + _crc_trailer(hb, session_key)


def parse_packet(pkt: bytes) -> dict:
    total = struct.unpack_from("<H", pkt, 2)[0]
    return {
        "cmd": struct.unpack_from("<H", pkt, 6)[0],
        "session": struct.unpack_from("<I", pkt, 8)[0],
        "serial": struct.unpack_from("<H", pkt, 12)[0],
        "msg_type": pkt[14],
        "errcode": pkt[15],
        "body": pkt[HEADER_LEN:total - 4],
    }


# ---- body crypto ---------------------------------------------------------------

def _aes_enc(raw: bytes) -> bytes:
    return AES.new(BODY_KEY, AES.MODE_ECB).encrypt(pad(raw, 16))


def _aes_dec(ct: bytes) -> bytes:
    return AES.new(BODY_KEY, AES.MODE_ECB).decrypt(ct)


def _b_a_decrypt(ct: bytes, comm_key: bytes) -> str:
    """common.b.a: AES-256/ECB/NoPadding with the 32-byte session comm-key."""
    return (AES.new(comm_key, AES.MODE_ECB).decrypt(ct)
            .decode("utf-8", "replace").rstrip("\x00").strip())


def _b_c_encrypt(pwd: str) -> bytes:
    """common.b.c: pad to 32B then native AES-192 (device pwd block)."""
    raw = pwd.encode()[:32].ljust(32, b"\x00")
    return AES.new(NATIVE_KEY, AES.MODE_ECB).encrypt(raw)


def _l(n: int, s: str) -> bytes:
    e = s.encode() if s else b""
    if not e or n < len(e):
        return b"\x00" * n
    return e[:n] + b"\x00" * (n - len(e))


def _login_body(account: str, password: str, ts: int) -> bytes:
    b = bytearray()
    b += b"\x01" + b"10".ljust(16, b"\x00") + bytes([2, 50])
    b += b"PAMDS001" + bytes([7, 53])
    b += DUID + b"\x00" * 6 + b"\x00" * 4
    b += b"\x03"
    b += _l(32, account) + _l(32, password)
    b += struct.pack("<I", 0) + struct.pack("<I", ts)
    b += b"\x00" * 32
    assert len(b) == 150
    return bytes(b)


def _parse_login_reply(body: bytes) -> dict:
    session, uid = struct.unpack_from("<ii", body, 0)
    return {
        "session": session & 0xFFFFFFFF,
        "uid": uid & 0xFFFFFFFF,
        "comm_key": body[8:40],
    }


def parse_workstatus(b: bytes) -> dict:
    if len(b) < 10:
        return {}
    secs_on = struct.unpack_from("<I", b, 8)[0]
    return {"on": b[1] == 1, "secs_on": secs_on}


# ---- client ---------------------------------------------------------------------

class SwitcherClient:
    """Blocking TCP client; HA wraps calls in asyncio.to_thread."""

    def __init__(self, account: str, password: str):
        self._account = account
        self._password = password
        self._sock: socket.socket | None = None
        self._serial = 1000
        self.session = 0
        self.uid = 0
        self.comm_key = b"\x00" * 32

    def _next_serial(self) -> int:
        self._serial = 1001 if self._serial == 0x7FFF else self._serial + 1
        return self._serial

    def _recv_packet(self) -> bytes:
        s = self._sock
        assert s is not None
        hdr = b""
        while len(hdr) < HEADER_LEN:
            chunk = s.recv(HEADER_LEN - len(hdr))
            if not chunk:
                raise ConnectionError("closed during header")
            hdr += chunk
        total = struct.unpack_from("<H", hdr, 2)[0]
        rest = b""
        while len(hdr) + len(rest) < total:
            chunk = s.recv(total - len(hdr) - len(rest))
            if not chunk:
                raise ConnectionError("closed during body")
            rest += chunk
        return hdr + rest

    def _send(self, cmd: int, body: bytes, *, encrypted: bool = True,
              version: bytes = b"\x03\x05", control: int = 0) -> dict:
        assert self._sock is not None, "not connected"
        wire = _aes_enc(body) if encrypted else body
        pkt = build_packet(cmd, wire, self._next_serial(), self.session,
                           session_key=self.comm_key, control=control,
                           relation_id=struct.pack("<I", self.uid).ljust(6, b"\x00"),
                           timestamp=int(time.time()), version=version)
        self._sock.sendall(pkt)
        deadline = time.time() + 15
        while time.time() < deadline:
            self._sock.settimeout(max(1, deadline - time.time()))
            p = parse_packet(self._recv_packet())
            if p["cmd"] == cmd or p["cmd"] in (0x0103, 0x0102):
                if p["cmd"] == cmd:
                    return p
            else:
                return p
        raise SwitcherApiError(f"timeout waiting for 0x{cmd:04x}")

    def connect(self) -> None:
        """Open TCP + login (0x1102, version 2.50)."""
        self.close()
        self._sock = socket.create_connection((HOST, PORT), timeout=15)
        self._serial = 1000
        pkt = build_packet(0x1102, _aes_enc(_login_body(self._account,
                                                        self._password,
                                                        int(time.time()))),
                           self._next_serial(), 0, msg_type=1, control=0x0100,
                           relation_id=b"\x00" * 6, timestamp=int(time.time()),
                           version=b"\x02\x32")
        self._sock.sendall(pkt)
        p = parse_packet(self._recv_packet())
        if p["errcode"] != 0:
            self.close()
            raise SwitcherAuthError(f"login errcode={p['errcode']}")
        info = _parse_login_reply(_aes_dec(p["body"]))
        self.session, self.uid, self.comm_key = (
            info["session"], info["uid"], info["comm_key"])
        _LOGGER.debug("logged in session=%s uid=%s", self.session, self.uid)

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _ensure(self) -> None:
        if self._sock is None:
            self.connect()

    def _retrying(self, fn):
        self._ensure()
        try:
            return fn()
        except (OSError, SwitcherApiError, struct.error) as err:
            _LOGGER.debug("request failed (%s); reconnecting once", err)
            self.close()
            self.connect()
            return fn()

    def get_devices(self) -> list[dict[str, Any]]:
        def go():
            p = self._send(0x1343, b"")
            if p["errcode"] != 0:
                raise SwitcherApiError(f"device list errcode={p['errcode']}")
            body = p["body"]
            if len(body) % 16 == 0:
                try:
                    body = _aes_dec(body)
                except ValueError:
                    pass
            i = body.find(b"{")
            if i < 0:
                return []
            data = json.loads(body[i:].decode("utf-8", "replace"))
            return data.get("DevList") or data.get("devList") or []
        return self._retrying(go)

    def device_password(self, device: dict[str, Any]) -> str:
        return _b_a_decrypt(base64.b64decode(device["VisitPwd"]), self.comm_key)

    def set_switch(self, device_id: int, device_pwd: str, on: bool) -> None:
        def go():
            b = bytearray(48)
            struct.pack_into("<I", b, 0, device_id)
            struct.pack_into("<I", b, 4, self.uid)
            b[8:40] = _b_c_encrypt(device_pwd)
            b[40] = 0x23
            struct.pack_into("<H", b, 41, 5)
            b[43] = 1 if on else 0
            struct.pack_into("<I", b, 44, 0)
            p = self._send(0x0201, bytes(b))
            if p["errcode"] != 0:
                raise SwitcherApiError(f"control errcode={p['errcode']}")
        self._retrying(go)

    @staticmethod
    def device_state(device: dict[str, Any]) -> dict:
        ws = base64.b64decode(device.get("WorkStatus") or "")
        st = parse_workstatus(ws)
        st["name"] = device.get("DeviceName")
        st["did"] = device.get("DID")
        st["online"] = device.get("OnlineStatus") == 1
        st["dmac"] = device.get("DMAC")
        st["model"] = f'S216 (fw {device.get("FirmwareVersion", "?")})'
        return st
