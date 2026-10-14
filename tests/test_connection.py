"""Connection resilience: a fake cloud on localhost that logs the client in,
answers one device-list request, then goes silent (session dead, TCP open).
The client must notice on its own and reconnect, and the new session must
answer requests again. Mirrors the overnight failure of 2026-10-11."""
import socket
import struct
import threading
import time

from test_devices import api  # noqa: E402  (shared loader)

LOGIN_OK = struct.pack("<ii", 0x1234, 42) + b"\x00" * 32


class FakeCloud(threading.Thread):
    def __init__(self, dead_after_replies: int = 1):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.dead_after = dead_after_replies
        self.logins = 0
        self.business_before_login = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                self.srv.settimeout(0.5)
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        logged_in = False
        replies = 0
        conn.settimeout(10)
        try:
            while not self.stop.is_set():
                hdr = conn.recv(40)
                if not hdr or len(hdr) < 40:
                    return
                total = struct.unpack_from("<H", hdr, 2)[0]
                body = b""
                while len(hdr) + len(body) < total:
                    body += conn.recv(total - 40 - len(body))
                p = api.parse_packet(hdr + body)
                if p["cmd"] == 0x1102:
                    with self.lock:
                        self.logins += 1
                    logged_in = True
                    conn.sendall(api.build_packet(0x1102, api._aes_enc(LOGIN_OK), p["serial"], 0x1234))
                    continue
                if not logged_in:
                    with self.lock:
                        self.business_before_login += 1
                    return                                   # the real server kicks you
                if p["cmd"] == 0x1104:                       # heartbeat: answered while alive
                    if replies < self.dead_after:
                        conn.sendall(api.build_packet(0x1104, b"", p["serial"], 0x1234))
                    continue
                if replies < self.dead_after:
                    replies += 1
                    conn.sendall(api.build_packet(p["cmd"], b'{"DevList":[]}', p["serial"], 0x1234))
                # else: dead session, say nothing, keep TCP open
        except OSError:
            return


def _client(port) -> "api.SwitcherClient":
    api.HOST, api.PORT = "127.0.0.1", port
    c = api.SwitcherClient("user@example.com", "secret")
    return c


def test_request_never_sent_before_login():
    srv = FakeCloud(dead_after_replies=99); srv.start()
    c = _client(srv.port)
    try:
        # several concurrent requests racing the very first connect
        results = []
        def go():
            try:
                results.append(c.get_devices())
            except Exception as e:  # noqa: BLE001
                results.append(e)
        ts = [threading.Thread(target=go) for _ in range(4)]
        for t in ts: t.start()
        for t in ts: t.join(timeout=20)
        assert all(r == [] for r in results), results
        assert srv.business_before_login == 0
        assert srv.logins == 1
    finally:
        c.stop_listener(); srv.stop.set()


def test_dead_session_is_detected_and_reconnected(monkeypatch):
    monkeypatch.setattr(api, "DEAD_AFTER", 3)
    monkeypatch.setattr(api, "HEARTBEAT_INTERVAL", 1)
    srv = FakeCloud(dead_after_replies=1); srv.start()
    c = _client(srv.port)
    try:
        assert c.get_devices() == []            # first session works once
        # second request: the session is dead, request times out quickly
        try:
            c.request(0x1343, b"", timeout=2)
        except api.SwitcherApiError:
            pass
        # the listener must notice the silence and re-login on its own
        deadline = time.time() + 15
        while time.time() < deadline and srv.logins < 2:
            time.sleep(0.2)
        assert srv.logins >= 2, "client never reconnected after the session went silent"
        assert c.get_devices() == []            # and the new session answers
        assert c.stats["reconnects"] >= 1 and c.stats["last_error"]
    finally:
        c.stop_listener(); srv.stop.set()


def test_repeated_timeouts_force_reconnect(monkeypatch):
    monkeypatch.setattr(api, "RESET_AFTER_TIMEOUTS", 2)
    monkeypatch.setattr(api, "DEAD_AFTER", 10_000)          # disable the idle detector
    srv = FakeCloud(dead_after_replies=1); srv.start()
    c = _client(srv.port)
    try:
        assert c.get_devices() == []
        for _ in range(2):
            try:
                c.request(0x1343, b"", timeout=1.5)
            except api.SwitcherApiError:
                pass
        deadline = time.time() + 15
        while time.time() < deadline and srv.logins < 2:
            time.sleep(0.2)
        assert srv.logins >= 2, "two timeouts in a row did not trigger a reconnect"
        assert c.get_devices() == []
    finally:
        c.stop_listener(); srv.stop.set()


class RefusingCloud(FakeCloud):
    """Answers the first `refuse` logins with errcode 12, then lets the client in."""

    def __init__(self, refuse: int):
        super().__init__(dead_after_replies=10**6)
        self.refuse = refuse
        self.login_times: list[float] = []

    def _serve(self, conn):
        conn.settimeout(10)
        try:
            hdr = conn.recv(40)
            total = struct.unpack_from("<H", hdr, 2)[0]
            body = b""
            while len(hdr) + len(body) < total:
                body += conn.recv(total - 40 - len(body))
            p = api.parse_packet(hdr + body)
            assert p["cmd"] == 0x1102
            with self.lock:
                self.login_times.append(time.monotonic())
                n = len(self.login_times)
            if n <= self.refuse:
                h = bytearray(api.build_packet(0x1102, b"", p["serial"], 0)[:40])
                h[15] = 12                                   # errcode
                hb = bytes(h)
                conn.sendall(hb + api._crc_trailer(hb, b"\x00" * 32))
                conn.close()
                return
            conn.sendall(api.build_packet(0x1102, api._aes_enc(LOGIN_OK), p["serial"], 0x1234))
            while not self.stop.is_set():                    # accepted: stay connected
                if not conn.recv(4096):
                    return
        except OSError:
            return


def test_refused_login_backs_off_and_recovers(monkeypatch):
    monkeypatch.setattr(api, "LOGIN_BACKOFF_FIRST", 0.1)
    monkeypatch.setattr(api, "LOGIN_BACKOFF_MAX", 0.4)
    cloud = RefusingCloud(refuse=5)
    cloud.start()
    c = _client(cloud.port)
    try:
        c.start_listener()
        deadline = time.time() + 10
        while len(cloud.login_times) < 6 and time.time() < deadline:
            if len(cloud.login_times) >= 2:
                assert c.login_rejected is not None and c.login_rejected[0] == 12
            time.sleep(0.02)
        t = cloud.login_times
        assert len(t) >= 6, t
        gaps = [b - a for a, b in zip(t, t[1:6])]
        # 0.1, 0.2, 0.4, 0.4, 0.4 (+ connect overhead): grows, then capped
        assert gaps[0] < gaps[1] < gaps[2], gaps
        assert 0.35 < gaps[3] < 0.8 and 0.35 < gaps[4] < 0.8, gaps
        assert c._ready.wait(5)                              # sixth login accepted
        assert c.login_rejected is None
    finally:
        c.stop_listener()
        cloud.stop.set()


class StaleIdentityCloud(FakeCloud):
    """The real server's rule, measured 2026-10-14: a login signed with the
    previous session's key is refused with errcode 12. The first connection is
    dropped by the server after one reply, as on the 18:58 outage."""

    def __init__(self):
        super().__init__(dead_after_replies=10**6)
        self.logins_ok = 0
        self.refused = 0
        self.connections = 0

    def _serve(self, conn):
        conn.settimeout(10)
        with self.lock:
            self.connections += 1
            first = self.connections == 1
        try:
            while not self.stop.is_set():
                hdr = conn.recv(40)
                if not hdr or len(hdr) < 40:
                    return
                total = struct.unpack_from("<H", hdr, 2)[0]
                body = b""
                while len(hdr) + len(body) < total:
                    body += conn.recv(total - 40 - len(body))
                pkt = hdr + body
                p = api.parse_packet(pkt)
                if p["cmd"] == 0x1102:
                    # measured: a trailer signed with a previous session's key is
                    # refused (session and uid alone did not matter once the key did)
                    if pkt[-4:] != api._crc_trailer(pkt[:-4], b"\x00" * 32):
                        with self.lock:
                            self.refused += 1
                        h = bytearray(api.build_packet(0x1102, b"", p["serial"], 0)[:40])
                        h[15] = 12
                        conn.sendall(bytes(h) + api._crc_trailer(bytes(h), b"\x00" * 32))
                        return
                    with self.lock:
                        self.logins_ok += 1
                    # a real session key, so a stale one is not all zeros
                    reply = struct.pack("<ii", 0x1234, 42) + b"\x11" * 32
                    conn.sendall(api.build_packet(0x1102, api._aes_enc(reply), p["serial"], 0x1234))
                    continue
                conn.sendall(api.build_packet(p["cmd"], b'{"DevList":[]}', p["serial"], 0x1234))
                if first and p["cmd"] != 0x1104:
                    return                                   # server drops the session
        except OSError:
            return
        finally:
            conn.close()


def test_dropped_connection_relogs_in_with_a_clean_identity(monkeypatch):
    monkeypatch.setattr(api, "RETRY_FIRST", 0.05, raising=False)
    cloud = StaleIdentityCloud()
    cloud.start()
    c = _client(cloud.port)
    try:
        c.start_listener()
        assert c.get_devices() == []                         # first session, then dropped
        deadline = time.time() + 5
        while cloud.logins_ok < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert cloud.refused == 0, "re-login was signed with the old session key"
        assert cloud.logins_ok == 2
        assert c.get_devices() == []                         # usable again, no reload
    finally:
        c.stop_listener()
        cloud.stop.set()
