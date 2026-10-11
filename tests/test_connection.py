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
