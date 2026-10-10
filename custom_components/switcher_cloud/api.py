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
import contextlib
import json
import logging
import queue
import socket
import struct
import threading
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


def parse_push(body: bytes) -> dict | None:
    """0x0103 push body (live-captured): cmd u16, serial u16, ts u32,
    then device-specific bytes (sub 0x23 = control ack)."""
    if len(body) < 8:
        return None
    return {
        "cmd": struct.unpack_from("<H", body, 0)[0],
        "serial": struct.unpack_from("<H", body, 2)[0],
        "ts": struct.unpack_from("<I", body, 4)[0],
        "payload": body[8:],
    }


def parse_workstatus(b: bytes) -> dict:
    """OgeSPlugS216Model.parseWorkStatus (decompiled, capture-verified):
    wifiPower u8@0, switchState @1, power int32@9 (W = /220), voltage int32@13,
    elect int32@17 (kWh = /3600000/1000), u16@21, lasttime int32@23,
    opentime int32@27, isdelay u8@31."""
    if len(b) < 32:
        return {}
    def u32(o):
        return struct.unpack_from("<I", b, o)[0]
    return {
        "on": b[1] == 1,
        "power_raw": u32(9),
        "energy_kwh": round(u32(17) / 3_600_000 / 1000, 2),
        "lasttime": u32(23),
        "secs_on": u32(27),
        "isdelay": b[31],
    }


# ---- client ---------------------------------------------------------------------

HEARTBEAT_INTERVAL = 25       # app: 0x1104 every ~10 s; we can be gentler
PUSH_CMD = 0x0103             # server pushes (device ack / status relay)


class SwitcherClient:
    """Blocking TCP client with a persistent connection.

    One background listener thread owns the socket: it logs in, receives every
    packet, delivers replies to waiting `request()` callers (aux-cloud-style
    realtime channel), forwards 0x0103 pushes to `on_push`, and heartbeats
    0x1104 on idle. If the connection drops it reconnects + re-logins.
    """

    def __init__(self, account: str, password: str):
        self._account = account
        self._password = password
        self._sock: socket.socket | None = None
        self._serial = 1000
        self.session = 0
        self.uid = 0
        self.comm_key = b"\x00" * 32
        self._send_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[dict]] = {}
        self._listener: threading.Thread | None = None
        self._stopping = threading.Event()
        self._on_push = None                 # callable(dict) in caller's thread
        self._on_reconnect = None            # callable() in listener thread

    # -- serial ------------------------------------------------------------------
    def _next_serial(self) -> int:
        self._serial = 1001 if self._serial == 0x7FFF else self._serial + 1
        return self._serial

    # -- socket ------------------------------------------------------------------
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

    def _raw_send(self, cmd: int, body: bytes, *, encrypted: bool = True,
                  version: bytes = b"\x03\x05", control: int = 0) -> None:
        s = self._sock
        assert s is not None, "not connected"
        wire = _aes_enc(body) if encrypted else body
        pkt = build_packet(cmd, wire, self._next_serial(), self.session,
                           session_key=self.comm_key, control=control,
                           relation_id=struct.pack("<I", self.uid).ljust(6, b"\x00"),
                           timestamp=int(time.time()), version=version)
        s.sendall(pkt)

    def connect(self) -> None:
        """Open TCP + login (0x1102, version 2.50)."""
        self.close()
        self._sock = socket.create_connection((HOST, PORT), timeout=15)
        self._serial = 1000
        self._raw_send(0x1102, _login_body(self._account, self._password,
                                           int(time.time())),
                       encrypted=True, version=b"\x02\x32", control=0x0100)
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

    # -- listener thread (realtime channel) --------------------------------------
    def start_listener(self, on_push=None, on_reconnect=None) -> None:
        """Start (or ensure) the persistent receive thread. Non-blocking."""
        self._on_push = on_push or self._on_push
        self._on_reconnect = on_reconnect or self._on_reconnect
        if self._listener and self._listener.is_alive():
            return
        self._stopping.clear()
        self._listener = threading.Thread(
            target=self._listener_loop, name="switcher-cloud-listener", daemon=True)
        self._listener.start()

    def stop_listener(self) -> None:
        self._stopping.set()
        self.close()

    def _listener_loop(self) -> None:
        reconnects = 0
        while not self._stopping.is_set():
            try:
                if self._sock is None:
                    self.connect()
                    reconnects = 0
                    if self._on_reconnect:
                        try:
                            self._on_reconnect()
                        except Exception:  # noqa: BLE001
                            _LOGGER.exception("on_reconnect callback failed")
                self._sock.settimeout(HEARTBEAT_INTERVAL)
                p = parse_packet(self._recv_packet())
                self._dispatch(p)
            except socket.timeout:
                with contextlib.suppress(OSError):
                    self._raw_send(0x1104, b"")       # heartbeat on idle
            except Exception as err:  # noqa: BLE001
                if self._stopping.is_set():
                    break
                self.close()
                reconnects += 1
                delay = min(3 * reconnects, 30)
                _LOGGER.warning("switcher connection lost (%s); retry in %ss",
                                err, delay)
                if self._stopping.wait(delay):
                    break

    def _dispatch(self, p: dict) -> None:
        if p["cmd"] == PUSH_CMD:
            push = parse_push(p["body"])
            if push and self._on_push:
                try:
                    self._on_push(push)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("push callback failed")
            # pushes also resolve any pending control command implicitly
            q = self._pending.get(push["cmd"]) if push else None
        else:
            q = self._pending.get(p["cmd"])
        if q is None:
            q = self._pending.get(0)                  # catch-all slot
        if q is not None:
            try:
                q.put_nowait(p)
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    q.get_nowait()
                    q.put_nowait(p)

    # -- request bridge ----------------------------------------------------------
    def request(self, cmd: int, body: bytes, *, encrypted: bool = True,
                version: bytes = b"\x03\x05", control: int = 0,
                timeout: float = 15) -> dict:
        """Send one packet on the persistent connection and wait for its reply.
        Reconnects once on failure."""
        if self._listener is None or not self._listener.is_alive():
            self.start_listener()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._sock is None:      # listener still connecting
                time.sleep(0.2)
                continue
            q = self._pending.setdefault(cmd, queue.Queue(maxsize=8))
            with contextlib.suppress(queue.Empty):
                q.get_nowait()                        # drop stale replies
            try:
                with self._send_lock:
                    self._raw_send(cmd, body, encrypted=encrypted,
                                   version=version, control=control)
                return q.get(timeout=max(1, deadline - time.time()))
            except (OSError, queue.Empty) as err:
                self.close()
                _LOGGER.debug("request 0x%04x failed (%s); reconnecting", cmd, err)
                time.sleep(1)
        raise SwitcherApiError(f"timeout waiting for 0x{cmd:04x}")

    # -- high-level API ----------------------------------------------------------
    def get_devices(self) -> list[dict[str, Any]]:
        p = self.request(0x1343, b"", encrypted=True)
        if p["errcode"] != 0:
            raise SwitcherApiError(f"device list errcode={p['errcode']}")
        # 0x1343 reply body is PLAINTEXT JSON (live-capture verified);
        # only the 0x1102 login reply is AES-encrypted.
        body = p["body"]
        i = body.find(b"{")
        if i < 0:
            return []
        data = json.loads(body[i:].decode("utf-8", "replace"))
        return data.get("DevList") or data.get("devList") or []

    def device_password(self, device: dict[str, Any]) -> str:
        return _b_a_decrypt(base64.b64decode(device["VisitPwd"]), self.comm_key)

    def set_switch(self, device_id: int, device_pwd: str, on: bool) -> None:
        b = bytearray(48)
        struct.pack_into("<I", b, 0, device_id)
        struct.pack_into("<I", b, 4, self.uid)
        b[8:40] = _b_c_encrypt(device_pwd)
        b[40] = 0x23
        struct.pack_into("<H", b, 41, 5)
        b[43] = 1 if on else 0
        struct.pack_into("<I", b, 44, 0)
        # control body is sent PLAINTEXT (live capture); only the embedded
        # pwd block is pre-encrypted with the native key
        p = self.request(0x0201, bytes(b), encrypted=False)
        if p["errcode"] != 0:
            raise SwitcherApiError(f"control errcode={p['errcode']}")

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
