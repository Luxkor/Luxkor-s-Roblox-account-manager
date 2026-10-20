"""Pruebas de velocidad del lanzamiento: conexiones reutilizadas, token CSRF guardado y precalentamiento."""
import hashlib
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import COOKIE, rm
from test_parallel import FakeApi, FakePlat, make


JOB = "123e4567-e89b-12d3-a456-426614174000"


class Roblox(BaseHTTPRequestHandler):
    """Servidor falso de auth.roblox.com con HTTP/1.1 (conexiones persistentes) y token CSRF."""
    protocol_version = "HTTP/1.1"
    stats = {}
    csrf = "token-bueno"
    close_after_reply = False  # simula un servidor que corta conexiones "persistentes" sin avisar

    def log_message(self, *a):
        pass

    def setup(self):
        Roblox.stats["conns"] += 1
        super().setup()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Roblox.stats["reqs"] += 1
        sent = self.headers.get("X-CSRF-TOKEN")
        if sent != Roblox.csrf:
            code, extra = 403, [("x-csrf-token", Roblox.csrf)]
        else:
            Roblox.stats["tickets"] += 1
            code, extra = 200, [("rbx-authentication-ticket", "TICKET-%d" % Roblox.stats["tickets"])]
        self.send_response(code)
        self.send_header("Content-Length", "2")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b"{}")
        if Roblox.close_after_reply:
            self.close_connection = True


def start():
    Roblox.stats = {"conns": 0, "reqs": 0, "tickets": 0}
    Roblox.csrf, Roblox.close_after_reply = "token-bueno", False
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Roblox)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, rm.RobloxAPI(auth_base="http://127.0.0.1:%d" % srv.server_address[1], strict=False)


class Network(unittest.TestCase):
    def test_second_ticket_needs_one_request_and_no_new_connection(self):
        srv, api = start()
        try:
            self.assertEqual(api.auth_ticket(COOKIE, 1818), "TICKET-1")
            self.assertEqual(Roblox.stats, {"conns": 1, "reqs": 2, "tickets": 1})  # 403 + 200, una sola conexión
            self.assertEqual(api.auth_ticket(COOKIE, 1818), "TICKET-2")
            self.assertEqual(Roblox.stats, {"conns": 1, "reqs": 3, "tickets": 2})  # token guardado: 1 petición
        finally:
            srv.shutdown()

    def test_warm_gets_the_token_without_minting_a_ticket(self):
        srv, api = start()
        try:
            api.warm(COOKIE)
            self.assertEqual(Roblox.stats["tickets"], 0)  # el adelanto nunca crea tickets
            self.assertEqual(api.auth_ticket(COOKIE, 1818), "TICKET-1")
            self.assertEqual(Roblox.stats, {"conns": 1, "reqs": 2, "tickets": 1})  # 1 de aviso + 1 del ticket
        finally:
            srv.shutdown()

    def test_expired_token_is_renewed_automatically(self):
        srv, api = start()
        try:
            api.warm(COOKIE)
            Roblox.csrf = "token-nuevo"  # Roblox rota el token: el guardado deja de valer
            self.assertTrue(api.auth_ticket(COOKIE, 1818).startswith("TICKET-"))
        finally:
            srv.shutdown()

    def test_tokens_are_per_account(self):
        srv, api = start()
        try:
            api.warm(COOKIE)
            other = COOKIE + "otra"
            self.assertEqual(api._csrf_get(api._csrf_key(other)), None)
            self.assertEqual(api._csrf_key(COOKIE), hashlib.sha256(COOKIE.encode()).hexdigest())
        finally:
            srv.shutdown()

    def test_a_connection_closed_by_the_server_is_replaced_transparently(self):
        srv, api = start()
        try:
            Roblox.close_after_reply = True
            api.auth_ticket(COOKIE, 1818)
            self.assertEqual(api.auth_ticket(COOKIE, 1818), "TICKET-2")  # la conexión guardada ya estaba muerta
            self.assertGreaterEqual(Roblox.stats["conns"], 2)
        finally:
            srv.shutdown()

    def test_a_silent_server_fails_fast_with_a_clear_error(self):
        silent = ThreadingHTTPServer(("127.0.0.1", 0), type("Mudo", (BaseHTTPRequestHandler,), {
            "log_message": lambda *a: None, "do_POST": lambda self: time.sleep(3)}))
        threading.Thread(target=silent.serve_forever, daemon=True).start()
        try:
            api = rm.RobloxAPI(auth_base="http://127.0.0.1:%d" % silent.server_address[1], strict=False, timeout=0.4)
            t0 = time.time()
            with self.assertRaises(rm.RobloxError):
                api.auth_ticket(COOKIE, 1818)
            self.assertLess(time.time() - t0, 1.5)  # una espera, no dos (sin reintentar por otro camino)
        finally:
            silent.shutdown()


class Counting(FakeApi):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.warmed = []

    def warm(self, cookie):
        self.warmed.append(cookie)


class Launching(unittest.TestCase):
    def test_prewarm_runs_once_per_account_in_a_while(self):
        api = Counting(delay=0)
        mgr, ids = make(api, FakePlat(), n=3)
        mgr.prewarm(ids[:2])
        mgr.prewarm(ids)  # las dos primeras ya están calientes: solo falta la tercera
        mgr.prewarm([])
        end = time.time() + 3
        while len(api.warmed) < 3 and time.time() < end:
            time.sleep(0.02)
        time.sleep(0.2)
        self.assertEqual(sorted(api.warmed), sorted(mgr.store.cookie(i) for i in ids))
        self.assertEqual(len(api.warmed), 3)

    def test_a_failing_prewarm_never_breaks_anything(self):
        class Broken(FakeApi):
            def warm(self, cookie):
                raise RuntimeError("fallo cualquiera")
        mgr, ids = make(Broken(delay=0), FakePlat(), n=1)
        mgr.prewarm(ids)
        time.sleep(0.3)
        mgr.launch(ids, "1818", "", 0, parallel=True)  # y el lanzamiento funciona igual
        mgr.worker.join(10)
        self.assertEqual(mgr.status[ids[0]]["text"], "Abierta")

    def test_each_account_launches_as_soon_as_its_own_ticket_is_ready(self):
        class Slow(FakePlat):
            def launch(self, uri):
                time.sleep(0.15)
                super().launch(uri)

        class Varied(FakeApi):
            def auth_ticket(self, cookie, place):  # la última cuenta tarda mucho más en dar su ticket
                time.sleep(0.9 if cookie.endswith("0003") else 0.05)
                return "T" + cookie[-4:]
        mgr, ids = make(Varied(), Slow(), n=4)
        t0, stamps = time.time(), {}
        orig = mgr.plat.launch

        def spy(uri):
            orig(uri)
            stamps[len(stamps)] = time.time() - t0
        mgr.plat.launch = spy
        mgr.launch(ids, "1818", "", 0, parallel=True)
        mgr.worker.join(15)
        # las tres rápidas salen ya (no esperan a la lenta): ~0,05 + 3 x 0,15 s, mucho antes de 0,9 s
        self.assertLess(stamps[2], 0.75)
        self.assertEqual(len(stamps), 4)

    def test_settings_are_saved_without_delaying_the_launch(self):
        mgr, ids = make(FakeApi(delay=0), FakePlat(), n=1)
        orig = mgr.store.update_settings
        mgr.store.update_settings = lambda **kw: (time.sleep(0.6), orig(**kw))[1]  # disco muy lento
        t0 = time.time()
        mgr.launch(ids, "1818", JOB, 0, parallel=True)
        while not mgr.plat.uris and time.time() - t0 < 3:
            time.sleep(0.01)
        self.assertLess(time.time() - t0, 0.4)  # la ventana se lanza sin esperar a la escritura
        mgr.worker.join(10)
        self.assertEqual(mgr.store.settings()["job"], JOB)  # y los ajustes se guardan igualmente


@unittest.skipUnless(rm.IS_WINDOWS, "solo en Windows")
class NativeProcesses(unittest.TestCase):
    def test_toolhelp_finds_this_process(self):
        self.assertIn(os.getpid(), rm.toolhelp_pids(os.path.basename(sys.executable)))
        self.assertEqual(rm.toolhelp_pids("no-existe-xyz.exe"), [])

    def test_the_platform_uses_it(self):
        plat = rm.WindowsPlatform()
        saved = rm.PLAYER_EXE
        rm.PLAYER_EXE = os.path.basename(sys.executable)
        try:
            self.assertIn(os.getpid(), plat.list_pids())
        finally:
            rm.PLAYER_EXE = saved


if __name__ == "__main__":
    unittest.main()
