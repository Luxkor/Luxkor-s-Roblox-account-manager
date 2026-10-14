import asyncio
import json
import threading
import time
import unittest
import urllib.parse

from helpers import COOKIE, JsonHandler, make_manager, rm, serve


class Fake(JsonHandler):
    polls = 0

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/v1/users/authenticated":
            if COOKIE in self.headers.get("Cookie", ""):
                return self.send_json({"id": 42, "name": "Tester", "displayName": "T"})
            return self.send_json({}, 401)
        if u.path == "/explore-api/v1/get-sorts":
            return self.send_json({"sorts": [{"sortId": "top", "sortDisplayName": "Populares", "contentType": "Games"},
                                             {"sortId": "f", "sortDisplayName": "Filtros", "contentType": "Filters"}]})
        if u.path == "/explore-api/v1/get-sort-content":
            return self.send_json({"games": [{"universeId": 1, "name": "Adopt Me", "playerCount": 5, "rootPlaceId": 920},
                                             {"universeId": 2, "name": "Sin place", "playerCount": 1}]})
        if u.path == "/search-api/omni-search":
            return self.send_json({"searchResults": [{"contentGroupType": "Game", "contents": [
                {"contentType": "Game", "universeId": 3, "name": "Mega Obby", "rootPlaceId": 33},
                {"contentType": "Game", "universeId": 4, "name": "Obby", "rootPlaceId": 44}]}]})
        if u.path == "/v1/games":
            return self.send_json({"data": [{"id": int(i), "name": "D%s" % i, "rootPlaceId": 1000 + int(i), "playing": 7}
                                            for i in q["universeIds"][0].split(",")]})
        if u.path == "/v1/games/icons":
            return self.send_json({"data": []})
        self.send_json({}, 404)

    def do_POST(self):
        body = self.read_json()
        if "X-Csrf-Token" not in self.headers:
            return self.send_json({}, 403, [("x-csrf-token", "tok")])
        if self.path == "/auth-token-service/v1/login/create":
            return self.send_json({"code": "ABC123", "privateKey": "PK", "status": "Created"})
        if self.path == "/auth-token-service/v1/login/status":
            Fake.polls += 1
            return self.send_json({"status": "Validated" if Fake.polls >= 2 else "Created"})
        if self.path == "/v2/login":
            assert body == {"ctype": "AuthToken", "cvalue": "ABC123", "password": "PK"}, body
            return self.send_json({}, 200, [("Set-Cookie", ".ROBLOSECURITY=%s; path=/; secure" % COOKIE)])
        self.send_json({})


class Core(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = serve(Fake)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def wait(self, mgr):
        end = time.time() + 20
        while time.time() < end:
            s = mgr.login_state()
            if s["state"] != "waiting":
                return s
            time.sleep(0.2)
        self.fail("timeout")

    def test_normalize_cookie(self):
        self.assertEqual(rm.normalize_cookie("usuario:clave:" + COOKIE), COOKIE)
        self.assertNotIn("clave", rm.normalize_cookie("usuario:clave:" + COOKIE))
        self.assertEqual(rm.normalize_cookie(COOKIE + ";"), COOKIE)
        with self.assertRaises(ValueError):
            rm.normalize_cookie("corta")
        with self.assertRaises(ValueError):
            rm.normalize_cookie(COOKIE + " extra")

    def test_add_accounts_validates_before_saving(self):
        mgr = make_manager(self.base)
        res = mgr.add_accounts(COOKIE + "\n" + COOKIE.replace("AAA", "BBB"))
        self.assertTrue(res[0]["ok"])
        self.assertFalse(res[1]["ok"])  # cookie que Roblox no reconoce: no se guarda
        self.assertEqual(len(mgr.store.accounts()), 1)

    def test_quick_login(self):
        Fake.polls = 0
        mgr = make_manager(self.base)
        mgr.start_login("quick")
        s = self.wait(mgr)
        self.assertEqual((s["state"], s["username"]), ("ok", "Tester"))
        self.assertEqual(mgr.store.cookie(mgr.store.accounts()[0]["id"]), COOKIE)

    def test_cancel_login(self):
        Fake.polls = -1000
        mgr = make_manager(self.base)
        mgr.start_login("quick")
        time.sleep(0.5)
        mgr.cancel_login()
        self.assertEqual(mgr.login_state()["state"], "cancelled")

    def test_browser_login_with_fake_cdp(self):
        import websockets
        calls = []

        async def cdp(ws):
            async for raw in ws:
                m = json.loads(raw)
                calls.append(m["method"])
                cookies = [{"name": "x", "value": "1", "domain": ".roblox.com"}]
                if calls.count("Storage.getCookies") >= 3:
                    cookies.append({"name": ".ROBLOSECURITY", "value": COOKIE, "domain": ".roblox.com"})
                await ws.send(json.dumps({"id": m["id"], "result": {"cookies": cookies} if "Cookies" in m["method"] else {}}))

        loop, ready, port = asyncio.new_event_loop(), threading.Event(), []

        def run():
            asyncio.set_event_loop(loop)

            async def main():
                async with websockets.serve(cdp, "127.0.0.1", 0) as s:
                    port.append(s.sockets[0].getsockname()[1])
                    ready.set()
                    await asyncio.Future()
            try:
                loop.run_until_complete(main())
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()
        ready.wait()

        class FakeProc:
            def poll(self):
                return 0

            def terminate(self):
                pass
        mgr = make_manager(self.base)
        mgr._wait_cdp = staticmethod(lambda p, j, timeout=25: "ws://127.0.0.1:%d/devtools/browser/x" % port[0])
        old_find, old_popen = rm.find_app_browser, rm.subprocess.Popen
        rm.find_app_browser = lambda: "/bin/true"
        rm.subprocess.Popen = lambda *a, **k: FakeProc()
        try:
            mgr.start_login("browser")
            s = self.wait(mgr)
        finally:
            rm.find_app_browser, rm.subprocess.Popen = old_find, old_popen
        self.assertEqual(s["state"], "ok")
        self.assertIn("Browser.close", calls)

    def test_games(self):
        mgr = make_manager(self.base)
        self.assertEqual([s["id"] for s in mgr.game_sorts()["sorts"]], ["x:top"])
        games = mgr.games_list("x:top")["games"]
        self.assertEqual([g["place_id"] for g in games], [920, 1002])  # el que no traía place se completa
        found = mgr.games_search("obby")["games"]
        self.assertEqual(found[0]["name"], "Obby")  # coincidencia exacta primero
        with self.assertRaises(ValueError):
            mgr.games_search("a")


if __name__ == "__main__":
    unittest.main()
