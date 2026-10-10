"""Switcher cloud client. Blocking TCP; Home Assistant runs it in the executor.

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
    if len(pkt) < HEADER_LEN + 4 or pkt[0:2] != START_FLAG or pkt[38:40] != END_FLAG:
        raise ConnectionError("bad frame")                 # desync → reconnect
    total = struct.unpack_from("<H", pkt, 2)[0]
    if total < HEADER_LEN + 4 or total > len(pkt):
        raise ConnectionError("bad frame length")
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
    if len(e) > n:
        raise SwitcherAuthError(f"field longer than {n} bytes")   # creds can never log in
    return e + b"\x00" * (n - len(e))


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
    """0x0103 push body (DataParser0x0103.java:23-40): inner cmd u16@0, inner
    serial u16@2, ts u32@4, status u8@8 (0 or 3 = accepted), len u16@9,
    payload@11..11+len. The app re-wraps payload as a reply packet for the
    inner cmd (e.g. sub 0x23 ack = [0x23][result u8][u16])."""
    if len(body) < 11:
        return None
    ln = struct.unpack_from("<H", body, 9)[0]
    return {
        "cmd": struct.unpack_from("<H", body, 0)[0],
        "serial": struct.unpack_from("<H", body, 2)[0],
        "ts": struct.unpack_from("<I", body, 4)[0],
        "status": body[8],
        "payload": body[11:11 + ln],
    }




# ---- client ---------------------------------------------------------------------

HEARTBEAT_INTERVAL = 25       # app: 0x1104 every ~10 s; we can be gentler
PUSH_CMD = 0x0103             # server pushes (device ack / status relay)
# login errcodes that mean "credentials wrong" (app ReuestError strings:
# 5 account not registered, 11 wrong password, 13 account error)
AUTH_ERRCODES = {5, 11, 13}
AUTO_SHUTDOWN_TTL = 600       # s; the 0x0C14 value only changes on a write


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
        self._pending: dict[int, queue.Queue] = {}
        self.auth_error: SwitcherAuthError | None = None
        self._warned_types: set = set()
        self._auto_shutdown_cache: dict[int, tuple[int, float]] = {}
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
        if hdr[0:2] != START_FLAG or total < HEADER_LEN + 4:
            raise ConnectionError("bad frame header")       # desync → reconnect
        rest = b""
        while len(hdr) + len(rest) < total:
            chunk = s.recv(total - len(hdr) - len(rest))
            if not chunk:
                raise ConnectionError("closed during body")
            rest += chunk
        return hdr + rest

    def _raw_send(self, cmd: int, body: bytes, *, encrypted: bool = True,
                  version: bytes = b"\x03\x05", control: int = 0,
                  relation_id: bytes | None = None) -> int:
        """Send one packet; returns the serial used (for reply matching)."""
        s = self._sock
        assert s is not None, "not connected"
        wire = _aes_enc(body) if encrypted else body
        serial = self._next_serial()
        pkt = build_packet(cmd, wire, serial, self.session,
                           session_key=self.comm_key, control=control,
                           relation_id=relation_id if relation_id is not None
                           else struct.pack("<I", self.uid).ljust(6, b"\x00"),
                           timestamp=int(time.time()), version=version)
        s.sendall(pkt)
        return serial

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
            # only credential-class errcodes are auth failures; anything else
            # (server-side 1xx errors, 6 "refresh", 12 pre-auth gate) is transient
            if p["errcode"] in AUTH_ERRCODES:
                raise SwitcherAuthError(f"login errcode={p['errcode']}")
            raise SwitcherApiError(f"login errcode={p['errcode']}")
        info = _parse_login_reply(_aes_dec(p["body"]))
        self.session, self.uid, self.comm_key = (
            info["session"], info["uid"], info["comm_key"])
        _LOGGER.debug("logged in session=%s uid=%s", self.session, self.uid)

    def close(self) -> None:
        if self._sock:
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RDWR)   # wake a blocked recv
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None

    # -- listener thread (realtime channel) --------------------------------------
    def start_listener(self, on_push=None, on_reconnect=None) -> None:
        """Start the receive thread if it is not already running. Returns at once."""
        self._on_push = on_push or self._on_push
        self._on_reconnect = on_reconnect or self._on_reconnect
        if self._listener and self._listener.is_alive():
            return
        self._stopping.clear()
        self.auth_error = None
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
                s = self._sock
                if s is None:
                    self.connect()
                    s = self._sock
                    if reconnects:
                        _LOGGER.info("switcher connection re-established")
                    reconnects = 0
                    if self._on_reconnect:
                        try:
                            self._on_reconnect()
                        except Exception:  # noqa: BLE001
                            _LOGGER.exception("on_reconnect callback failed")
                if s is None:               # closed by request() meanwhile
                    continue
                s.settimeout(HEARTBEAT_INTERVAL)
                p = parse_packet(self._recv_packet())
                self._dispatch(p)
            except socket.timeout:
                with self._send_lock, contextlib.suppress(OSError):
                    self._raw_send(0x1104, b"")       # heartbeat on idle
            except SwitcherAuthError as err:
                # credentials rejected: stop retrying, surface to request()
                self.auth_error = err
                self.close()
                self._fail_pending(err)
                break
            except Exception as err:  # noqa: BLE001
                if self._stopping.is_set():
                    break
                self.close()
                reconnects += 1
                delay = min(3 * reconnects, 30)
                # Log a warning once, then debug. An overnight outage should not fill the log.
                log = _LOGGER.warning if reconnects == 1 else _LOGGER.debug
                log("switcher connection lost (%s); retry in %ss", err, delay)
                if self._stopping.wait(delay):
                    break

    def _fail_pending(self, err: Exception) -> None:
        for q in list(self._pending.values()):
            with contextlib.suppress(queue.Full):
                q.put_nowait(err)

    def _dispatch(self, p: dict) -> None:
        if p["cmd"] == PUSH_CMD:
            push = parse_push(p["body"])
            # A 0x0E02 param read answers through this push. Hand it to the
            # request waiting on that serial and stop: it must NOT reach the
            # push callback, or every read would schedule another refresh and
            # the integration would poll itself forever.
            if push and push["cmd"] == 0x0E02:
                q = self._pending.get(push["serial"])
                if q is not None:
                    with contextlib.suppress(queue.Full):
                        q.put_nowait(push)
                return
            if push and self._on_push:
                try:
                    self._on_push(push)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("push callback failed")
            return                                     # other pushes never resolve requests
        q = self._pending.get(p["serial"])             # replies matched by serial
        if q is not None:
            with contextlib.suppress(queue.Full):
                q.put_nowait(p)

    # -- request bridge ----------------------------------------------------------
    def request(self, cmd: int, body: bytes, *, encrypted: bool = True,
                version: bytes = b"\x03\x05", control: int = 0,
                relation_id: bytes | None = None,
                timeout: float = 15) -> dict:
        """Send one packet on the persistent connection and wait for its own reply
        (matched by serial). Reconnects on transport failure; raises
        SwitcherAuthError if the listener hit a credential rejection."""
        if self._listener is None or not self._listener.is_alive():
            if self.auth_error:
                raise self.auth_error
            self.start_listener()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.auth_error:
                raise self.auth_error
            if self._sock is None:      # listener still connecting
                time.sleep(0.2)
                continue
            q: queue.Queue = queue.Queue(maxsize=2)
            try:
                with self._send_lock:
                    serial = self._raw_send(cmd, body, encrypted=encrypted,
                                            version=version, control=control,
                                            relation_id=relation_id)
                    self._pending[serial] = q
                try:
                    r = q.get(timeout=max(1, deadline - time.time()))
                finally:
                    self._pending.pop(serial, None)
                if isinstance(r, Exception):
                    raise r
                return r
            except SwitcherAuthError:
                raise
            except queue.Empty:
                # no reply in time: don't tear down the shared socket for everyone;
                # the listener's heartbeat decides if the connection is really dead
                raise SwitcherApiError(f"timeout waiting for 0x{cmd:04x}") from None
            except OSError as err:
                self.close()
                _LOGGER.debug("request 0x%04x failed (%s); reconnecting", cmd, err)
                time.sleep(1)
        raise SwitcherApiError(f"timeout waiting for 0x{cmd:04x}")

    # -- high-level API ----------------------------------------------------------
    def get_devices(self) -> list[dict[str, Any]]:
        p = self.request(0x1343, b"", encrypted=True)
        if p["errcode"] != 0:
            raise SwitcherApiError(f"device list errcode={p['errcode']}")
        # The 0x1343 reply body is plain JSON (seen in a live capture). Only the
        # 0x1102 login reply is AES-encrypted.
        body = p["body"]
        i = body.find(b"{")
        if i < 0:
            return []
        data = json.loads(body[i:].decode("utf-8", "replace"))
        return data.get("DevList") or data.get("devList") or []

    def poll(self) -> dict[str, dict[str, Any]]:
        """One executor job: device list + per-device state + decrypted pwd,
        plus the auto-shutdown limit for boilers (0x0E02 read, like the app).
        A malformed row is logged once and skipped, never kills the poll."""
        from .devices import PARAM_AUTO_SHUTDOWN, Family, body_read_params
        out: dict[str, dict[str, Any]] = {}
        for dev in self.get_devices():
            try:
                st = self.device_state(dev)
            except Exception as err:  # noqa: BLE001
                dt = dev.get("DeviceType") if isinstance(dev, dict) else "?"
                if dt not in self._warned_types:
                    self._warned_types.add(dt)
                    _LOGGER.warning("skipping device type %s: %s", dt, err)
                continue
            try:
                st["device_pwd"] = self.device_password(dev)
            except Exception:  # noqa: BLE001 - pwd decrypt must not kill the poll
                st["device_pwd"] = None
            # S216 keeps its auto-shutdown limit in parameter 0x0C14 (capture-verified).
            # The other boilers carry it inside WorkStatus (Java autoclosing / delay,
            # aioswitcher auto_shutdown), so no extra round trip for them.
            # The 0x0C14 read is cached: it only changes when someone writes it,
            # and the confirm loop after a command polls at 2 Hz.
            if Family(st["family"]) is Family.ON_WALL and st["device_pwd"] and st.get("online"):
                key = st["did_int"]
                cached = self._auto_shutdown_cache.get(key)
                if cached and time.time() - cached[1] < AUTO_SHUTDOWN_TTL:
                    st["auto_shutdown_s"] = cached[0]
                else:
                    try:
                        vals = self.read_params(body_read_params(
                            key, self.uid, _b_c_encrypt(st["device_pwd"]),
                            [PARAM_AUTO_SHUTDOWN]))
                        raw = vals.get(PARAM_AUTO_SHUTDOWN)
                        if raw and len(raw) >= 4:
                            v = struct.unpack_from("<i", raw, 0)[0]
                            st["auto_shutdown_s"] = v
                            self._auto_shutdown_cache[key] = (v, time.time())
                    except Exception as err:  # noqa: BLE001 - optional field
                        _LOGGER.debug("auto-shutdown read failed: %s", err)
                        if cached:                      # carry the last good value
                            st["auto_shutdown_s"] = cached[0]
            elif st.get("boiler") and st.get("autoclosing") is not None:
                st["auto_shutdown_s"] = st["autoclosing"]
            out[self.state_key(dev)] = st
        return out

    def device_password(self, device: dict[str, Any]) -> str:
        return _b_a_decrypt(base64.b64decode(device["VisitPwd"]), self.comm_key)

    def send_control(self, body: bytes) -> None:
        """Send one pre-built 0x0201 control body (plaintext; pwd block inside
        is already native-encrypted) and require errcode 0."""
        p = self.request(0x0201, body, encrypted=False)
        if p["errcode"] != 0:
            raise SwitcherApiError(f"control errcode={p['errcode']}")

    def send_control_single(self, did: int, body: bytes) -> None:
        """Single-line plug/socket cloud path (OgeSPlugModel.java:145-173,
        common/a.java:142-158, ei/a.java:109-137): the whole 49 B body is
        AES-ECB'd with key2_50 (= BODY_KEY), control=0x0100, relation=DID."""
        p = self.request(0x0201, body, encrypted=True, control=0x0100,
                         relation_id=struct.pack("<I", did).ljust(6, b"\x00"))
        if p["errcode"] != 0:
            raise SwitcherApiError(f"control errcode={p['errcode']}")

    def send_write_params(self, body: bytes) -> None:
        """Parameter write, businessCode 0x0E01 (ei/b.z, OgeCommonDeviceModel.getWriteParamsCMD).
        Same framing as 0x0201: plaintext body, pwd block pre-encrypted."""
        p = self.request(0x0E01, body, encrypted=False)
        if p["errcode"] != 0:
            raise SwitcherApiError(f"write params errcode={p['errcode']}")
        self._auto_shutdown_cache.clear()      # the next poll re-reads the new value

    def read_params(self, body: bytes, timeout: float = 10) -> dict[int, bytes]:
        """Parameter read, businessCode 0x0E02. The direct reply is an empty ack;
        the values arrive in a 0x0103 push whose inner cmd is 0x0E02 and whose
        inner serial equals the serial we sent (capture-verified 2026-10-10).
        Both land on the same per-serial queue, in either order."""
        from .devices import parse_params_payload
        if self.auth_error:
            raise self.auth_error
        if self._listener is None or not self._listener.is_alive():
            self.start_listener()
        deadline = time.time() + timeout
        while self._sock is None and time.time() < deadline:
            time.sleep(0.2)
        q: queue.Queue = queue.Queue(maxsize=2)
        with self._send_lock:
            serial = self._raw_send(0x0E02, body, encrypted=False)
            self._pending[serial] = q
        try:
            while True:
                msg = q.get(timeout=max(1, deadline - time.time()))
                if isinstance(msg, Exception):
                    raise msg
                if "payload" in msg:                   # the push carrying the values
                    return parse_params_payload(msg["payload"])
                if msg.get("errcode", 0) != 0:         # the ack, if it failed
                    raise SwitcherApiError(f"read params errcode={msg['errcode']}")
        except queue.Empty:
            raise SwitcherApiError("timeout waiting for 0x0E02 push") from None
        finally:
            self._pending.pop(serial, None)

    def pwd_block(self, device_pwd: str) -> bytes:
        return _b_c_encrypt(device_pwd)


    @staticmethod
    def state_key(device: dict[str, Any]) -> str:
        """Coordinator key. Mix panels (main 15) emit one 0x1343 row per line
        that share one DID (DataParser0x1343.java:62-70), so the key is DID plus line."""
        did = str(device.get("DID"))
        if str(device.get("DeviceType", ""))[:2].upper() == "0F":
            return f"{did}_L{int(device.get('LineNo') or 0)}"
        return did

    @staticmethod
    def device_state(device: dict[str, Any]) -> dict:
        from .devices import PARSERS, classify, is_boiler, product_name
        fam, main, sub = classify(str(device.get("DeviceType", "")))
        line_no = int(device.get("LineNo") or 0)
        line_type = int(device.get("LineType") or 0)
        ws = base64.b64decode(device.get("WorkStatus") or "")
        parser = PARSERS.get(fam)
        st = parser(ws, line_no, line_type).as_dict() if parser else {}
        st["family"] = fam.value
        st["main_type"] = main
        st["sub_type"] = sub
        st["device_type"] = str(device.get("DeviceType", ""))
        st["did_int"] = int(device.get("DID") or 0)
        st["line_no"] = line_no
        st["line_type"] = line_type
        st["name"] = device.get("DeviceName")
        st["did"] = device.get("DID")
        st["online"] = device.get("OnlineStatus") == 1
        st["dmac"] = device.get("DMAC")
        st["firmware"] = device.get("FirmwareVersion")
        st["product_features"] = device.get("ProductFeatures")
        st["model"] = product_name(st["device_type"], fam)
        st["boiler"] = is_boiler(st["device_type"])
        st["workstatus_hex"] = ws.hex()
        return st
