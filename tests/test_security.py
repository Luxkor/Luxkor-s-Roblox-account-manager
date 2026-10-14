import io
import json
import logging
import os
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler

from helpers import COOKIE, JsonHandler, make_manager, rm, serve


class Sink(BaseHTTPRequestHandler):
    """Servidor "malicioso": apunta lo que recibe."""
    got = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Sink.got.append(dict(self.headers))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_GET


class Redirector(JsonHandler):
    target = ""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", Redirector.target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_GET


class Revoker(JsonHandler):
    calls = []

    def do_GET(self):
        self.send_json({"id": 42, "name": "Tester", "displayName": "T"})

    def do_POST(self):
        if "X-Csrf-Token" not in self.headers:
            return self.send_json({}, 403, [("x-csrf-token", "tok")])
        Revoker.calls.append((self.path, self.headers.get("Cookie", "")))
        self.send_json({})


class Security(unittest.TestCase):
    def test_redirects_never_forward_the_cookie(self):
        sink, sink_base = serve(Sink)
        red, red_base = serve(Redirector)
        Redirector.target = sink_base + "/robado"
        Sink.got.clear()
        api = rm.RobloxAPI(users_base=red_base, strict=False)
        with self.assertRaises(rm.RobloxError):
            api.whoami(COOKIE)  # 302 -> "respuesta inesperada", sin seguir la redirección
        self.assertEqual(Sink.got, [])
        sink.shutdown()
        red.shutdown()

    def test_cookie_only_goes_to_roblox_over_https(self):
        api = rm.RobloxAPI(users_base="http://127.0.0.1:9")
        with self.assertRaises(rm.RobloxError) as ctx:
            api.whoami(COOKIE)
        self.assertIn("solo se envía", str(ctx.exception))
        api = rm.RobloxAPI(users_base="https://evil.example")
        with self.assertRaises(rm.RobloxError):
            api.whoami(COOKIE)

    def test_host_checks(self):
        ok = ["https://users.roblox.com/x", "https://roblox.com/", "https://a.b.roblox.com/"]
        bad = ["http://users.roblox.com/", "https://evilroblox.com/", "https://roblox.com.evil.com/",
               "https://roblox.com@evil.com/", "ftp://roblox.com/"]
        for u in ok:
            self.assertTrue(rm.cookie_host_ok(u), u)
        for u in bad:
            self.assertFalse(rm.cookie_host_ok(u), u)
        self.assertTrue(rm.roblox_domain(".roblox.com"))
        self.assertFalse(rm.roblox_domain("notroblox.com"))
        self.assertFalse(rm.roblox_domain("roblox.com.evil.com"))

    def test_image_host_allowlist(self):
        self.assertTrue(rm.roblox_img_ok("https://tr.rbxcdn.com/a.png"))
        for u in ["http://tr.rbxcdn.com/a.png", "https://evilrbxcdn.com/a.png", "https://tr.rbxcdn.com.evil.com/a.png",
                  "https://example.com/a.png"]:
            self.assertFalse(rm.roblox_img_ok(u), u)

    def test_logs_never_contain_cookies(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.addFilter(rm.RedactFilter())
        logger = logging.getLogger("prueba-redact")
        logger.propagate = False
        logger.addHandler(handler)
        logger.warning("fallo con %s", COOKIE)
        logger.warning("cabecera Cookie: .ROBLOSECURITY=%s; otra=1", "X" * 50)
        try:
            raise ValueError("dentro: " + COOKIE)
        except ValueError:
            logger.exception("excepción")
        out = stream.getvalue()
        self.assertNotIn("AAAAAAAAAA", out)
        self.assertNotIn("XXXXXXXXXX", out)
        self.assertIn("[oculto]", out)

    def test_state_never_exposes_cookies(self):
        class H(JsonHandler):
            def do_GET(self):
                self.send_json({"id": 5, "name": "N", "displayName": "N"})
        srv, base = serve(H)
        mgr = make_manager(base)
        mgr.add_accounts(COOKIE)
        self.assertEqual(len(mgr.store.accounts()), 1)
        self.assertNotIn("AAAAAAAAAA", json.dumps(mgr.state()))
        srv.shutdown()

    def test_launch_uri_rejects_odd_tickets(self):
        for bad in ["", "a b", "a+b", "a\nb"]:
            with self.assertRaises(rm.RobloxError):
                rm.build_launch_uri(bad, 1818)
        self.assertIn("gameinfo:TICKET123", rm.build_launch_uri("TICKET123", 1818))

    def test_place_and_job_parsing_blocks_injection(self):
        self.assertIsNone(rm.parse_place("1818; calc.exe"))
        self.assertIsNone(rm.parse_place("roblox-player:1+x"))
        self.assertEqual(rm.parse_place("https://www.roblox.com/games/1818/Nombre"), 1818)
        self.assertFalse(rm.valid_job("x; calc"))
        self.assertTrue(rm.valid_job("123e4567-e89b-12d3-a456-426614174000"))

    def test_delete_with_revoke_logs_out_on_roblox(self):
        srv, base = serve(Revoker)
        Revoker.calls.clear()
        mgr = make_manager(base)
        mgr.add_accounts(COOKIE)
        aid = mgr.store.accounts()[0]["id"]
        res = mgr.delete([aid], revoke=True)
        self.assertEqual(res, {"revoked": 1, "failed": 0})
        self.assertEqual(Revoker.calls[0][0], "/v2/logout")
        self.assertIn(COOKIE, Revoker.calls[0][1])
        self.assertEqual(mgr.store.accounts(), [])
        # sin revocar: solo se borra
        mgr.add_accounts(COOKIE)
        Revoker.calls.clear()
        mgr.delete([mgr.store.accounts()[0]["id"]])
        self.assertEqual(Revoker.calls, [])
        srv.shutdown()

    def test_image_bomb_and_redirect_are_rejected(self):
        from PIL import Image

        class Img(JsonHandler):
            def do_GET(self):
                if self.path == "/redir":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:9/x")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                buf = io.BytesIO()
                Image.new("L", (3000, 3000)).save(buf, "PNG")
                self.send_response(200)
                self.send_header("Content-Length", str(len(buf.getvalue())))
                self.end_headers()
                self.wfile.write(buf.getvalue())
        srv, base = serve(Img)
        rm.IMG_OK = lambda u: True
        try:
            with self.assertRaises(rm.RobloxError):
                rm.fetch_image(base + "/grande.png")
            with self.assertRaises(Exception):
                rm.fetch_image(base + "/redir")
        finally:
            rm.IMG_OK = rm.roblox_img_ok
            srv.shutdown()

    def test_sweep_removes_only_old_login_profiles(self):
        base = tempfile.gettempdir()
        old = tempfile.mkdtemp(prefix="rm-login-", dir=base)
        new = tempfile.mkdtemp(prefix="rm-login-", dir=base)
        other = tempfile.mkdtemp(prefix="otra-cosa-", dir=base)
        past = time.time() - 7200
        os.utime(old, (past, past))
        os.utime(other, (past, past))
        rm.sweep_login_profiles()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        self.assertTrue(os.path.exists(other))
        os.rmdir(new)
        os.rmdir(other)

    @unittest.skipIf(rm.IS_WINDOWS, "en Windows siempre se usa DPAPI")
    def test_plaintext_dev_mode_is_off_in_frozen_builds(self):
        import sys
        sys.frozen = True
        try:
            with self.assertRaises(rm.SecretError):
                rm.protect_secret("x")
        finally:
            del sys.frozen


if __name__ == "__main__":
    unittest.main()
