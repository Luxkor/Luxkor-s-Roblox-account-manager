"""Utilidades comunes de las pruebas: ruta del proyecto y un servidor HTTP falso de Roblox."""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ROBLOX_MANAGER_DEV"] = "1"  # cookies sin cifrar SOLO en pruebas fuera de Windows
os.environ["ROBLOX_MANAGER_HOME"] = tempfile.mkdtemp(prefix="rm-test-")

import roblox_manager as rm  # noqa: E402

COOKIE = ("_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-"
          "ROBUX-and-items.|_") + "A" * 200


def serve(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


class JsonHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200, extra=()):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")


def make_manager(base, **kw):
    api = rm.RobloxAPI(users_base=base, auth_base=base, apis_base=base, thumbs_base=base, games_base=base,
                       strict=False, img_ok=lambda u: False, **kw)
    store = rm.Store(os.path.join(tempfile.mkdtemp(prefix="rm-store-"), "data.json"))
    return rm.Manager(store, api, rm.NullPlatform())
