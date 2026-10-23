#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Luxkor's Account Manager
========================
Gestor personal de cuentas de Roblox con lanzamiento multi-instancia (Windows).

- Aplicación de escritorio (Tkinter + Pillow): ventana propia, sin navegador ni servidor local.
- Formas de añadir cuentas: cookie (también usuario:contraseña:cookie y archivo .txt),
  inicio de sesión en una ventana de Edge/Chrome aislada, y código rápido (Quick Log In).
- Lista de juegos de Roblox integrada: elige un juego y lánzalo con varias cuentas a la vez.
- Las cookies .ROBLOSECURITY se guardan cifradas con DPAPI (ligadas a tu usuario de Windows)
  en %APPDATA%\\RobloxManager\\data.json.

Uso:  pyw roblox_manager.py      (necesita:  pip install pillow)
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import copy
import csv
import hashlib
import http.client
import io
import json
import logging
import logging.handlers
import math
import os
import queue
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, ttk
except ImportError:  # pragma: no cover
    tk = None
try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageGrab, ImageTk
except ImportError:  # pragma: no cover
    Image = ImageGrab = None

APP_NAME = "RobloxManager"
APP_TITLE = "Luxkor's Account Manager"
APP_VERSION = "1.3.1"
IS_WINDOWS = os.name == "nt"
PLAYER_EXE = "RobloxPlayerBeta.exe"
MUTEX_NAME = "ROBLOX_singletonMutex"
EVENT_NAME = "ROBLOX_singletonEvent"
ERROR_ALREADY_EXISTS = 183
CREATE_NO_WINDOW = 0x08000000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

log = logging.getLogger("rm")


# --------------------------------------------------------------------------------------
# Errores
# --------------------------------------------------------------------------------------
class RobloxError(Exception):
    """Fallo hablando con Roblox o lanzando el cliente."""


class AuthExpired(RobloxError):
    """La cookie ya no es valida."""


class SecretError(Exception):
    """No se pudo descifrar una cookie guardada."""


# --------------------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------------------
def data_dir() -> str:
    override = os.environ.get("ROBLOX_MANAGER_HOME")
    if override:
        path = override
    elif IS_WINDOWS:
        path = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
    else:
        path = os.path.join(os.path.expanduser("~"), ".config", APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def sweep_login_profiles(max_age: int = 3600) -> None:
    """Borra perfiles temporales del login por navegador que quedaron si la app se cerró de golpe."""
    base = tempfile.gettempdir()
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        path = os.path.join(base, name)
        try:
            if name.startswith("rm-login-") and os.path.isdir(path) and time.time() - os.path.getmtime(path) > max_age:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


class RedactFilter(logging.Filter):
    """Red de seguridad: si una cookie llegara a un mensaje de log, se tapa antes de escribirla."""
    _RE = re.compile(r"(_\|WARNING[\x21-\x7e]*|\.?ROBLOSECURITY=[^\s;]+)")

    def filter(self, record):
        try:
            record.msg, record.args = self._RE.sub("[oculto]", record.getMessage()), None
            if record.exc_info:
                record.exc_text = self._RE.sub("[oculto]", logging.Formatter().formatException(record.exc_info))
        except Exception:  # noqa: BLE001
            pass
        return True


def setup_logging(directory: str) -> None:
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(directory, "app.log"), maxBytes=256 * 1024, backupCount=1, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(RedactFilter())
    log.setLevel(logging.INFO)
    log.addHandler(handler)


def parse_place(text: str):
    """Acepta un ID, un enlace roblox.com/games/ID/... o roblox://placeId=ID."""
    t = (text or "").strip()
    if re.fullmatch(r"\d{1,20}", t):
        return int(t)
    m = re.search(r"(?:/games/|placeId=)(\d{1,20})", t)
    if m:
        return int(m.group(1))
    return None


_JOB_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def valid_job(job: str) -> bool:
    return bool(_JOB_RE.match(job or ""))


def normalize_cookie(raw: str) -> str:
    c = (raw or "").strip().strip("\"'").strip()
    pos = c.find("_|WARNING")
    if pos > 0:  # formato usuario:contraseña:cookie u otro texto delante de la cookie
        c = c[pos:]
    if c.lower().startswith(".roblosecurity="):
        c = c.split("=", 1)[1]
    c = c.strip().strip("\"'").rstrip(";").strip()
    if not c:
        raise ValueError("La línea está vacía.")
    if not re.fullmatch(r"[\x21-\x7e]+", c):
        raise ValueError("La cookie tiene espacios o caracteres no válidos. Cópiala entera, sin añadir nada más.")
    if len(c) < 100:
        raise ValueError("La cookie es demasiado corta. Pega el valor completo de .ROBLOSECURITY.")
    return c


def parse_tasklist(text: str):
    pids = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[0].strip().lower() == PLAYER_EXE.lower() and row[1].strip().isdigit():
            pids.append(int(row[1]))
    return pids


def build_launch_uri(ticket: str, place_id: int, job_id=None, tracker=None, now_ms=None) -> str:
    """URI roblox-player: igual que la que genera la web de Roblox al pulsar Jugar."""
    if not ticket or re.search(r"[\s+]", ticket):
        raise RobloxError("Roblox devolvió un ticket de acceso con formato inesperado.")
    tracker = tracker or str(random.randint(10_000_000_000, 99_999_999_999))
    if job_id:
        request = (
            "request=RequestGameJob&browserTrackerId=%s&placeId=%d&gameId=%s&isPlayTogetherGame=false"
            % (tracker, place_id, job_id)
        )
    else:
        request = "request=RequestGame&browserTrackerId=%s&placeId=%d&isPlayTogetherGame=false" % (tracker, place_id)
    launcher = urllib.parse.quote("https://assetgame.roblox.com/game/PlaceLauncher.ashx?" + request, safe="")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return (
        "roblox-player:1+launchmode:play+gameinfo:%s+launchtime:%d+placelauncherurl:%s"
        "+browsertrackerid:%s+robloxLocale:en_us+gameLocale:en_us+channel:+LaunchExp:InApp"
        % (ticket, now_ms, launcher, tracker)
    )


# --------------------------------------------------------------------------------------
# Cifrado de cookies (DPAPI en Windows)
# --------------------------------------------------------------------------------------
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.restype = wintypes.BOOL
    second = wintypes.LPCWSTR if protect else ctypes.POINTER(wintypes.LPWSTR)
    fn.argtypes = [
        ctypes.POINTER(BLOB), second, ctypes.POINTER(BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(BLOB),
    ]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buf = ctypes.create_string_buffer(data, len(data))
    src = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI falló (código %d)" % ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect_secret(text: str) -> str:
    raw = text.encode("utf-8")
    if IS_WINDOWS:
        return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode("ascii")
    if os.environ.get("ROBLOX_MANAGER_DEV") == "1" and not getattr(sys, "frozen", False):  # solo pruebas fuera de Windows
        return "dev:" + base64.b64encode(raw).decode("ascii")
    raise SecretError("El cifrado seguro solo está disponible en Windows.")


def unprotect_secret(blob: str) -> str:
    try:
        kind, _, payload = blob.partition(":")
        raw = base64.b64decode(payload)
        if kind == "dpapi" and IS_WINDOWS:
            return _dpapi(raw, False).decode("utf-8")
        if kind == "dev" and os.environ.get("ROBLOX_MANAGER_DEV") == "1" and not getattr(sys, "frozen", False):
            return raw.decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise SecretError("No se pudo descifrar la cookie (¿otro usuario de Windows?).") from exc
    raise SecretError("No se pudo descifrar la cookie (¿otro usuario de Windows?).")


# --------------------------------------------------------------------------------------
# Almacenamiento
# --------------------------------------------------------------------------------------
class Store:
    DEFAULTS = {
        "version": 1,
        "settings": {"delay": 5, "place": "", "job": "", "multi": True, "theme": "auto"},
        "places": [],
        "accounts": [],
    }

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return copy.deepcopy(self.DEFAULTS)
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            if not isinstance(d, dict):
                raise ValueError("formato")
        except (OSError, ValueError):
            backup = "%s.corrupto-%d" % (self.path, int(time.time()))
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            log.error("data.json ilegible; copia guardada en %s", backup)
            return copy.deepcopy(self.DEFAULTS)
        for key, value in self.DEFAULTS.items():
            d.setdefault(key, copy.deepcopy(value))
        for key, value in self.DEFAULTS["settings"].items():
            d["settings"].setdefault(key, value)
        return d

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        if not IS_WINDOWS:
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # -- cuentas
    def accounts(self):
        with self.lock:
            return [dict(a) for a in self.data["accounts"]]

    def get(self, account_id: str):
        with self.lock:
            for a in self.data["accounts"]:
                if a["id"] == account_id:
                    return dict(a)
        return None

    def cookie(self, account_id: str) -> str:
        acc = self.get(account_id)
        if not acc:
            raise SecretError("La cuenta ya no existe.")
        return unprotect_secret(acc["cookie"])

    def upsert(self, info: dict, cookie: str) -> bool:
        """Devuelve True si la cuenta es nueva, False si solo se actualizó su cookie."""
        blob = protect_secret(cookie)
        with self.lock:
            for a in self.data["accounts"]:
                if a["user_id"] == info["id"]:
                    a["cookie"] = blob
                    a["username"] = info["name"]
                    self._save()
                    return False
            self.data["accounts"].append({
                "id": uuid.uuid4().hex[:12],
                "alias": info["name"],
                "user_id": info["id"],
                "username": info["name"],
                "cookie": blob,
                "added": int(time.time()),
                "last_launch": None,
            })
            self._save()
            return True

    def update_account(self, account_id: str, **fields):
        self.update_accounts({account_id: fields})

    def update_accounts(self, updates: dict) -> int:
        """Actualiza varias cuentas con una sola escritura atómica del archivo."""
        if not updates:
            return 0
        with self.lock:
            changed = 0
            for a in self.data["accounts"]:
                fields = updates.get(a["id"])
                if fields:
                    a.update(fields)
                    changed += 1
            if changed:
                self._save()
            return changed

    def delete_accounts(self, ids):
        with self.lock:
            ids = set(ids)
            self.data["accounts"] = [a for a in self.data["accounts"] if a["id"] not in ids]
            self._save()

    # -- ajustes y favoritos
    def settings(self):
        with self.lock:
            return dict(self.data["settings"])

    def update_settings(self, **fields):
        with self.lock:
            self.data["settings"].update(fields)
            self._save()

    def places(self):
        with self.lock:
            return [dict(p) for p in self.data["places"]]

    def add_place(self, name: str, place_id: int, universe_id=None, icon=None):
        with self.lock:
            entry = next((p for p in self.data["places"] if p["place_id"] == place_id), None)
            if entry is None:
                entry = {"name": name, "place_id": place_id}
                self.data["places"].append(entry)
            entry["name"] = name
            if universe_id:
                entry["universe_id"] = universe_id
            if icon:
                entry["icon"] = icon
            self._save()

    def delete_place(self, place_id: int):
        with self.lock:
            self.data["places"] = [p for p in self.data["places"] if p["place_id"] != place_id]
            self._save()


# --------------------------------------------------------------------------------------
# Inicio de sesión: cliente WebSocket mínimo (para leer la cookie del navegador aislado)
# --------------------------------------------------------------------------------------
class MiniWS:
    """Cliente WebSocket mínimo (RFC 6455) para hablar con el puerto de depuración de Edge/Chrome."""

    def __init__(self, url: str, timeout: float = 20):
        u = urllib.parse.urlsplit(url)
        if u.scheme != "ws" or u.hostname not in ("127.0.0.1", "localhost"):
            raise RobloxError("Dirección de depuración no válida.")
        self.sock = socket.create_connection((u.hostname, u.port), timeout=timeout)
        self._id = 0
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = u.path or "/"
        self.sock.sendall((
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, u.hostname, u.port, key)
        ).encode("ascii"))
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            ch = self.sock.recv(1)
            if not ch:
                raise OSError("conexión cerrada")
            head += ch
            if len(head) > 16384:
                raise OSError("respuesta demasiado larga")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise RobloxError("El navegador rechazó la conexión de control.")

    def _exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise OSError("conexión cerrada")
            buf += chunk
        return buf

    def _send(self, opcode: int, data: bytes) -> None:
        n = len(data)
        hdr = bytearray([0x80 | opcode])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv(self) -> str:
        msg = b""
        while True:
            b1, b2 = self._exact(2)
            fin, op, ln = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._exact(8))[0]
            if ln > 32 * 1024 * 1024:
                raise OSError("mensaje demasiado grande")
            payload = self._exact(ln)
            if op == 8:
                raise OSError("conexión cerrada")
            if op == 9:
                self._send(10, payload)
                continue
            if op in (0, 1, 2):
                msg += payload
                if fin:
                    return msg.decode("utf-8", "replace")

    def call(self, method: str, params=None) -> dict:
        self._id += 1
        self._send(1, json.dumps({"id": self._id, "method": method, "params": params or {}}).encode("utf-8"))
        while True:
            try:
                msg = json.loads(self._recv())
            except ValueError as err:
                raise RobloxError("El navegador respondió algo que no se entiende.") from err
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RobloxError("El navegador dio un error: %s" % msg["error"].get("message", "?"))
                return msg.get("result", {})

    def close(self) -> None:
        try:
            self._send(8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LoginJob:
    """Un inicio de sesión en curso (ventana de navegador o código rápido)."""

    def __init__(self, method: str):
        self.method = method
        self.state = "waiting"      # waiting | ok | error | cancelled
        self.message = ""
        self.code = None
        self.username = None
        self.cancel = threading.Event()
        self._lock = threading.Lock()

    def update(self, message=None, code=None) -> None:
        with self._lock:
            if self.state != "waiting":
                return
            if message is not None:
                self.message = message
            if code is not None:
                self.code = code

    def finish(self, state: str, message: str, username=None) -> bool:
        with self._lock:
            if self.state != "waiting":
                return False
            self.state, self.message, self.username = state, message, username
            return True

    def snapshot(self) -> dict:
        with self._lock:
            return {"method": self.method, "state": self.state, "message": self.message,
                    "code": self.code, "username": self.username}


# --------------------------------------------------------------------------------------
# API de Roblox
# --------------------------------------------------------------------------------------
def roblox_img_ok(url: str) -> bool:
    """Solo se muestran imágenes servidas por el CDN de Roblox."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname or "").endswith(".rbxcdn.com")


def _walk(obj):
    """Recorre todos los diccionarios de un JSON, en orden (la API de Roblox cambia de forma a menudo)."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _posint(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def parse_games(data, limit=60):
    """Saca los juegos de cualquier respuesta de Roblox: [{universe_id, place_id, name, players}]."""
    out, seen = [], set()
    for d in _walk(data):
        uid = _posint(d.get("universeId"))
        name = d.get("name") if isinstance(d.get("name"), str) and d.get("name").strip() else None
        if uid is None and str(d.get("contentType", "")).lower() == "game":
            uid = _posint(d.get("contentId"))
        if uid is None or uid in seen or (name is None and "universeId" in d):
            continue
        seen.add(uid)
        players = d.get("playerCount", d.get("playing"))
        out.append({
            "universe_id": uid,
            "place_id": _posint(d.get("rootPlaceId")) or _posint(d.get("placeId")),
            "name": name[:80] if name else None,
            "players": players if isinstance(players, int) and players >= 0 else None,
        })
        if len(out) >= limit:
            break
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Nunca seguimos redirecciones: una redirección podría reenviar la cookie a otro sitio."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def roblox_domain(domain) -> bool:
    d = str(domain or "").lstrip(".").lower()
    return d == "roblox.com" or d.endswith(".roblox.com")


def cookie_host_ok(url: str) -> bool:
    """La cookie de sesión solo puede viajar por HTTPS hacia roblox.com y sus subdominios."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and roblox_domain(parts.hostname)


class _ConnPool:
    """Conexiones HTTP(S) que se reutilizan: abrir una nueva (saludo TLS) cuesta más que la propia petición."""

    def __init__(self, max_idle: int = 8, idle_secs: float = 25.0):
        self.lock, self.idle, self.max_idle, self.idle_secs = threading.Lock(), {}, max_idle, idle_secs

    def get(self, key, timeout):
        with self.lock:
            lst = self.idle.get(key, [])
            while lst:
                conn, stamp = lst.pop()
                if time.time() - stamp < self.idle_secs:
                    return conn, True
                conn.close()
        cls = http.client.HTTPSConnection if key[0] == "https" else http.client.HTTPConnection
        return cls(key[1], key[2], timeout=timeout), False

    def put(self, key, conn):
        with self.lock:
            lst = self.idle.setdefault(key, [])
            if len(lst) < self.max_idle:
                lst.append((conn, time.time()))
                return
        conn.close()


class RobloxAPI:
    def __init__(self, users_base="https://users.roblox.com", auth_base="https://auth.roblox.com",
                 timeout=15, thumbs_base="https://thumbnails.roblox.com", img_ok=roblox_img_ok,
                 apis_base="https://apis.roblox.com", games_base="https://games.roblox.com", strict=True):
        self.strict = strict  # las pruebas con servidores locales lo desactivan
        self._pool = _ConnPool()
        self._csrf, self._csrf_lock = {}, threading.Lock()
        self.games_base = games_base.rstrip("/")
        self.apis_base = apis_base.rstrip("/")
        self.users_base = users_base.rstrip("/")
        self.auth_base = auth_base.rstrip("/")
        self.thumbs_base = thumbs_base.rstrip("/")
        self.img_ok = img_ok
        self.timeout = timeout

    def _request(self, method, url, headers, body=None):
        if self.strict and any(k.lower() == "cookie" for k in headers) and not cookie_host_ok(url):
            raise RobloxError("Por seguridad, la cookie solo se envía por HTTPS a roblox.com.")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme in ("http", "https") and parts.hostname:
            try:
                return self._pooled_request(method, parts, headers, body)
            except TimeoutError as err:
                raise RobloxError("No hay conexión con Roblox (%s). Revisa tu internet." % err) from err
            except (http.client.HTTPException, OSError):
                pass  # p. ej. detrás de un proxy: se usa el camino normal de urllib
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.headers, err.read()
        except (urllib.error.URLError, OSError) as err:
            reason = getattr(err, "reason", err)
            raise RobloxError("No hay conexión con Roblox (%s). Revisa tu internet." % reason) from err

    def _pooled_request(self, method, parts, headers, body):
        """Petición con conexión reutilizada. No sigue redirecciones: un 3xx se devuelve tal cual."""
        key = (parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        hdrs = dict(headers)
        hdrs.setdefault("Accept-Encoding", "identity")
        for attempt in (0, 1):
            conn, reused = self._pool.get(key, self.timeout)
            try:
                conn.request(method, path, body=body, headers=hdrs)
                resp = conn.getresponse()
                data = resp.read()
            except (http.client.HTTPException, OSError):
                conn.close()
                if reused and attempt == 0:  # el servidor ya había cerrado esa conexión inactiva: se prueba con una nueva
                    continue
                raise
            if resp.will_close:
                conn.close()
            else:
                self._pool.put(key, conn)
            return resp.status, resp.headers, data
        raise OSError("sin conexión")

    @staticmethod
    def _headers(cookie, extra=None):
        h = {"Cookie": ".ROBLOSECURITY=" + cookie, "User-Agent": USER_AGENT, "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    def whoami(self, cookie: str) -> dict:
        status, _, body = self._request("GET", self.users_base + "/v1/users/authenticated", self._headers(cookie))
        if status in (401, 403):
            raise AuthExpired("La cookie no es válida o ha caducado. Vuelve a copiarla del navegador.")
        if status == 429:
            raise RobloxError("Roblox está limitando las peticiones (429). Espera un minuto y reintenta.")
        if status != 200:
            raise RobloxError("Respuesta inesperada de Roblox (%d)." % status)
        try:
            j = json.loads(body.decode("utf-8"))
            return {"id": int(j["id"]), "name": str(j["name"]), "display": str(j.get("displayName", j["name"]))}
        except (ValueError, KeyError, TypeError) as err:
            raise RobloxError("Roblox devolvió una respuesta que no se entiende.") from err

    def avatars(self, user_ids) -> dict:
        """Miniaturas públicas de cabeza: {user_id: url}. No necesita cookie."""
        ids = [int(i) for i in user_ids][:100]
        if not ids:
            return {}
        query = urllib.parse.urlencode({
            "userIds": ",".join(str(i) for i in ids), "size": "150x150", "format": "Png", "isCircular": "false",
        })
        status, _, body = self._request(
            "GET", "%s/v1/users/avatar-headshot?%s" % (self.thumbs_base, query),
            {"User-Agent": USER_AGENT, "Accept": "application/json"})
        if status != 200:
            raise RobloxError("Roblox no dio los avatares (%d)." % status)
        try:
            items = json.loads(body.decode("utf-8")).get("data", [])
            out = {}
            for item in items:
                url = item.get("imageUrl")
                if item.get("state") == "Completed" and isinstance(url, str) and self.img_ok(url):
                    out[int(item["targetId"])] = url
            return out
        except (ValueError, KeyError, TypeError, AttributeError) as err:
            raise RobloxError("Roblox devolvió avatares que no se entienden.") from err

    CSRF_TTL = 600  # segundos que se reutiliza un token CSRF; si caduca antes, Roblox da uno nuevo y se reintenta

    def _csrf_key(self, cookie: str) -> str:
        return hashlib.sha256(cookie.encode("utf-8")).hexdigest()

    def _csrf_get(self, key: str):
        with self._csrf_lock:
            hit = self._csrf.get(key)
            return hit[0] if hit and time.time() - hit[1] < self.CSRF_TTL else None

    def _csrf_put(self, key: str, token: str) -> None:
        with self._csrf_lock:
            self._csrf[key] = (token, time.time())

    def _ticket_request(self, cookie: str, place_id: int):
        url = self.auth_base + "/v1/authentication-ticket"
        extra = {
            "Content-Type": "application/json",
            "Referer": "https://www.roblox.com/games/%d" % place_id,
            "Origin": "https://www.roblox.com",
            "RBX-For-Gameauth": "true",
        }
        return url, self._headers(cookie, extra)

    def warm(self, cookie: str) -> None:
        """Se adelanta al lanzamiento: consigue el token CSRF y deja la conexión abierta.
        Sin token Roblox responde 403 con el token y NO crea ningún ticket."""
        url, headers = self._ticket_request(cookie, 1)
        status, resp_headers, _ = self._request("POST", url, headers, b"{}")
        if status == 403 and resp_headers.get("x-csrf-token"):
            self._csrf_put(self._csrf_key(cookie), resp_headers["x-csrf-token"])

    def auth_ticket(self, cookie: str, place_id: int) -> str:
        url, headers = self._ticket_request(cookie, place_id)
        key = self._csrf_key(cookie)
        token = self._csrf_get(key)
        if token:  # con el token ya guardado basta UNA petición (antes eran dos)
            headers["X-CSRF-TOKEN"] = token
        status, resp_headers, _ = self._request("POST", url, headers, b"{}")
        if status == 403 and resp_headers.get("x-csrf-token"):
            token = resp_headers["x-csrf-token"]
            self._csrf_put(key, token)
            headers["X-CSRF-TOKEN"] = token
            status, resp_headers, _ = self._request("POST", url, headers, b"{}")
        elif status == 200 and token:
            self._csrf_put(key, token)
        if status == 401:
            raise AuthExpired("Cookie caducada")
        if status == 429:
            raise RobloxError("Roblox está limitando las peticiones (429). Espera un poco entre cuentas.")
        if status != 200:
            raise RobloxError("Roblox no dio el ticket de acceso (%d)." % status)
        ticket = resp_headers.get("rbx-authentication-ticket")
        if not ticket:
            raise RobloxError("Roblox no devolvió el ticket de acceso.")
        return ticket

    def logout(self, cookie: str) -> None:
        """Cierra la sesión en los servidores de Roblox: la cookie guardada deja de servir."""
        url = self.auth_base + "/v2/logout"
        headers = self._headers(cookie, {"Content-Type": "application/json", "Origin": "https://www.roblox.com",
                                         "Referer": "https://www.roblox.com/"})
        status, resp_headers, _ = self._request("POST", url, headers, b"{}")
        if status == 403 and resp_headers.get("x-csrf-token"):
            headers["X-CSRF-TOKEN"] = resp_headers["x-csrf-token"]
            status, resp_headers, _ = self._request("POST", url, headers, b"{}")
        if status == 401:
            return  # ya estaba caducada
        if status != 200:
            raise RobloxError("Roblox no cerró la sesión (%d)." % status)

    # -- lista de juegos (datos públicos, sin cookie)
    def _get_json(self, url):
        status, _, body = self._request("GET", url, {"User-Agent": USER_AGENT, "Accept": "application/json"})
        if status == 429:
            raise RobloxError("Roblox está limitando las peticiones (429). Espera un momento y reintenta.")
        if status != 200:
            raise RobloxError("Roblox no devolvió la lista de juegos (%d)." % status)
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as err:
            raise RobloxError("Roblox devolvió datos de juegos que no se entienden.") from err

    def explore_sorts(self, session: str):
        q = urllib.parse.urlencode({"sessionId": session, "device": "computer", "country": "all"})
        data = self._get_json(self.apis_base + "/explore-api/v1/get-sorts?" + q)
        out, seen = [], set()
        for d in _walk(data):
            sid = d.get("sortId")
            name = d.get("sortDisplayName") or d.get("sortName") or d.get("displayName")
            ctype = str(d.get("contentType", "")).lower()
            if isinstance(sid, str) and sid and isinstance(name, str) and name and sid not in seen \
                    and (not ctype or "game" in ctype):
                seen.add(sid)
                out.append({"id": "x:" + sid, "name": name[:50]})
        return out

    def legacy_sorts(self):
        q = urllib.parse.urlencode({"model.gameSortsContext": "GamesDefaultSorts"})
        data = self._get_json(self.games_base + "/v1/games/sorts?" + q)
        out = []
        for d in _walk(data):
            token = d.get("token")
            name = d.get("displayName") or d.get("name")
            if isinstance(token, str) and token and isinstance(name, str) and name:
                out.append({"id": "l:" + token, "name": name[:50]})
        return out

    def sort_games(self, session: str, sort_id: str):
        kind, _, value = sort_id.partition(":")
        if kind == "x" and value:
            q = urllib.parse.urlencode({"sessionId": session, "sortId": value, "device": "computer", "country": "all"})
            return parse_games(self._get_json(self.apis_base + "/explore-api/v1/get-sort-content?" + q))
        if kind == "l" and value:
            q = urllib.parse.urlencode({"model.sortToken": value, "model.maxRows": 48, "model.startRows": 0})
            return parse_games(self._get_json(self.games_base + "/v1/games/list?" + q))
        raise ValueError("Categoría no válida.")

    def search_games(self, session: str, text: str):
        q = urllib.parse.urlencode({"searchQuery": text, "sessionId": session, "pageType": "all"})
        return parse_games(self._get_json(self.apis_base + "/search-api/omni-search?" + q))

    def game_details(self, universe_ids):
        ids = [int(i) for i in universe_ids][:100]
        if not ids:
            return {}
        q = urllib.parse.urlencode({"universeIds": ",".join(str(i) for i in ids)})
        data = self._get_json(self.games_base + "/v1/games?" + q)
        out = {}
        for d in (data.get("data", []) if isinstance(data, dict) else []):
            uid = _posint(d.get("id"))
            if uid:
                out[uid] = {"place_id": _posint(d.get("rootPlaceId")),
                            "name": str(d.get("name") or "")[:80] or None,
                            "players": d.get("playing") if isinstance(d.get("playing"), int) else None}
        return out

    def game_icons(self, universe_ids):
        ids = [int(i) for i in universe_ids][:100]
        if not ids:
            return {}
        q = urllib.parse.urlencode({"universeIds": ",".join(str(i) for i in ids), "returnPolicy": "PlaceHolder",
                                    "size": "150x150", "format": "Png", "isCircular": "false"})
        data = self._get_json(self.thumbs_base + "/v1/games/icons?" + q)
        out = {}
        for item in (data.get("data", []) if isinstance(data, dict) else []):
            url, uid = item.get("imageUrl"), _posint(item.get("targetId"))
            if uid and isinstance(url, str) and self.img_ok(url):
                out[uid] = url
        return out

    def place_icons(self, place_ids):
        ids = [int(i) for i in place_ids][:100]
        if not ids:
            return {}
        q = urllib.parse.urlencode({"placeIds": ",".join(str(i) for i in ids), "returnPolicy": "PlaceHolder",
                                    "size": "150x150", "format": "Png", "isCircular": "false"})
        data = self._get_json(self.thumbs_base + "/v1/places/gameicons?" + q)
        out = {}
        for item in (data.get("data", []) if isinstance(data, dict) else []):
            url, pid = item.get("imageUrl"), _posint(item.get("targetId"))
            if pid and isinstance(url, str) and self.img_ok(url):
                out[pid] = url
        return out

    # -- inicio rápido (Quick Log In): se escribe un código en un dispositivo donde ya hay sesión
    def _post_json(self, url, payload):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json",
                   "Origin": "https://www.roblox.com", "Referer": "https://www.roblox.com/"}
        body = json.dumps(payload).encode("utf-8")
        status, resp_headers, data = self._request("POST", url, headers, body)
        if status == 403 and resp_headers.get("x-csrf-token"):
            headers["X-CSRF-TOKEN"] = resp_headers["x-csrf-token"]
            status, resp_headers, data = self._request("POST", url, headers, body)
        return status, resp_headers, data

    @staticmethod
    def _json(data):
        try:
            d = json.loads(data.decode("utf-8"))
            return d if isinstance(d, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def quick_create(self) -> dict:
        status, _, data = self._post_json(self.apis_base + "/auth-token-service/v1/login/create", {})
        info = self._json(data)
        if status == 429:
            raise RobloxError("Roblox está limitando las peticiones (429). Espera un minuto.")
        if status != 200 or not info.get("code") or not info.get("privateKey"):
            raise RobloxError("Roblox no generó el código de inicio rápido (%d)." % status)
        return info

    def quick_status(self, code: str, key: str):
        status, _, data = self._post_json(self.apis_base + "/auth-token-service/v1/login/status",
                                          {"code": code, "privateKey": key})
        return status, self._json(data)

    def quick_cancel(self, code: str) -> None:
        try:
            self._post_json(self.apis_base + "/auth-token-service/v1/login/cancel", {"code": code})
        except RobloxError:
            pass

    def quick_login(self, code: str, key: str) -> str:
        status, headers, _ = self._post_json(self.auth_base + "/v2/login",
                                             {"ctype": "AuthToken", "cvalue": code, "password": key})
        if status == 429:
            raise RobloxError("Roblox está limitando las peticiones (429). Espera un minuto.")
        if status != 200:
            raise RobloxError("Roblox no completó el inicio rápido (%d)." % status)
        for value in headers.get_all("Set-Cookie") or []:
            m = re.match(r"\s*\.ROBLOSECURITY=([^;]+)", value)
            if m:
                return m.group(1)
        raise RobloxError("Roblox no devolvió la cookie de sesión.")


# --------------------------------------------------------------------------------------
# Plataforma (procesos, multi-instancia, lanzamiento)
# --------------------------------------------------------------------------------------
def process_cmdline(pid: int):
    """Línea de comandos de un proceso del mismo usuario (None si Windows no deja leerla)."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
                                                ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    handle = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = wintypes.ULONG(0)
        ntdll.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(size))  # 60 = ProcessCommandLineInformation
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if ntdll.NtQueryInformationProcess(handle, 60, buf, size.value, ctypes.byref(size)) != 0:
            return None
        length = struct.unpack_from("<H", buf, 0)[0]  # UNICODE_STRING: Length, MaximumLength, (relleno), Buffer
        ptr = struct.unpack_from("<Q" if ctypes.sizeof(ctypes.c_void_p) == 8 else "<I", buf,
                                 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 4)[0]
        return ctypes.wstring_at(ptr, length // 2) if ptr and length else None
    finally:
        k32.CloseHandle(handle)


def toolhelp_processes(exe_names=None):
    """Instantánea de PIDs por nombre de ejecutable, sin lanzar procesos externos."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snap in (None, ctypes.c_void_p(-1).value):
        raise OSError("No se pudo leer la lista de procesos")
    if isinstance(exe_names, str):
        exe_names = (exe_names,)
    wanted = {name.lower() for name in exe_names} if exe_names else None
    processes = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.lower()
            if wanted is None or name in wanted:
                processes.setdefault(name, []).append(int(entry.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    for pids in processes.values():
        pids.sort()
    return processes


def toolhelp_pids(exe_name: str):
    """PIDs de un ejecutable según una instantánea rápida de procesos de Windows."""
    return toolhelp_processes((exe_name,)).get(exe_name.lower(), [])


def _sys32(name: str) -> str:
    """Ruta completa de una herramienta de Windows (evita ejecutar un .exe falso de la carpeta actual)."""
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", name)


class WindowsPlatform:
    UPDATER_EXES = ("RobloxPlayerInstaller.exe", "RobloxPlayerLauncher.exe")

    def __init__(self, mutex_name=MUTEX_NAME, event_name=EVENT_NAME):
        self._mutex = self._event_guard = None
        self.mutex_name, self.event_name = mutex_name, event_name

    def launch(self, uri: str) -> None:
        os.startfile(uri)  # noqa: S606  (abre el manejador del protocolo roblox-player:)

    def list_pids(self):
        try:
            return toolhelp_pids(PLAYER_EXE)
        except Exception:  # noqa: BLE001
            pass  # si falla la API nativa, se pregunta a tasklist como antes
        try:
            out = subprocess.run(
                [_sys32("tasklist.exe"), "/FI", "IMAGENAME eq " + PLAYER_EXE, "/FO", "CSV", "/NH"],
                capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        return parse_tasklist(out.stdout.decode("utf-8", "replace"))

    def process_snapshot(self):
        return toolhelp_processes((PLAYER_EXE,) + self.UPDATER_EXES)

    def updater_pids(self, snapshot=None):
        try:
            snapshot = snapshot if snapshot is not None else self.process_snapshot()
            return sorted({pid for name in self.UPDATER_EXES for pid in snapshot.get(name.lower(), ())})
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def player_version():
        """Versión instalada que usa el protocolo roblox-player, si Windows la publica."""
        if not IS_WINDOWS:
            return None
        try:
            import winreg

            key_path = r"Software\Classes\roblox-player\shell\open\command"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                command = winreg.QueryValueEx(key, None)[0]
            match = re.match(r'^\s*(?:"([^"]+)"|(.*?RobloxPlayerBeta\.exe))', command, re.IGNORECASE)
            executable = (match.group(1) or match.group(2)) if match else ""
            if os.path.basename(executable).lower() != PLAYER_EXE.lower():
                return None
            return os.path.basename(os.path.dirname(executable))
        except (OSError, ImportError, TypeError):
            return None

    def show_update_window(self, pids=None):
        """Trae al frente una ventana del actualizador que Roblox ya haya iniciado."""
        pids = set(pids or self.updater_pids())
        if not pids or not IS_WINDOWS:
            return False
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindowAsync.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.FlashWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        user32.FlashWindow.restype = wintypes.BOOL
        found_windows = []

        def visit(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
                foreground = bool(user32.SetForegroundWindow(hwnd))
                if not foreground:
                    user32.FlashWindow(hwnd, True)
                found_windows.append(True)
                return False
            return True

        user32.EnumWindows(enum_proc(visit), 0)
        return bool(found_windows)

    def cmdline(self, pid: int):
        try:
            return process_cmdline(pid)
        except Exception:  # noqa: BLE001
            return None

    def kill_pid(self, pid: int) -> None:
        subprocess.run([_sys32("taskkill.exe"), "/PID", str(int(pid)), "/F", "/T"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15)

    def kill_all(self) -> None:
        subprocess.run([_sys32("taskkill.exe"), "/IM", PLAYER_EXE, "/F"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15)

    def multi_enable(self):
        """Toma ROBLOX_singletonMutex y ROBLOX_singletonEvent antes que el cliente. Devuelve (activa, detalle).

        El Roblox actual cierra la ventana anterior cuando se abre una nueva si existe el *evento*
        ROBLOX_singletonEvent. Reclamamos ese nombre con un *mutex*: Roblox no puede crear un evento con un
        nombre que ya es de otro tipo, y las ventanas ya abiertas dejan de recibir la orden de cerrarse."""
        if self._mutex and self._event_guard:
            return True, ""
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        ctypes.set_last_error(0)
        handle = k32.CreateMutexW(None, True, self.mutex_name)
        err = ctypes.get_last_error()
        if not handle:
            return False, "Windows no permitió crear el bloqueo (código %d)." % err
        note = ""
        if err == ERROR_ALREADY_EXISTS:
            if self.list_pids():
                k32.CloseHandle(handle)
                return False, ("Había un Roblox abierto antes de activar la multi-instancia. "
                               "Ciérralos y pulsa Reintentar.")
            note = "Otro programa (Fishstrap, Bloxstrap…) ya mantenía el bloqueo; lo compartimos."
        ctypes.set_last_error(0)
        guard = k32.CreateMutexW(None, True, self.event_name)
        if not guard:  # el nombre ya es un evento: lo creó un Roblox que sigue abierto
            k32.CloseHandle(handle)
            return False, self._event_busy_detail()
        self._mutex, self._event_guard = handle, guard
        return True, note

    def _event_busy_detail(self) -> str:
        pids = []
        try:
            pids = self.list_pids()
        except Exception:  # noqa: BLE001
            pass
        if any("--launch-to-tray" in (self.cmdline(pid) or "") for pid in pids):
            return ("Roblox está abierto en segundo plano (icono de la bandeja del sistema) y bloquea la "
                    "multi-instancia. Ciérralo y pulsa Reintentar.")
        return "Hay un Roblox abierto que bloquea la multi-instancia. Ciérralos todos y pulsa Reintentar."

    def multi_disable(self) -> None:
        if not (self._mutex or self._event_guard):
            return
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        for handle in (self._mutex, self._event_guard):
            if handle:
                k32.CloseHandle(handle)
        self._mutex = self._event_guard = None


class NullPlatform:
    """Para ver la interfaz fuera de Windows (no lanza nada)."""

    def launch(self, uri):
        raise RobloxError("Lanzar Roblox solo funciona en Windows.")

    def list_pids(self):
        return []

    def cmdline(self, pid):
        return None

    def kill_pid(self, pid):
        pass

    def kill_all(self):
        pass

    def multi_enable(self):
        return False, "Este equipo no es Windows: solo puedes ver la interfaz."

    def multi_disable(self):
        pass


def make_platform():
    return WindowsPlatform() if IS_WINDOWS else NullPlatform()


# --------------------------------------------------------------------------------------
# Lógica principal
# --------------------------------------------------------------------------------------
class Manager:
    def __init__(self, store: Store, api: RobloxAPI, platform):
        self.store, self.api, self.plat = store, api, platform
        self.lock = threading.RLock()
        self.status = {}        # id -> {"text", "tag"}   tag: idle | busy | ok | warn | bad
        self.pids = {}          # id -> pid
        self.known_pids = []
        self.multi_active = False
        self.multi_detail = ""
        self.cancel = threading.Event()
        self.worker = None
        self._launch_started = {}
        self._launch_version = {}
        self._update_notice_id = 0
        self._update_notice = None
        self._update_notice_signature = None
        self._last_update_raise_at = 0.0
        self._shown_updater_pids = set()
        self._update_lock = threading.Lock()
        self._warm = {}
        self.login = None
        self.session_id = str(uuid.uuid4())
        self._gcache = {}
        self._stop = threading.Event()

    # -- ciclo de vida
    def start(self):
        if self.store.settings().get("multi", True):
            self.multi_active, self.multi_detail = self.plat.multi_enable()
        else:
            self.multi_active, self.multi_detail = False, ""
        threading.Thread(target=self._ticker, daemon=True).start()
        threading.Thread(target=self.refresh_avatars, daemon=True).start()

    def stop(self):
        self._stop.set()
        self.cancel_login()

    def _ticker(self):
        while not self._stop.is_set():
            try:
                self.refresh_pids()
            except Exception:  # noqa: BLE001
                log.exception("Error actualizando procesos")
            self._stop.wait(2)

    def launching(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def refresh_avatars(self, force=False):
        """Pide los avatares que faltan o tienen más de un día. Los fallos no son graves."""
        try:
            now = time.time()
            need = [a for a in self.store.accounts()
                    if force or not a.get("avatar") or now - a.get("avatar_ts", 0) > 86400]
            if not need:
                return
            urls = self.api.avatars([a["user_id"] for a in need])
            updates = {a["id"]: {"avatar": urls[a["user_id"]], "avatar_ts": int(now)}
                       for a in need if urls.get(a["user_id"])}
            self.store.update_accounts(updates)
        except (RobloxError, OSError, ValueError) as err:
            log.info("No se pudieron actualizar los avatares: %s", err)

    # -- estado
    def set_status(self, account_id, text, tag):
        with self.lock:
            self.status[account_id] = {"text": text, "tag": tag}

    def _process_snapshot(self):
        snapshot = getattr(self.plat, "process_snapshot", None)
        if snapshot:
            try:
                return snapshot()
            except Exception:  # noqa: BLE001
                log.debug("No se pudo leer la instantánea de procesos", exc_info=True)
        try:
            return {PLAYER_EXE.lower(): self.plat.list_pids()}
        except Exception:  # noqa: BLE001
            return {PLAYER_EXE.lower(): []}

    def _player_version(self):
        getter = getattr(self.plat, "player_version", None)
        try:
            return getter() if getter else None
        except Exception:  # noqa: BLE001
            return None

    def _notify_update(self, signature, message):
        with self.lock:
            if signature == self._update_notice_signature:
                return
            self._update_notice_signature = signature
            self._update_notice_id += 1
            self._update_notice = {"id": self._update_notice_id, "text": message}

    def _observe_update(self, snapshot, baseline_version=None):
        get_updaters = getattr(self.plat, "updater_pids", None)
        try:
            updater_pids = get_updaters(snapshot) if get_updaters else []
        except Exception:  # noqa: BLE001
            updater_pids = []
        if updater_pids:
            now = time.monotonic()
            active_pids = set(updater_pids)
            with self._update_lock:
                self._shown_updater_pids.intersection_update(active_pids)
                pending_pids = active_pids - self._shown_updater_pids
                if pending_pids and now - self._last_update_raise_at >= 1.0:
                    self._last_update_raise_at = now
                    show = getattr(self.plat, "show_update_window", None)
                    if show:
                        try:
                            if show(sorted(pending_pids)):
                                self._shown_updater_pids.update(pending_pids)
                        except Exception:  # noqa: BLE001
                            log.debug("No se pudo mostrar la ventana del actualizador de Roblox", exc_info=True)
                raised = bool(active_pids & self._shown_updater_pids)
            suffix = " La ventana del actualizador de Roblox está abierta." if raised else ""
            self._notify_update(("updating", tuple(sorted(active_pids)), raised),
                                "Roblox está aplicando una actualización. Espera a que termine y vuelve a pulsar «Lanzar»."
                                + suffix)
            return "updating"

        if not baseline_version:
            return None
        current_version = self._player_version()
        if current_version and current_version != baseline_version:
            self._notify_update(("updated", current_version),
                                "Roblox se actualizó y cerró una instancia durante el inicio. "
                                "Vuelve a pulsar «Lanzar» para abrirla de nuevo.")
            return "updated"
        return None

    def refresh_pids(self):
        snapshot = self._process_snapshot()
        pids = snapshot.get(PLAYER_EXE.lower(), [])
        now = time.monotonic()
        with self.lock:
            recent = {aid: version for aid, started in self._launch_started.items()
                      if now - started <= 90 for version in (self._launch_version.get(aid),)}
        update_state = self._observe_update(snapshot) if recent else None
        current_version = self._player_version() if recent and update_state != "updating" else None
        updated_launches = {aid for aid, version in recent.items()
                            if version and current_version and version != current_version}
        if update_state != "updating" and updated_launches:
            self._notify_update(("updated", current_version),
                                "Roblox se actualizó y cerró una instancia durante el inicio. "
                                "Vuelve a pulsar «Lanzar» para abrirla de nuevo.")
            update_state = "updated"
        with self.lock:
            self.known_pids = pids
            alive = set(pids)
            for aid, pid in list(self.pids.items()):
                if pid not in alive:
                    del self.pids[aid]
                    if self.status.get(aid, {}).get("tag") == "ok":
                        if aid in recent and update_state == "updating":
                            self.status[aid] = {"text": "Actualización de Roblox", "tag": "warn"}
                        elif aid in updated_launches:
                            self.status[aid] = {"text": "Roblox actualizado; relanza", "tag": "warn"}
                        else:
                            self.status[aid] = {"text": "Cerrada", "tag": "idle"}
            if update_state == "updated":
                for aid in updated_launches:
                    if aid not in self.pids and self.status.get(aid, {}).get("tag") == "warn":
                        self.status[aid] = {"text": "Roblox actualizado; relanza", "tag": "warn"}
            expired = [aid for aid, started in self._launch_started.items() if now - started > 90]
            for aid in expired:
                self._launch_started.pop(aid, None)
                self._launch_version.pop(aid, None)

    def state(self) -> dict:
        with self.lock:
            accounts = []
            for a in self.store.accounts():
                st = self.status.get(a["id"], {"text": "Lista", "tag": "idle"})
                accounts.append({
                    "id": a["id"], "alias": a["alias"], "username": a["username"],
                    "status": st["text"], "tag": st["tag"],
                    "pid": self.pids.get(a["id"]), "last_launch": a.get("last_launch"),
                    "avatar": a.get("avatar"),
                })
            return {
                "accounts": accounts,
                "places": self.store.places(),
                "settings": self.store.settings(),
                "multi": {"active": self.multi_active, "detail": self.multi_detail},
                "roblox_count": len(self.known_pids),
                "launching": self.launching(),
                "platform": "windows" if IS_WINDOWS else "other",
                "update_notice": dict(self._update_notice) if self._update_notice else None,
            }

    # -- cuentas
    def add_accounts(self, text: str):
        lines = text.splitlines()
        entries = [(i + 1, l.strip()) for i, l in enumerate(lines) if l.strip()]
        if not entries:
            raise ValueError("Pega al menos una cookie.")
        if len(entries) > 50:
            raise ValueError("Añade como mucho 50 cuentas de una vez.")
        results = []
        for line_no, raw in entries:
            try:
                cookie = normalize_cookie(raw)
                info = self.api.whoami(cookie)
                created = self.store.upsert(info, cookie)
                results.append({"ok": True, "line": line_no, "username": info["name"], "created": created})
            except (ValueError, RobloxError, SecretError) as err:
                results.append({"ok": False, "line": line_no, "error": str(err)})
        if any(r["ok"] for r in results):
            self.refresh_avatars()
        return results

    def rename(self, account_id: str, alias: str):
        alias = alias.strip()[:40]
        if not alias:
            raise ValueError("El nombre no puede estar vacío.")
        if not self.store.get(account_id):
            raise ValueError("La cuenta ya no existe.")
        self.store.update_account(account_id, alias=alias)

    def delete(self, ids, revoke=False):
        """Elimina cuentas. Con revoke=True cierra antes la sesión en Roblox (invalida la cookie guardada)."""
        revoked = failed = 0
        if revoke:
            for i in ids:
                try:
                    self.api.logout(self.store.cookie(i))
                    revoked += 1
                except (RobloxError, SecretError):
                    failed += 1
        self.store.delete_accounts(ids)
        with self.lock:
            for i in ids:
                self.status.pop(i, None)
                self.pids.pop(i, None)
                self._launch_started.pop(i, None)
                self._launch_version.pop(i, None)
        return {"revoked": revoked, "failed": failed}

    def verify(self, ids):
        ids = [i for i in ids if self.store.get(i)] or [a["id"] for a in self.store.accounts()]
        if not ids:
            raise ValueError("Todavía no hay cuentas que comprobar.")
        threading.Thread(target=self._verify_worker, args=(ids,), daemon=True).start()

    def _verify_worker(self, ids):
        for aid in ids:
            with self.lock:
                if self.status.get(aid, {}).get("tag") in ("busy", "ok"):
                    continue
            self.set_status(aid, "Comprobando…", "busy")
            try:
                info = self.api.whoami(self.store.cookie(aid))
                self.store.update_account(aid, username=info["name"])
                self.set_status(aid, "Cookie válida", "idle")
            except AuthExpired:
                self.set_status(aid, "Cookie caducada", "bad")
            except (RobloxError, SecretError) as err:
                self.set_status(aid, "Error: %s" % err, "bad")
            time.sleep(0.3)

    # -- lista de juegos
    def _cached(self, key, fn, ttl=90):
        now = time.time()
        hit = self._gcache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = fn()
        self._gcache[key] = (now, value)
        if len(self._gcache) > 60:
            self._gcache.pop(min(self._gcache, key=lambda k: self._gcache[k][0]), None)
        return value

    def _decorate_games(self, games):
        need = [g["universe_id"] for g in games if not g["place_id"] or not g["name"]]
        if need:
            try:
                details = self.api.game_details(need)
            except RobloxError as err:
                log.info("Sin detalles de juegos: %s", err)
                details = {}
            for g in games:
                d = details.get(g["universe_id"])
                if d:
                    g["place_id"] = g["place_id"] or d["place_id"]
                    g["name"] = g["name"] or d["name"]
                    if g["players"] is None:
                        g["players"] = d["players"]
        games = [g for g in games if g["place_id"] and g["name"]]
        try:
            icons = self.api.game_icons([g["universe_id"] for g in games])
        except RobloxError as err:
            log.info("Sin iconos de juegos: %s", err)
            icons = {}
        for g in games:
            g["icon"] = icons.get(g["universe_id"])
        return games

    def game_sorts(self):
        def load():
            sorts = []
            try:
                sorts = self.api.explore_sorts(self.session_id)
            except RobloxError as err:
                log.info("explore-api no disponible: %s", err)
            if not sorts:
                sorts = self.api.legacy_sorts()
            if not sorts:
                raise RobloxError("No pude cargar las categorías de juegos de Roblox. Prueba con el buscador.")
            return sorts[:14]
        return {"sorts": self._cached("sorts", load, 600)}

    def games_list(self, sort_id: str):
        sort_id = str(sort_id or "")[:300]
        if not sort_id:
            raise ValueError("Elige una categoría.")
        return {"games": self._cached("s:" + sort_id,
                                      lambda: self._decorate_games(self.api.sort_games(self.session_id, sort_id)))}

    def games_search(self, text: str):
        q = str(text or "").strip()[:60]
        if len(q) < 2:
            raise ValueError("Escribe al menos 2 letras para buscar.")
        def load():
            found = self.api.search_games(self.session_id, q)
            low = q.lower()

            def rank(g):  # coincidencia exacta, luego empieza por, luego contiene (orden estable)
                name = (g["name"] or "").lower()
                return 0 if name == low else 1 if name.startswith(low) else 2 if low in name else 3
            found.sort(key=rank)
            return self._decorate_games(found)
        return {"games": self._cached("q:" + q.lower(), load)}

    # -- inicio de sesión: ventana de navegador aislada / código rápido
    def start_login(self, method: str):
        if method not in ("browser", "quick"):
            raise ValueError("Método de inicio de sesión no válido.")
        with self.lock:
            if self.login and self.login.state == "waiting":
                raise ValueError("Ya hay un inicio de sesión en curso. Cancélalo primero.")
            job = LoginJob(method)
            job.update("Abriendo ventana…" if method == "browser" else "Generando código…")
            self.login = job
        threading.Thread(target=self._run_login, args=(job,), daemon=True).start()

    def login_state(self) -> dict:
        job = self.login
        return job.snapshot() if job else {"state": "none"}

    def cancel_login(self):
        job = self.login
        if job:
            job.cancel.set()
            job.finish("cancelled", "Cancelado.")

    def _run_login(self, job):
        try:
            if job.method == "browser":
                self._browser_login(job)
            else:
                self._quick_login(job)
        except (RobloxError, SecretError, ValueError, OSError) as err:
            log.warning("Inicio de sesión (%s) fallido: %s", job.method, err)
            job.finish("error", str(err) or "Error desconocido.")
        except Exception:  # noqa: BLE001
            log.exception("Fallo inesperado en el inicio de sesión")
            job.finish("error", "Error inesperado (mira app.log).")

    def _save_login(self, job, cookie):
        info = self.api.whoami(cookie)
        created = self.store.upsert(info, cookie)
        if job.finish("ok", ("Cuenta añadida: %s" if created else "Sesión actualizada: %s") % info["name"], info["name"]):
            self.refresh_avatars()

    def _quick_login(self, job):
        tok = self.api.quick_create()
        code, key = str(tok["code"]), str(tok["privateKey"])
        job.update("En un dispositivo donde ya tengas la cuenta abierta: Ajustes → Quick Log In, "
                   "escribe este código y confirma.", code)
        done = False
        try:
            deadline = time.time() + 300
            while True:
                if time.time() > deadline:
                    raise RobloxError("El código caducó. Genera otro.")
                if job.cancel.wait(2):
                    return
                status, info = self.api.quick_status(code, key)
                if status == 200 and info.get("status") == "Validated":
                    break
                if status == 429:
                    job.cancel.wait(5)
                elif status in (400, 404):
                    raise RobloxError("El código caducó o ya no es válido. Genera otro.")
            job.update("Código confirmado. Iniciando sesión…")
            cookie = self.api.quick_login(code, key)
            self._save_login(job, cookie)
            done = True
        finally:
            if not done:
                self.api.quick_cancel(code)

    def _browser_login(self, job):
        exe = find_app_browser()
        if not exe:
            raise RobloxError("Para este método hace falta Microsoft Edge o Google Chrome.")
        port = free_port()
        profile = tempfile.mkdtemp(prefix="rm-login-")
        proc = ws = None
        try:
            kwargs = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}
            proc = subprocess.Popen(
                [exe, "--user-data-dir=" + profile, "--remote-debugging-port=%d" % port,
                 "--no-first-run", "--no-default-browser-check", "--window-size=520,780",
                 "https://www.roblox.com/login"], **kwargs)
            ws = MiniWS(self._wait_cdp(port, job))
            job.update("Inicia sesión en la ventana que se ha abierto (captcha y verificación en dos pasos incluidos).")
            deadline = time.time() + 600
            cookie = None
            while cookie is None:
                if job.cancel.is_set():
                    return
                if time.time() > deadline:
                    raise RobloxError("Se agotó el tiempo (10 minutos) sin iniciar sesión.")
                try:
                    cookies = ws.call("Storage.getCookies").get("cookies", [])
                except OSError:
                    raise RobloxError("Se cerró la ventana de inicio de sesión antes de terminar.")
                value = next((c.get("value") for c in cookies
                              if c.get("name") == ".ROBLOSECURITY" and roblox_domain(c.get("domain"))
                              and c.get("value")), None)
                if value:
                    try:
                        job.update("Sesión detectada. Comprobando…")
                        self._save_login(job, value)
                        return
                    except AuthExpired:
                        pass  # cookie intermedia; seguimos esperando
                job.cancel.wait(1.5)
        finally:
            if ws:
                try:
                    ws.call("Browser.close")
                except (OSError, RobloxError):
                    pass
                ws.close()
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            for _ in range(10):  # el navegador tarda un momento en soltar los archivos
                shutil.rmtree(profile, ignore_errors=True)
                if not os.path.exists(profile):
                    break
                time.sleep(0.5)

    @staticmethod
    def _wait_cdp(port, job, timeout=25):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        end = time.time() + timeout
        while time.time() < end and not job.cancel.is_set():
            try:
                with opener.open("http://127.0.0.1:%d/json/version" % port, timeout=2) as resp:
                    return json.loads(resp.read().decode("utf-8"))["webSocketDebuggerUrl"]
            except (OSError, ValueError, KeyError):
                time.sleep(0.5)
        raise RobloxError("No se pudo abrir o controlar la ventana del navegador.")

    # -- ajustes
    def update_settings(self, body: dict):
        changes = {}
        if "delay" in body:
            try:
                changes["delay"] = max(0.0, min(120.0, float(body["delay"])))
            except (TypeError, ValueError):
                raise ValueError("La pausa tiene que ser un número de segundos.")
        if "place" in body:
            changes["place"] = str(body["place"])[:200]
        if "job" in body:
            changes["job"] = str(body["job"])[:64]
        if "theme" in body:
            if body["theme"] not in ("auto", "light", "dark"):
                raise ValueError("Tema no válido.")
            changes["theme"] = body["theme"]
        if changes:
            self.store.update_settings(**changes)

    def set_multi(self, enabled: bool):
        self.store.update_settings(multi=bool(enabled))
        with self.lock:
            if enabled:
                self.multi_active, self.multi_detail = self.plat.multi_enable()
            else:
                self.plat.multi_disable()
                self.multi_active, self.multi_detail = False, ""
            return {"active": self.multi_active, "detail": self.multi_detail}

    def add_place(self, name: str, place_text: str, game=None):
        place = parse_place(place_text)
        if place is None:
            raise ValueError("Escribe primero el ID o el enlace del juego.")
        name = name.strip()[:40] or ("Juego %d" % place)
        game = game or {}
        self.store.add_place(name, place, game.get("universe_id"), game.get("icon"))

    # -- lanzar y cerrar
    def launch(self, ids, place_text, job_text, delay, parallel=False):
        """parallel=True abre todas las cuentas a la vez, sin pausa entre ellas ni esperar a cada una."""
        place = parse_place(place_text)
        if place is None:
            raise ValueError("Elige primero un juego de la lista.")
        job = (job_text or "").strip() or None
        if job and not valid_job(job):
            raise ValueError("El servidor tiene que ser un JobId con formato xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.")
        if parallel:
            delay = 0.0
        else:
            try:
                delay = max(0.0, min(120.0, float(delay)))
            except (TypeError, ValueError):
                raise ValueError("La pausa tiene que ser un número de segundos.")
        ids = [i for i in dict.fromkeys(ids) if self.store.get(i)]
        if not ids:
            raise ValueError("Selecciona al menos una cuenta.")
        snapshot = self._process_snapshot()
        get_updaters = getattr(self.plat, "updater_pids", None)
        try:
            updater_pids = get_updaters(snapshot) if get_updaters else []
        except Exception:  # noqa: BLE001
            updater_pids = []
        if updater_pids:
            self._observe_update(snapshot)
            raise ValueError("Roblox está aplicando una actualización. Espera a que termine.")
        with self.lock:
            if self.launching():
                raise ValueError("Ya hay un lanzamiento en curso. Espera a que termine o cancélalo.")
            todo = [i for i in ids if i not in self.pids]
            skipped = len(ids) - len(todo)
            if not todo:
                raise ValueError("Esas cuentas ya tienen Roblox abierto. Ciérralo antes de volver a lanzarlas.")
            if not isinstance(self.plat, NullPlatform) and not self.multi_active and (self.known_pids or len(todo) > 1):
                raise ValueError("La multi-instancia no está activa: Roblox cerraría la partida que ya está abierta. "
                                 "Actívala arriba (cierra antes los Roblox abiertos si hace falta).")
            settings = {"place": str(place_text)[:200], "job": job or ""}
            if not parallel:  # la pausa guardada es del modo "una a una": a la vez no la toca
                settings["delay"] = delay
            self.cancel.clear()
            for i in todo:
                self.set_status(i, "En cola", "busy")
            saver = threading.Thread(target=self._save_launch_settings, args=(settings,), daemon=True)
            saver.start()  # escribir en disco no retrasa el lanzamiento

            def run():
                try:
                    if parallel:
                        self._launch_parallel(todo, place, job)
                    else:
                        self._launch_worker(todo, place, job, delay)
                finally:
                    saver.join(5)
            self.worker = threading.Thread(target=run, daemon=True)
            self.worker.start()
        return {"started": len(todo), "skipped": skipped, "parallel": bool(parallel)}

    def _launch_worker(self, ids, place, job, delay):
        try:
            for n, aid in enumerate(ids):
                if self.cancel.is_set():
                    for rest in ids[n:]:
                        self.set_status(rest, "Cancelada", "idle")
                    break
                self._launch_one(aid, place, job)
                if n != len(ids) - 1 and not self.cancel.is_set():
                    self.cancel.wait(delay)
        except Exception:  # noqa: BLE001
            log.exception("Fallo inesperado en el lanzamiento")
            for aid in ids:
                if self.status.get(aid, {}).get("tag") == "busy":
                    self.set_status(aid, "Error inesperado (mira app.log)", "bad")

    def _launch_one(self, aid, place, job):
        try:
            self.set_status(aid, "Pidiendo acceso…", "busy")
            ticket = self.api.auth_ticket(self.store.cookie(aid), place)
            before = set(self.plat.list_pids())
            self.set_status(aid, "Abriendo Roblox…", "busy")
            version_before = self._player_version()
            launch_started = time.monotonic()
            self.plat.launch(build_launch_uri(ticket, place, job))
            self.store.update_account(aid, last_launch=int(time.time()))
            pid = self._wait_new_pid(before, 15)
            with self.lock:
                self._launch_started[aid] = launch_started
                self._launch_version[aid] = version_before
                if pid:
                    self.pids[aid] = pid
                    self.status[aid] = {"text": "Abierta", "tag": "ok"}
                else:
                    self.status[aid] = {"text": "Lanzada, sin confirmar", "tag": "warn"}
            update_state = self._observe_update(self._process_snapshot(), version_before)
            if not pid and update_state:
                self.set_status(aid, "Actualización de Roblox" if update_state == "updating"
                                else "Roblox actualizado; relanza", "warn")
        except AuthExpired:
            self.set_status(aid, "Cookie caducada", "bad")
        except (RobloxError, SecretError, OSError, ValueError) as err:
            log.warning("Fallo lanzando %s: %s", aid, err)
            self.set_status(aid, "Error: %s" % err, "bad")

    def _save_launch_settings(self, settings):
        try:
            self.store.update_settings(**settings)
        except Exception:  # noqa: BLE001
            log.exception("No se pudieron guardar los ajustes del lanzamiento")

    def prewarm(self, ids):
        """Al seleccionar cuentas: pide ya el token CSRF y abre la conexión, para que «Lanzar» tarde menos."""
        now = time.time()
        todo = [i for i in ids if now - self._warm.get(i, 0) > 240]
        if not todo:
            return
        for i in todo:
            self._warm[i] = now

        def work(aid):
            try:
                self.api.warm(self.store.cookie(aid))
            except Exception as err:  # noqa: BLE001
                log.debug("Precalentamiento fallido: %s", err)  # es solo un adelanto: el lanzamiento lo hará por su cuenta

        def run():
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(todo), 8)) as pool:
                list(pool.map(work, todo))
        threading.Thread(target=run, daemon=True).start()

    PID_WAIT = 25  # segundos que se espera a que aparezcan todas las ventanas de Roblox

    def _launch_parallel(self, ids, place, job):
        """Abre todas las cuentas a la vez. Cada una pide su ticket y lanza su ventana sin esperar a las demás."""
        try:
            for aid in ids:
                self.set_status(aid, "Pidiendo acceso…", "busy")
            before = set(self.plat.list_pids())  # procesos que ya había, antes de abrir nada
            version_before = self._player_version()
            launched, lock = [], threading.Lock()  # (cuenta, trackerId): el trackerId viaja en la línea de comandos
            launch_times, launch_dates = {}, {}

            def one(aid):
                try:
                    ticket = self.api.auth_ticket(self.store.cookie(aid), place)
                except AuthExpired:
                    self.set_status(aid, "Cookie caducada", "bad")
                    return
                except (RobloxError, SecretError, OSError, ValueError) as err:
                    log.warning("Fallo pidiendo acceso para %s: %s", aid, err)
                    self.set_status(aid, "Error: %s" % err, "bad")
                    return
                if self.cancel.is_set():
                    self.set_status(aid, "Cancelada", "idle")
                    return
                tracker = str(random.randint(10_000_000_000, 99_999_999_999))
                self.set_status(aid, "Abriendo Roblox…", "busy")
                with lock:  # lanzar y anotar van juntos: el orden anotado es el orden real de las ventanas
                    try:
                        launch_times[aid] = time.monotonic()
                        self.plat.launch(build_launch_uri(ticket, place, job, tracker))
                    except (RobloxError, OSError) as err:
                        launch_times.pop(aid, None)
                        log.warning("Fallo lanzando %s: %s", aid, err)
                        self.set_status(aid, "Error: %s" % err, "bad")
                        return
                    launch_dates[aid] = int(time.time())
                    launched.append((aid, tracker))

            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(ids), 8))) as pool:
                list(pool.map(one, ids))
            self.store.update_accounts({aid: {"last_launch": stamp} for aid, stamp in launch_dates.items()})
            self._assign_pids(launched, before, version_before, launch_times)
        except Exception:  # noqa: BLE001
            log.exception("Fallo inesperado en el lanzamiento a la vez")
            for aid in ids:
                if self.status.get(aid, {}).get("tag") == "busy":
                    self.set_status(aid, "Error inesperado (mira app.log)", "bad")

    def _assign_pids(self, launched, before, version_before=None, launch_times=None):
        """Relaciona cada cuenta con su proceso de Roblox.
        Exacto si Windows deja leer la línea de comandos (lleva el trackerId); si no, por orden de aparición."""
        if not launched:
            return
        launch_times = launch_times or {}
        found, deadline = {}, time.time() + self.PID_WAIT
        new = []
        update_state = None
        while True:
            snapshot = self._process_snapshot()
            new = sorted(p for p in snapshot.get(PLAYER_EXE.lower(), []) if p not in before)
            observed = self._observe_update(snapshot, version_before)
            if observed:
                update_state = observed
            taken = set(found.values())
            for pid in new:
                if pid in taken:
                    continue
                cmd = self.plat.cmdline(pid) or ""
                for aid, tracker in launched:
                    if aid not in found and tracker in cmd:
                        found[aid] = pid
                        break
            if len(found) == len(launched) or len(new) >= len(launched) or time.time() >= deadline \
                    or self.cancel.is_set():
                break
            self.cancel.wait(0.2)
        observed = self._observe_update(self._process_snapshot(), version_before)
        if observed:
            update_state = observed
        left_accounts = [aid for aid, _t in launched if aid not in found]
        left_pids = [p for p in new if p not in found.values()]
        if left_accounts and len(left_accounts) == len(left_pids):  # sin línea de comandos: por orden de aparición
            found.update(zip(left_accounts, left_pids))
        with self.lock:
            for aid, _t in launched:
                self._launch_started[aid] = launch_times.get(aid, time.monotonic())
                self._launch_version[aid] = version_before
                if aid in found:
                    self.pids[aid] = found[aid]
                    self.status[aid] = {"text": "Abierta", "tag": "ok"}
                else:
                    if update_state == "updating":
                        self.status[aid] = {"text": "Actualización de Roblox", "tag": "warn"}
                    elif update_state == "updated":
                        self.status[aid] = {"text": "Roblox actualizado; relanza", "tag": "warn"}
                    else:
                        self.status[aid] = {"text": "Lanzada, sin confirmar", "tag": "warn"}

    def _wait_new_pid(self, before, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline and not self.cancel.is_set():
            new = [p for p in self.plat.list_pids() if p not in before]
            if new:
                return max(new)
            time.sleep(0.5)
        return None
    def close(self, ids, everything=False):
        if everything:
            self.plat.kill_all()
        else:
            for aid in ids:
                pid = self.pids.get(aid)
                if pid:
                    self.plat.kill_pid(pid)
        time.sleep(0.6)
        self.refresh_pids()


# --------------------------------------------------------------------------------------
# Icono / logo
# --------------------------------------------------------------------------------------
def draw_logo(size: int = 256):
    """Logo de la app: baldosa negra y cuadrado rojo inclinado con una L en el centro."""
    S = size * 4
    c = S // 2
    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.225), fill=(9, 9, 12, 255))
    d.rounded_rectangle((S // 128, S // 128, S - 1 - S // 128, S - 1 - S // 128), radius=int(S * 0.215),
                        outline=(255, 255, 255, 22), width=max(1, S // 170))
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    r = int(S * 0.33)
    ImageDraw.Draw(glow).ellipse((c - r, c - r, c + r, c + r), fill=(255, 40, 62, 105))
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.075))
    side = int(S * 0.54)
    x0 = (S - side) // 2
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((x0, x0, x0 + side, x0 + side), radius=int(side * 0.2), fill=255)
    t, w, h = side * 0.165, side * 0.34, side * 0.50
    left, top = c - w / 2 + side * 0.025, c - h / 2
    lpoly = [(left, top), (left + t, top), (left + t, top + h - t), (left + w, top + h - t),
             (left + w, top + h), (left, top + h)]
    grad = Image.linear_gradient("L").resize((S, S))
    logo = Image.composite(Image.new("RGBA", (S, S), (205, 0, 32, 255)),
                           Image.new("RGBA", (S, S), (255, 88, 100, 255)), grad)
    logo.putalpha(mask)
    ImageDraw.Draw(logo).polygon(lpoly, fill=(9, 9, 12, 255))
    logo = logo.rotate(-14, resample=Image.BICUBIC, center=(c, c))
    out = Image.alpha_composite(Image.alpha_composite(tile, glow), logo)
    return out.resize((size, size), Image.LANCZOS)


def save_ico(path: str) -> None:
    draw_logo(256).save(path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                                   (128, 128), (256, 256)])


# --------------------------------------------------------------------------------------
# Utilidades de interfaz
# --------------------------------------------------------------------------------------
C = {
    "bg": "#08080b", "panel": "#0f0f14", "card": "#15151b", "card2": "#1c1c24", "hover": "#1a1a22",
    "selected": "#1c1318", "line": "#23232c", "line2": "#2e2e3a", "ink": "#f4f4f7", "muted": "#9696a6",
    "faint": "#62626f", "accent": "#ff3347", "ok": "#3ddc97", "warn": "#ffc65c", "bad": "#ff6b7d",
}
RING = {"ok": "#3ddc97", "busy": "#ffc65c", "warn": "#ffc65c", "bad": "#ff6b7d", "idle": "#2e2e3a"}
BADGE = {  # (texto, fondo)
    "ok": ("#3ddc97", "#10261e"), "busy": ("#ffc65c", "#2a2311"), "warn": ("#ffc65c", "#2a2311"),
    "bad": ("#ff6b7d", "#2a1318"), "idle": ("#9696a6", "#1c1c24"),
}
FONTS = {}
SCALE = 1.0
IMG_OK = roblox_img_ok  # las pruebas pueden cambiarlo
_PHOTOS = {}
IMAGE_CACHE_LIMIT = 192


def asset_path(name: str) -> str:
    """Ruta de un recurso tanto desde el código como desde el ejecutable PyInstaller."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", name)


def px(n) -> int:
    return max(1, int(round(n * SCALE)))


def when(ts) -> str:
    if not ts:
        return "Sin usar todavía"
    now = time.time()
    s = now - ts
    d, n = time.localtime(ts), time.localtime(now)
    hm = time.strftime("%H:%M", d)
    if s < 60:
        return "Usada ahora"
    if s < 3600:
        return "Usada hace %d min" % (s // 60)
    if (d.tm_year, d.tm_yday) == (n.tm_year, n.tm_yday):
        return "Usada hoy a las " + hm
    y = time.localtime(now - 86400)
    if (d.tm_year, d.tm_yday) == (y.tm_year, y.tm_yday):
        return "Usada ayer a las " + hm
    return "Usada el " + time.strftime("%d/%m", d)


def short(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def fit_one(text: str, font, width: int) -> str:
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text + "…"


def fit_lines(text: str, font, width: int, lines: int = 2):
    words, out, cur, i = text.split(), [], "", 0
    while i < len(words) and len(out) < lines:
        trial = (cur + " " + words[i]).strip()
        if font.measure(trial) <= width:
            cur, i = trial, i + 1
            continue
        if not cur:  # una sola palabra más ancha que la línea
            cur, i = fit_one(words[i], font, width), i + 1
        out.append(cur)
        cur = ""
    if cur and len(out) < lines:
        out.append(cur)
    if i < len(words) and out:
        out[-1] = fit_one(out[-1] + "…", font, width)
    return out or [""]


def fetch_image(url: str):
    if not IMG_OK(url):
        raise RobloxError("Imagen no permitida.")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _OPENER.open(req, timeout=15) as resp:
        data = resp.read(2_000_000)
    img = Image.open(io.BytesIO(data))
    if img.width * img.height > 4_000_000:  # evita imágenes "bomba" que gastan toda la memoria
        raise RobloxError("Imagen demasiado grande.")
    img.load()
    return img


# --------------------------------------------------------------------------------------
# Gráficos (Pillow): formas con bordes suavizados
# --------------------------------------------------------------------------------------
def _rgba(h, a=255):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5)) + (a,)


def rr_image(w, h, r, fill=None, border=None, bw=1, grad=None, dot=None, dot_x=0, extra=None):
    """Rectángulo redondeado antialias: relleno liso o degradado, borde opcional, punto de color opcional."""
    S = 2
    w, h = max(1, int(w)), max(1, int(h))
    W, H = w * S, h * S
    R = int(r * S)
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    inset = 0
    if border:
        d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=_rgba(border))
        inset = int(bw * S)
    box, ir = (inset, inset, W - 1 - inset, H - 1 - inset), max(0, R - inset)
    if grad:
        g = Image.linear_gradient("L").resize((W, H))
        body = Image.composite(Image.new("RGBA", (W, H), _rgba(grad[1])), Image.new("RGBA", (W, H), _rgba(grad[0])), g)
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).rounded_rectangle(box, radius=ir, fill=255)
        im.paste(body, (0, 0), m)
    elif fill:
        d.rounded_rectangle(box, radius=ir, fill=_rgba(fill))
    if dot:
        rad = int(H * 0.11)
        cx, cy = int(dot_x * S), H // 2
        d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=_rgba(dot))
    if extra:
        extra(d, S, W, H)
    return im.resize((w, h), Image.LANCZOS)


def photo(key, make):
    p = _PHOTOS.get(key)
    if p is None:
        p = _PHOTOS[key] = ImageTk.PhotoImage(make())
    return p


def check_image(on, n):
    def tick(d, S, W, H):
        d.line([(W * .27, H * .52), (W * .43, H * .68), (W * .74, H * .32)], fill=(255, 255, 255, 255),
               width=max(2, int(S * 2.2)), joint="curve")
    if on:
        return rr_image(n, n, n * .3, grad=("#ff5568", "#e0213a"), extra=tick)
    return rr_image(n, n, n * .3, fill=C["card2"], border="#3a3a47")


def ring_pad(size):
    return max(2, size // 11)


def with_ring(im, size, ring):
    pad, S = ring_pad(size), 4
    total = size + 2 * pad
    big = Image.new("RGBA", (total * S, total * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.ellipse((0, 0, total * S - 1, total * S - 1), fill=_rgba(ring))
    rw = max(2, pad // 2) * S
    d.ellipse((rw, rw, total * S - 1 - rw, total * S - 1 - rw), fill=(0, 0, 0, 0))
    big = big.resize((total, total), Image.LANCZOS)
    big.alpha_composite(im, (pad, pad))
    return big


def shape_image(img, size: int, circle: bool = False, ring=None):
    img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    big = size * 4
    mask = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(mask)
    if circle:
        d.ellipse((0, 0, big - 1, big - 1), fill=255)
    else:
        d.rounded_rectangle((0, 0, big - 1, big - 1), radius=big // 8, fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return with_ring(out, size, ring) if ring else out


def placeholder_image(letter: str, size: int, circle: bool = False, ring=None):
    big = size * 4
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    fill = (30, 30, 38, 255)
    if circle:
        d.ellipse((0, 0, big - 1, big - 1), fill=fill)
    else:
        d.rounded_rectangle((0, 0, big - 1, big - 1), radius=big // 8, fill=fill)
    try:
        font = ImageFont.truetype("segoeuib.ttf", int(big * 0.46))
    except OSError:
        try:
            font = ImageFont.load_default(size=int(big * 0.46))
        except TypeError:
            font = ImageFont.load_default()
    d.text((big / 2, big / 2), (letter or "?")[:1].upper(), font=font, fill=(150, 150, 166, 255), anchor="mm")
    im = im.resize((size, size), Image.LANCZOS)
    return with_ring(im, size, ring) if ring else im


# --------------------------------------------------------------------------------------
# Widgets propios
# --------------------------------------------------------------------------------------
def lbl(master, text="", kind="ink", font="n", **kw):
    kw.setdefault("bg", master.cget("bg"))
    return tk.Label(master, text=text, fg=C.get(kind, kind), font=FONTS[font], **kw)


class Btn(tk.Canvas):
    STY = {
        "primary": {"fill": None, "grad": ("#ff5568", "#e0213a"), "border": None, "fg": "#ffffff",
                    "hover_grad": ("#ff7381", "#f13b52")},
        "ghost": {"fill": "#1c1c24", "border": "#2e2e3a", "fg": C["ink"], "hover_fill": "#262631"},
        "danger": {"fill": "#241217", "border": "#46202a", "fg": C["bad"], "hover_fill": "#331920"},
        "flat": {"fill": None, "border": None, "fg": C["muted"], "hover_fill": "#1c1c24"},
        "pill": {"fill": "#17171d", "border": "#2e2e3a", "fg": C["ink"], "hover_fill": "#20202a"},
    }

    def __init__(self, master, text, command=None, kind="ghost", padx=16, pady=8, font="n", radius=11,
                 min_w=0, dot=None, bg=None):
        self.cmd, self.kind, self.fontkey, self.dot = command, kind, font, dot
        self.padx, self.pady, self.radius, self.min_w = padx, pady, radius, min_w
        self.text, self.enabled, self.hover = text, True, False
        super().__init__(master, bg=bg or master.cget("bg"), highlightthickness=0, bd=0, cursor="hand2")
        self._resize()
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", self._click)

    def _resize(self):
        f = FONTS[self.fontkey]
        extra = px(18) if self.dot else 0
        self.w = max(f.measure(self.text) + 2 * px(self.padx) + extra, px(self.min_w))
        self.h = f.metrics("linespace") + 2 * px(self.pady)
        self.configure(width=self.w, height=self.h)
        self._draw()

    def _draw(self):
        self.delete("all")
        s = dict(self.STY[self.kind])
        if not self.enabled:
            s = {"fill": "#121218", "border": "#1b1b23", "fg": C["faint"], "grad": None}
        elif self.hover:
            s["fill"] = s.get("hover_fill", s.get("fill"))
            s["grad"] = s.get("hover_grad", s.get("grad"))
        if s.get("fill") or s.get("grad"):
            key = ("btn", self.w, self.h, self.radius, s.get("fill"), s.get("border"), s.get("grad"), self.dot)
            ph = photo(key, lambda: rr_image(self.w, self.h, px(self.radius), s.get("fill"), s.get("border"), 1,
                                             s.get("grad"), self.dot, px(16)))
            self.create_image(0, 0, image=ph, anchor="nw")
        tx = (self.w + (px(18) if self.dot else 0)) / 2
        self.create_text(tx, self.h / 2, text=self.text, fill=s["fg"], font=FONTS[self.fontkey])

    def _set_hover(self, v):
        self.hover = v
        self._draw()

    def _click(self, _e):
        if self.enabled and self.cmd:
            self.cmd()

    def set_text(self, text):
        if text != self.text:
            self.text = text
            self._resize()

    def set_kind(self, kind):
        if kind != self.kind:
            self.kind = kind
            self._draw()

    def set_dot(self, color):
        if color != self.dot:
            self.dot = color
            self._draw()

    def set_enabled(self, on):
        if on != self.enabled:
            self.enabled = on
            self.configure(cursor="hand2" if on else "arrow")
            self._draw()


def _rounded_canvas_items(canvas, tag, fill):
    items = [canvas.create_rectangle(0, 0, 1, 1, fill=fill, outline="", tags=(tag,)) for _ in range(2)]
    items.extend(canvas.create_oval(0, 0, 1, 1, fill=fill, outline="", tags=(tag,)) for _ in range(4))
    return items


def _draw_rounded_canvas_layer(canvas, items, x, y, w, h, radius, fill):
    r = min(radius, max(1, h // 2), max(1, w // 2))
    boxes = ((x + r, y, x + w - r, y + h), (x, y + r, x + w, y + h - r),
             (x, y, x + 2 * r, y + 2 * r), (x + w - 2 * r, y, x + w, y + 2 * r),
             (x, y + h - 2 * r, x + 2 * r, y + h),
             (x + w - 2 * r, y + h - 2 * r, x + w, y + h))
    for item, box in zip(items, boxes):
        canvas.coords(item, *box)
        canvas.itemconfigure(item, fill=fill, outline="")


class Panel(tk.Frame):
    """Contenedor con esquinas redondeadas. Los hijos se colocan en .body"""

    def __init__(self, master, fill=None, border=None, radius=16, height=None):
        super().__init__(master, bg=master.cget("bg"))
        if height:
            self.configure(height=height)
        self.fill, self.border, self.radius = fill or C["panel"], border or C["line"], px(radius)
        self.cv = tk.Canvas(self, bg=self.cget("bg"), highlightthickness=0, bd=0)
        self.cv.place(x=0, y=0, relwidth=1, relheight=1)
        self._outer = _rounded_canvas_items(self.cv, "outer", self.border)
        self._inner = _rounded_canvas_items(self.cv, "inner", self.fill)
        inset = int(self.radius * 0.3) + px(2)
        self.body = tk.Frame(self, bg=self.fill)
        self.body.place(x=inset, y=inset, relwidth=1, relheight=1, width=-2 * inset, height=-2 * inset)
        self._job, self._size = None, (0, 0)
        self.cv.bind("<Configure>", self._resized)

    def _resized(self, e):
        if (e.width, e.height) == self._size:
            return
        self._size = (e.width, e.height)
        self._redraw()

    def _redraw(self):
        w, h = self._size
        if w < 4 or h < 4:
            return
        _draw_rounded_canvas_layer(self.cv, self._outer, 0, 0, w, h, self.radius, self.border)
        inset = max(1, px(1))
        _draw_rounded_canvas_layer(self.cv, self._inner, inset, inset, w - 2 * inset, h - 2 * inset,
                                   max(1, self.radius - inset), self.fill)


class ScrollFrame(tk.Frame):
    def __init__(self, master, bg):
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview, style="Slim.Vertical.TScrollbar")
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(fill="both", expand=True, padx=(0, px(12)))
        self._bar_on = False
        self.inner.bind("<Configure>", self._inner_resized)
        self.canvas.bind("<Configure>", self._canvas_resized)

    def _update_bar(self):
        need = self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1
        if need and not self._bar_on:  # la barra flota a la derecha: no cambia el ancho del contenido
            self.vsb.place(relx=1.0, rely=0, relheight=1.0, anchor="ne", width=px(8))
            self._bar_on = True
        elif not need and self._bar_on:
            self.vsb.place_forget()
            self._bar_on = False

    def _inner_resized(self, _e):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._update_bar()

    def _canvas_resized(self, e):
        self.canvas.itemconfigure(self.win, width=e.width)
        self._update_bar()

    def scroll(self, steps):
        if self._bar_on:
            self.canvas.yview_scroll(steps, "units")


class Field(tk.Frame):
    """Campo de texto redondeado con texto de ejemplo y borde rojo al enfocar."""

    def __init__(self, master, textvariable=None, placeholder="", width=None, justify="left", height=40):
        bg = master.cget("bg")
        super().__init__(master, bg=bg, height=px(height))
        if width:
            self.configure(width=px(width))
        self.var = textvariable or tk.StringVar()
        self.cv = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.cv.place(x=0, y=0, relwidth=1, relheight=1)
        self.focused, self._size = False, (0, 0)
        self._outer = _rounded_canvas_items(self.cv, "outer", "#2e2e3a")
        self._inner = _rounded_canvas_items(self.cv, "inner", C["card"])
        self.entry = tk.Entry(self, textvariable=self.var, bd=0, relief="flat", highlightthickness=0, bg=C["card"],
                              fg=C["ink"], insertbackground=C["ink"], selectbackground=C["accent"],
                              selectforeground="#ffffff", font=FONTS["n"], justify=justify)
        self.entry.place(x=px(14), rely=0.5, anchor="w", relwidth=1, width=-px(28))
        self.ph = tk.Label(self, text=placeholder, bg=C["card"], fg=C["faint"], font=FONTS["n"], anchor="w")
        self.ph.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.entry.bind("<FocusIn>", lambda e: self._focus(True))
        self.entry.bind("<FocusOut>", lambda e: self._focus(False))
        self.var.trace_add("write", lambda *a: self._sync())
        self.cv.bind("<Configure>", self._resized)
        self._sync()

    def _sync(self):
        if self.var.get() or not self.ph.cget("text"):
            self.ph.place_forget()
        else:
            self.ph.place(x=px(14), rely=0.5, anchor="w")

    def _focus(self, on):
        self.focused = on
        self._redraw()

    def _resized(self, e):
        if (e.width, e.height) != self._size:
            self._size = (e.width, e.height)
            self._redraw()

    def _redraw(self):
        w, h = self._size
        if w < 4 or h < 4:
            return
        border = C["accent"] if self.focused else "#2e2e3a"
        _draw_rounded_canvas_layer(self.cv, self._outer, 0, 0, w, h, px(11), border)
        inset = max(1, px(1))
        _draw_rounded_canvas_layer(self.cv, self._inner, inset, inset, w - 2 * inset, h - 2 * inset,
                                   max(1, px(11) - inset), C["card"])


class ChipBar(tk.Frame):
    """Fila de botones tipo "chip" que salta de línea si no caben."""

    def __init__(self, master, on_select):
        super().__init__(master, bg=master.cget("bg"), height=px(32))
        self.on_select, self.chips, self.current = on_select, {}, None
        self.bind("<Configure>", lambda e: self.relayout(e.width))

    def set_items(self, items, current):
        for c in self.chips.values():
            c.destroy()
        self.current = current
        self.chips = {k: Btn(self, label, lambda key=k: self.pick(key), "primary" if k == current else "ghost",
                             padx=13, pady=5, font="s", radius=15) for k, label in items}
        self.relayout(self.winfo_width())

    def set_current(self, key):
        self.current = key
        for k, c in self.chips.items():
            c.set_kind("primary" if k == key else "ghost")

    def pick(self, key):
        self.set_current(key)
        self.on_select(key)

    def relayout(self, width):
        if width < 20 or not self.chips:
            return
        x = y = rowh = 0
        gap = px(8)
        for chip in self.chips.values():
            if x and x + chip.w > width:
                x, y = 0, y + rowh + gap
            chip.place(x=x, y=y)
            x, rowh = x + chip.w + gap, chip.h
        if int(self.cget("height")) != y + rowh:
            self.configure(height=y + rowh)


class AccountRow(tk.Canvas):
    H = 76

    def __init__(self, app, parent, a, idx, sel):
        self.app, self.a, self.idx, self.sel = app, a, idx, sel
        self.bgc = C["selected"] if sel else C["card"]
        super().__init__(parent, bg=C["panel"], height=px(self.H), highlightthickness=0, bd=0, cursor="hand2")
        self.b_edit = Btn(self, "✎", lambda: app.rename(a["id"], a["alias"]), "ghost", padx=10, pady=6, font="s", bg=self.bgc)
        self._bg_outer = _rounded_canvas_items(self, "background", C["line"])
        self._bg_inner = _rounded_canvas_items(self, "background", self.bgc)
        self._job, self._width, self._keep = None, 0, []
        self._rendered = False
        self.bind("<Configure>", self._on_size)
        self.bind("<Button-1>", lambda e: app.toggle(a["id"], idx, bool(e.state & 0x1)))
        self.bind("<Double-Button-1>", lambda e: app.do_launch([a["id"]]))

    def _on_size(self, e):
        if e.width == self._width:
            return
        self._width = e.width
        if self._rendered:
            self._resize_content()
        else:
            if self._job:
                self.after_cancel(self._job)
            self._job = self.after(16, self.render)

    def render(self):
        w, h, a = self._width, px(self.H), self.a
        if w < 120:
            return
        self._job = None
        self.delete("content")
        self._keep = []
        pad = px(16)
        border = C["accent"] if self.sel else C["line"]
        _draw_rounded_canvas_layer(self, self._bg_outer, 0, 0, w, h, px(16), border)
        inset = px(1)
        _draw_rounded_canvas_layer(self, self._bg_inner, inset, inset, w - 2 * inset, h - 2 * inset,
                                   max(1, px(16) - inset), self.bgc)
        n = px(22)
        self._check_item = self.create_image(pad, h // 2,
                                             image=photo(("chk", self.sel, n), lambda: check_image(self.sel, n)),
                                             anchor="w", tags=("content",))
        size = px(44)
        x = pad + n + px(14)
        self._avatar_item = self.create_image(x, h // 2, anchor="w", tags=("content",))
        self.app.set_image(a.get("avatar"), "avatar", size, a["alias"] or a["username"],
                           lambda ph, it=self._avatar_item: self._apply(it, ph), RING.get(a["tag"], RING["idle"]))
        self._button_items = [self.create_window(0, h // 2, window=b, anchor="e", tags=("content",))
                              for b in (self.b_edit,)]
        self._alias_item = self.create_text(0, h * 0.36, anchor="w", fill=C["ink"], font=FONTS["b"],
                                            tags=("content",))
        self._user_item = self.create_text(0, h * 0.68, anchor="w", fill=C["muted"], font=FONTS["s"],
                                           tags=("content",))
        self._badge_items = _rounded_canvas_items(self, "content", C["line"])
        self._status_item = self.create_text(0, h * 0.68, anchor="center", font=FONTS["sb"], tags=("content",))
        self._rendered = True
        self._resize_content()

    def _resize_content(self):
        w, h, a = self._width, px(self.H), self.a
        if w < 120:
            return
        _draw_rounded_canvas_layer(self, self._bg_outer, 0, 0, w, h, px(16),
                                   C["accent"] if self.sel else C["line"])
        inset = px(1)
        _draw_rounded_canvas_layer(self, self._bg_inner, inset, inset, w - 2 * inset, h - 2 * inset,
                                   max(1, px(16) - inset), self.bgc)
        pad, n, size = px(16), px(22), px(44)
        tx = pad + n + px(14) + size + 2 * ring_pad(size) + px(14)
        ex = w - pad
        for item, button in zip(self._button_items, (self.b_edit,)):
            self.coords(item, ex, h // 2)
            ex -= button.w + px(6)
        room = max(0, ex - tx)
        self.coords(self._alias_item, tx, h * 0.36)
        self.itemconfigure(self._alias_item, text=fit_one(a["alias"], FONTS["b"], room))
        status = a["status"] + (" · PID %d" % a["pid"] if a["tag"] == "ok" and a.get("pid") else "")
        fg, bgb = BADGE.get(a["tag"], BADGE["idle"])
        text = fit_one(status, FONTS["sb"], int(room * 0.5))
        bw, bh = FONTS["sb"].measure(text) + px(18), px(20)
        user = fit_one("@" + a["username"], FONTS["s"], max(px(40), room - bw - px(12)))
        self.coords(self._user_item, tx, h * 0.68)
        self.itemconfigure(self._user_item, text=user)
        user_box = self.bbox(self._user_item)
        bx = (user_box[2] if user_box else tx) + px(10)
        _draw_rounded_canvas_layer(self, self._badge_items, bx, h * 0.68 - bh / 2, bw, bh, bh / 2, bgb)
        self.coords(self._status_item, bx + bw / 2, h * 0.68)
        self.itemconfigure(self._status_item, text=text, fill=fg)

    def _apply(self, item, ph):
        self._keep.append(ph)
        try:
            self.itemconfigure(item, image=ph)
        except tk.TclError:
            pass


class GameCard(tk.Canvas):
    W, H = 150, 206

    def __init__(self, app, parent, g, selected, fav, favorite_ids=None):
        super().__init__(parent, width=px(self.W), height=px(self.H), bg=C["panel"], highlightthickness=0, bd=0,
                         cursor="hand2")
        self.app, self.g, self.selected, self.hover = app, g, selected, False
        w, h, pad = px(self.W), px(self.H), px(10)
        self._bg_item = self.create_image(0, 0, anchor="nw")
        size = w - 2 * pad
        icon = self.create_image(pad, pad, anchor="nw")
        app.set_image(g.get("icon"), "game", size, g["name"], lambda ph: self._apply(icon, ph))
        if favorite_ids is None:
            favorite_ids = {p["place_id"] for p in app.m.store.places()}
        is_favorite = g["place_id"] in favorite_ids
        self._star_item = self.create_image(w - pad, pad, image=app.star_image(px(24), is_favorite), anchor="ne")
        self.tag_bind(self._star_item, "<Button-1>", self._toggle_favorite)
        lines = fit_lines(g["name"], FONTS["sb"], w - 2 * px(12), 2)
        self.create_text(px(12), pad + size + px(10), anchor="nw", text="\n".join(lines), fill=C["ink"], font=FONTS["sb"])
        players = "%s jugando" % format(g["players"], ",").replace(",", ".") if g.get("players") is not None else ""
        self.create_text(px(12), h - px(12), anchor="sw", text=players, fill=C["muted"], font=FONTS["s"])
        self.restyle()
        self.bind("<Enter>", lambda e: self._hov(True))
        self.bind("<Leave>", lambda e: self._hov(False))
        self.bind("<Button-1>", lambda e: app.pick_game(g))
        if fav:
            self.bind("<Button-3>", lambda e: app.remove_fav(g))

    def _toggle_favorite(self, _event=None):
        self.app.root.after_idle(self.app.toggle_game_fav, self.g)
        return "break"

    def set_favorite(self, enabled):
        self.itemconfigure(self._star_item, image=self.app.star_image(px(24), enabled))

    def _apply(self, item, ph):
        self._ph = ph
        try:
            self.itemconfigure(item, image=ph)
        except tk.TclError:
            pass

    def _hov(self, v):
        self.hover = v
        self.restyle()

    def set_selected(self, v):
        if v != self.selected:
            self.selected = v
            self.restyle()

    def restyle(self):
        fill = C["selected"] if self.selected else (C["hover"] if self.hover else C["card"])
        border = C["accent"] if self.selected else (C["line2"] if self.hover else C["line"])
        w, h = px(self.W), px(self.H)
        ph = photo(("gc", w, h, fill, border), lambda: rr_image(w, h, px(16), fill, border, 1))
        self.itemconfigure(self._bg_item, image=ph)


# --------------------------------------------------------------------------------------
# Ventana principal
# --------------------------------------------------------------------------------------
class AddDialog:
    TABS = (("cookie", "Cookie"), ("browser", "Navegador"), ("quick", "Código rápido"))
    LABEL = {"cookie": "Añadir", "browser": "Abrir ventana de inicio de sesión", "quick": "Generar código"}

    def __init__(self, app):
        self.app, self.m = app, app.m
        self.tab, self.running, self.timer = "cookie", False, None
        self.win = win = app.make_dialog("Añadir cuentas", 660, 610)
        body = win.body
        lbl(body, "Añadir cuentas", font="h").pack(anchor="w", padx=px(28), pady=(px(24), px(14)))
        bar = tk.Frame(body, bg=C["panel"])
        bar.pack(fill="x", padx=px(28))
        self.tabbtns = {}
        for key, text in self.TABS:
            b = Btn(bar, text, lambda k=key: self.set_tab(k), "ghost", padx=14, pady=7, radius=14, min_w=150)
            b.pack(side="left", padx=(0, px(8)))
            self.tabbtns[key] = b
        panes_body = tk.Frame(body, bg=C["panel"])
        panes_body.pack(fill="both", expand=True, padx=px(28), pady=px(14))
        self.panes = {k: tk.Frame(panes_body, bg=C["panel"]) for k, _ in self.TABS}
        wrap = px(600)

        p = self.panes["cookie"]
        lbl(p, "Pega una cookie .ROBLOSECURITY por línea (también vale usuario:contraseña:cookie).", "muted",
            wraplength=wrap, justify="left").pack(anchor="w")
        box = Panel(p, fill=C["card"], border=C["line2"], radius=12, height=px(190))
        box.pack(fill="x", pady=(px(10), px(10)))
        self.txt = tk.Text(box.body, wrap="none", bg=C["card"], fg=C["ink"], insertbackground=C["ink"], relief="flat",
                           bd=0, highlightthickness=0, font=("Consolas", 9), undo=True, selectbackground=C["accent"],
                           padx=px(8), pady=px(6))
        self.txt.pack(fill="both", expand=True)
        Btn(p, "Cargar desde archivo…", self.load_file, "ghost", padx=12, pady=5, font="s").pack(anchor="w")
        self.results = lbl(p, "", "bad", wraplength=wrap, justify="left")
        self.results.pack(anchor="w", pady=(px(8), 0))
        lbl(p, "Dónde está la cookie: F12 en el navegador → Aplicación → Cookies → roblox.com → .ROBLOSECURITY. "
               "Quien la tenga controla la cuenta: aquí se guarda cifrada y no se vuelve a mostrar.", "faint", font="s",
            wraplength=wrap, justify="left").pack(anchor="w", pady=(px(6), 0))

        p = self.panes["browser"]
        lbl(p, "Se abre una ventana limpia de Edge/Chrome (sin tus datos ni tus sesiones). Inicia sesión como siempre, "
               "con captcha y verificación en dos pasos si te los pide, y la cuenta se añade sola al terminar.", "muted",
            wraplength=wrap, justify="left").pack(anchor="w", pady=(px(4), px(18)))
        self.msg_browser = lbl(p, "", "muted", wraplength=wrap)
        self.msg_browser.pack(pady=px(8))

        p = self.panes["quick"]
        lbl(p, "Genera un código y escríbelo en un dispositivo donde esa cuenta ya tenga la sesión abierta "
               "(Ajustes → Quick Log In). No hace falta contraseña ni cookie.", "muted",
            wraplength=wrap, justify="left").pack(anchor="w", pady=(px(4), px(12)))
        cbox = Panel(p, fill=C["card"], border=C["line2"], radius=14, height=px(96))
        cbox.pack(fill="x", pady=px(8))
        self.code = tk.Label(cbox.body, text="······", font=("Consolas", 34, "bold"), fg=C["ink"], bg=C["card"])
        self.code.pack(expand=True)
        self.msg_quick = lbl(p, "", "muted", wraplength=wrap)
        self.msg_quick.pack(pady=px(8))

        foot = tk.Frame(body, bg=C["panel"])
        foot.pack(fill="x", padx=px(28), pady=(0, px(24)))
        self.ok = Btn(foot, self.LABEL["cookie"], self.submit, "primary", padx=20, pady=9, font="b")
        self.ok.pack(side="right")
        Btn(foot, "Cancelar", self.close, "ghost", padx=18, pady=9).pack(side="right", padx=(0, px(10)))
        win.protocol("WM_DELETE_WINDOW", self.close)
        self.set_tab("cookie")
        self.txt.focus_set()

    def set_tab(self, tab):
        if self.running:
            return
        self.tab = tab
        for k, b in self.tabbtns.items():
            b.set_kind("primary" if k == tab else "ghost")
            if k == tab:
                self.panes[k].pack(fill="both", expand=True)
            else:
                self.panes[k].pack_forget()
        self.ok.set_text(self.LABEL[tab])
        self.ok.set_enabled(True)

    def load_file(self):
        path = filedialog.askopenfilename(parent=self.win, title="Cargar cookies",
                                          filetypes=[("Texto", "*.txt *.csv *.log"), ("Todos", "*.*")])
        if not path:
            return
        try:
            if os.path.getsize(path) > 2_000_000:
                raise OSError("El archivo es demasiado grande.")
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read()
        except OSError as err:
            self.results.configure(text=str(err))
            return
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", data)

    def submit(self):
        if self.tab == "cookie":
            text = self.txt.get("1.0", "end")
            self.ok.set_enabled(False)
            self.ok.set_text("Comprobando…")
            self.app.bg(lambda: self.m.add_accounts(text), lambda res, err: self._added(text, res, err))
        else:
            msg = self.msg_quick if self.tab == "quick" else self.msg_browser
            try:
                self.m.start_login(self.tab)
            except ValueError as err:
                msg.configure(text=str(err), fg=C["bad"])
                return
            self.running = True
            self.ok.set_text("Esperando…")
            self.ok.set_enabled(False)
            msg.configure(text="Iniciando…", fg=C["muted"])
            self.timer = self.win.after(1000, self.poll)

    def _added(self, text, results, err):
        if not self.win.winfo_exists():
            return
        self.ok.set_text(self.LABEL["cookie"])
        self.ok.set_enabled(True)
        if err:
            self.results.configure(text=str(err) if isinstance(err, ValueError) else "Error inesperado (mira app.log).")
            return
        good = [r for r in results if r["ok"]]
        bad = [r for r in results if not r["ok"]]
        if good:
            self.app.clear_cookie_clipboard()
            self.app.toast("Añadida la cuenta %s." % good[0]["username"] if len(good) == 1
                           else "Añadidas %d cuentas." % len(good))
        self.app.refresh()
        if bad:
            lines = text.splitlines()
            self.txt.delete("1.0", "end")
            self.txt.insert("1.0", "\n".join(lines[r["line"] - 1] for r in bad))
            self.results.configure(text="\n".join("Línea %d: %s" % (r["line"], r["error"]) for r in bad))
        else:
            self.close()

    def poll(self):
        if not self.win.winfo_exists():
            return
        r = self.m.login_state()
        msg = self.msg_quick if self.tab == "quick" else self.msg_browser
        state = r.get("state")
        if r.get("code"):
            self.code.configure(text=r["code"])
        if state in ("waiting", "ok", "error"):
            msg.configure(text=r.get("message", ""), fg=C["bad"] if state == "error" else C["muted"])
        if state == "ok":
            self.running = False
            self.app.toast(r.get("message") or "Cuenta añadida.")
            self.app.refresh()
            self.close()
            return
        if state in ("error", "cancelled"):
            self.running = False
            self.ok.set_text(self.LABEL[self.tab])
            self.ok.set_enabled(True)
            return
        self.timer = self.win.after(1000, self.poll)

    def close(self):
        if self.running:
            self.m.cancel_login()
        if self.timer:
            try:
                self.win.after_cancel(self.timer)
            except tk.TclError:
                pass
        self.app.add_dialog = None
        self.win.destroy()


class App:
    def __init__(self, manager):
        global SCALE
        self.m = manager
        self.root = root = tk.Tk()
        root.withdraw()
        try:
            SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            SCALE = 1.0
        root.title(APP_TITLE)
        root.configure(bg=C["bg"])
        if IS_WINDOWS:
            try:
                root.attributes("-alpha", 0.0)
            except tk.TclError:
                pass
        root.geometry("%dx%d" % (px(1220), px(860)))
        root.minsize(px(1000), px(740))
        for key, size, weight in (("n", 10, "normal"), ("b", 10, "bold"), ("s", 9, "normal"), ("sb", 9, "bold"),
                                  ("h", 17, "bold"), ("t", 13, "bold")):
            FONTS[key] = tkfont.Font(root=root, family="Segoe UI", size=size, weight=weight)
        self._logo_source = draw_logo(256)
        self.icons = [ImageTk.PhotoImage(self._logo_source if n == 256 else
                                         self._logo_source.resize((n, n), Image.Resampling.LANCZOS))
                      for n in (256, 64, 32)]
        root.iconphoto(True, *self.icons)
        self.q = queue.Queue()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=6)
        self.photos, self._pending_photos = OrderedDict(), {}
        self.sel, self.anchor = set(), None
        self.star_photos = {}
        root.report_callback_exception = self._report_callback_exception
        self.state, self.acc_sig, self.places_sig = None, None, None
        self.greq, self.sorts, self.cat_id, self.sorts_error = 0, [], None, ""
        self.game_cards, self.sel_game, self.add_dialog = [], None, None
        self._fav_icon_loading = False
        self.toast_job = self.save_job = self.search_job = self.layout_job = None
        self._suppress_search = False
        self.fav_confirm = None
        self._build_style()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind_all("<MouseWheel>", self._wheel)
        root.update_idletasks()
        self.tick()
        root.deiconify()
        self.dark_titlebar(root)
        if IS_WINDOWS:
            root.after(35, lambda: root.attributes("-alpha", 1.0) if root.winfo_exists() else None)
        self._pump()
        self.bg(self.m.game_sorts, self._sorts_done)
        self._seen_update_notice = 0

    def _report_callback_exception(self, exc, value, tb):
        log.error("Excepción en evento de interfaz", exc_info=(exc, value, tb))

    def star_image(self, size, filled=False):
        key = (size, bool(filled))
        cached = self.star_photos.get(key)
        if cached is not None:
            return cached
        high = max(4, size * 4)
        if filled:
            im = Image.new("RGBA", (high, high), (0, 0, 0, 0))
            cx = cy = high / 2
            points = []
            for i in range(10):
                angle = -math.pi / 2 + i * math.pi / 5
                radius = high * (0.47 if i % 2 == 0 else 0.205)
                points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
            draw = ImageDraw.Draw(im)
            draw.polygon(points, fill="#ffcc33")
            draw.line(points + [points[0]], fill="#ffdf70", width=max(3, high // 28), joint="curve")
        else:
            im = Image.open(asset_path("star.png")).convert("RGBA").resize((high, high), Image.LANCZOS)
        im = im.resize((size, size), Image.LANCZOS)
        cached = ImageTk.PhotoImage(im)
        self.star_photos[key] = cached
        return cached

    # -- estilo
    def _build_style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.layout("Slim.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        st.configure("Slim.Vertical.TScrollbar", background="#2a2a35", troughcolor=C["panel"], bordercolor=C["panel"],
                     lightcolor="#2a2a35", darkcolor="#2a2a35", relief="flat", arrowsize=px(8), gripcount=0)
        st.map("Slim.Vertical.TScrollbar", background=[("active", "#3c3c4a"), ("pressed", C["accent"])])

    def dark_titlebar(self, win):
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            hwnd = user32.GetParent(wintypes.HWND(win.winfo_id()))
            dwmapi = ctypes.windll.dwmapi
            dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            for attr in (20, 19):
                value = ctypes.c_int(1)
                dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
            # Keep Windows' native caption controls and resize handling, while
            # blending the caption text/border into the app's dark background.
            caption = ctypes.c_int(0x000B0808)  # COLORREF #08080b
            text = ctypes.c_int(0x000B0808)
            for attr, color in ((34, caption), (35, caption), (36, text)):
                dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(color), ctypes.sizeof(color))
            # WM_SETICON/ICON_SMALL hides the caption icon; ICON_BIG remains for the taskbar.
            user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.SendMessageW(hwnd, 0x0080, 0, 0)
        except Exception:  # noqa: BLE001
            log.exception("No se pudo personalizar la barra de título")

    def make_dialog(self, title, w, h=None):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.overrideredirect(True)
        win.configure(bg="#101014")
        win.transient(self.root)
        win.resizable(False, False)
        win.iconphoto(False, *self.icons)
        self.root.update_idletasks()
        width, height = px(w), px(h or 300)
        root_x, root_y = self.root.winfo_rootx(), self.root.winfo_rooty()
        root_w, root_h = max(1, self.root.winfo_width()), max(1, self.root.winfo_height())
        win.geometry("%dx%d+%d+%d" % (root_w, root_h, root_x, root_y))
        try:
            self.root.update_idletasks()
            backdrop = ImageGrab.grab(bbox=(root_x, root_y, root_x + root_w, root_y + root_h)).convert("RGB")
            backdrop = backdrop.resize((root_w, root_h), Image.Resampling.LANCZOS)
            backdrop = Image.blend(backdrop, Image.new("RGB", (root_w, root_h), C["bg"]), 0.38)
            backdrop = backdrop.filter(ImageFilter.GaussianBlur(radius=max(1, px(1))))
            win.backdrop = ImageTk.PhotoImage(backdrop)
            shade = tk.Label(win, image=win.backdrop, bg=C["bg"], bd=0, highlightthickness=0)
        except Exception:  # noqa: BLE001
            log.exception("No se pudo capturar el fondo del diálogo")
            win.backdrop = None
            shade = tk.Frame(win, bg=C["bg"])
        shade.place(x=0, y=0, relwidth=1, relheight=1)
        panel = Panel(shade, fill=C["panel"], border=C["line2"], radius=16)
        panel.place(relx=0.5, rely=0.5, anchor="center", width=width, height=height)
        win.dialog_panel = panel
        win.body = panel.body
        win.update_idletasks()
        win.lift()
        return win

    def _center(self, w, h):
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 3
        return "%dx%d+%d+%d" % (w, h, max(0, x), max(0, y))

    def ask(self, title, text, buttons, width=460):
        """Diálogo con botones propios. buttons: [(texto, valor, estilo)] de izquierda a derecha. Devuelve el valor."""
        win = self.make_dialog(title, width)
        body = win.body
        out = {"v": None}
        lbl(body, title, "ink", "t").pack(anchor="w", padx=px(28), pady=(px(24), px(8)))
        lbl(body, text, "muted", wraplength=px(width - 56), justify="left").pack(anchor="w", padx=px(28))
        row = tk.Frame(body, bg=C["panel"])
        row.pack(fill="x", padx=px(28), pady=px(24))

        def pick(v):
            out["v"] = v
            win.destroy()
        for label, value, kind in reversed(buttons):
            Btn(row, label, lambda v=value: pick(v), kind, padx=16, pady=8).pack(side="right", padx=(px(8), 0))
        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        panel = win.dialog_panel
        panel.place_configure(height=body.winfo_reqheight() + px(16))
        self.root.wait_window(win)
        return out["v"]

    def ask_async(self, title, text, buttons, on_result, width=460):
        """Muestra una confirmación sin detener el ciclo de eventos de la ventana principal."""
        win = self.make_dialog(title, width)
        body = win.body
        lbl(body, title, "ink", "t").pack(anchor="w", padx=px(28), pady=(px(24), px(8)))
        lbl(body, text, "muted", wraplength=px(width - 56), justify="left").pack(anchor="w", padx=px(28))
        row = tk.Frame(body, bg=C["panel"])
        row.pack(fill="x", padx=px(28), pady=px(24))
        panel = win.dialog_panel

        def finish(value=None):
            if not win.winfo_exists():
                return
            win.destroy()
            on_result(value)

        for label, value, kind in reversed(buttons):
            Btn(row, label, lambda v=value: finish(v), kind, padx=16, pady=8).pack(side="right", padx=(px(8), 0))
        win.bind("<Escape>", lambda _e: finish())
        win.update_idletasks()
        panel.place_configure(height=body.winfo_reqheight() + px(16))

    def ask_text(self, title, prompt, initial=""):
        win = self.make_dialog(title, 440, 190)
        body = win.body
        out = {"v": None}
        lbl(body, prompt, "ink", "b").pack(anchor="w", padx=px(28), pady=(px(26), px(10)))
        var = tk.StringVar(value=initial)
        f = Field(body, var)
        f.pack(fill="x", padx=px(28))
        f.entry.focus_set()
        f.entry.select_range(0, "end")

        def ok(_e=None):
            out["v"] = var.get().strip()
            win.destroy()
        row = tk.Frame(body, bg=C["panel"])
        row.pack(fill="x", padx=px(28), pady=px(22))
        Btn(row, "Guardar", ok, "primary", padx=18, pady=8).pack(side="right")
        Btn(row, "Cancelar", win.destroy, "ghost", padx=16, pady=8).pack(side="right", padx=(0, px(10)))
        f.entry.bind("<Return>", ok)
        f.entry.bind("<Escape>", lambda _e: win.destroy())
        self.root.wait_window(win)
        return out["v"]

    # -- hilos y avisos
    def bg(self, fn, done=None):
        def run():
            try:
                res, err = fn(), None
            except Exception as exc:  # noqa: BLE001
                if not isinstance(exc, (RobloxError, ValueError, SecretError)):
                    log.exception("Error en segundo plano")
                res, err = None, exc
            if done:
                self.q.put((done, res, err))
        threading.Thread(target=run, daemon=True).start()

    def _pump(self):
        try:
            while True:
                cb, res, err = self.q.get_nowait()
                try:
                    cb(res, err)
                except Exception:  # noqa: BLE001
                    log.exception("Error en callback de interfaz")
        except queue.Empty:
            pass
        self.root.after(40, self._pump)

    def _hide_toast(self):
        self.toast_job = None
        try:
            self.toast_cv.place_forget()
            self.toast_cv.delete("all")
        except tk.TclError:
            pass

    def toast(self, text, bad=False, duration=None):
        if self.toast_job:
            try:
                self.root.after_cancel(self.toast_job)
            except tk.TclError:
                pass
            self.toast_job = None
        cv = self.toast_cv
        cv.delete("all")
        t = cv.create_text(px(20), px(14), anchor="nw", text=text, fill="#ffffff" if bad else C["ink"],
                           font=FONTS["b"], width=px(620))
        x1, y1, x2, y2 = cv.bbox(t)
        w, h = (x2 - x1) + px(40), (y2 - y1) + px(28)
        cv.configure(width=w, height=h)
        self._toast_img = ImageTk.PhotoImage(rr_image(w, h, px(14), "#5a1620" if bad else "#1d1d26",
                                                      "#ff6b7d" if bad else C["accent"]))
        cv.create_image(0, 0, image=self._toast_img, anchor="nw")
        cv.tag_raise(t)
        cv.place(relx=0.5, rely=1.0, y=-px(168), anchor="s")
        self.toast_job = self.root.after(duration if duration is not None else (8000 if bad else 3000),
                                         self._hide_toast)
        # Canvas.tkraise() eleva elementos del dibujo; esta orden Tcl eleva el widget.
        cv.tk.call("raise", cv._w)
    def _wheel(self, e):
        w = self.root.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            if isinstance(w, ScrollFrame):
                w.scroll(-3 if e.delta > 0 else 3)
                return
            w = getattr(w, "master", None)

    # -- imágenes
    def _cache_photo(self, key, image):
        self.photos[key] = image
        self.photos.move_to_end(key)
        while len(self.photos) > IMAGE_CACHE_LIMIT:
            self.photos.popitem(last=False)

    def set_image(self, url, kind, size, letter, apply, ring=None):
        circle = kind == "avatar"
        key = (url, kind, size, ring)
        cached = self.photos.get(key)
        if cached is not None:
            self.photos.move_to_end(key)
            apply(cached)
            return
        phk = ("ph", letter, kind, size, ring)
        if phk not in self.photos:
            self._cache_photo(phk, ImageTk.PhotoImage(placeholder_image(letter, size, circle, ring)))
        else:
            self.photos.move_to_end(phk)
        apply(self.photos[phk])
        if not url:
            return
        if key in self._pending_photos:
            self._pending_photos[key].append(apply)
            return
        self._pending_photos[key] = [apply]

        def done(img, err):
            callbacks = self._pending_photos.pop(key, [])
            if err or img is None:
                return
            ph = self.photos.get(key)
            if ph is None:
                ph = ImageTk.PhotoImage(img)
                self._cache_photo(key, ph)
            else:
                self.photos.move_to_end(key)
            for callback in callbacks:
                try:
                    callback(ph)
                except tk.TclError:
                    pass

        def run():
            try:
                res, err = shape_image(fetch_image(url), size, circle, ring), None
            except Exception as exc:  # noqa: BLE001
                res, err = None, exc
            self.q.put((done, res, err))
        self.pool.submit(run)

    # -- construcción
    def _build(self):
        root = self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(2, weight=1)
        pad = px(26)

        head = tk.Frame(root, bg=C["bg"])
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(px(14), px(12)))
        logo_size = px(48)
        self._logo = ImageTk.PhotoImage(self._logo_source.resize((logo_size, logo_size), Image.Resampling.LANCZOS))
        tk.Label(head, bg=C["bg"], image=self._logo).pack(side="left")
        titles = tk.Frame(head, bg=C["bg"])
        titles.pack(side="left", padx=(px(14), 0))
        lbl(titles, APP_TITLE, "ink", "h").pack(anchor="w")
        lbl(titles, "Cuentas de Roblox · multi-instancia", "muted", "s").pack(anchor="w")
        Btn(head, "Cerrar todas las instancias", self.close_all, "danger", padx=14, pady=8).pack(side="right")
        self.count_lbl = lbl(head, "", "muted")
        self.count_lbl.pack(side="right", padx=px(18))
        self.multi_btn = Btn(head, "Multi-instancia", self.toggle_multi, "pill", padx=14, pady=8, radius=17,
                             dot=C["ok"])
        self.multi_btn.pack(side="right")

        self.notice = Panel(root, fill="#211a0e", border="#4a3a14", radius=12, height=px(62))
        self.notice_lbl = lbl(self.notice.body, "", "warn", wraplength=px(720), justify="left", bg="#211a0e")
        self.notice_lbl.pack(side="left", padx=px(10))
        nb = tk.Frame(self.notice.body, bg="#211a0e")
        nb.pack(side="right", padx=px(4))
        Btn(nb, "Reintentar", self.retry_multi, "primary", padx=14, pady=6, font="s").pack(side="left", padx=(0, px(8)))
        Btn(nb, "Cerrar todos", self.close_all, "ghost", padx=14, pady=6, font="s").pack(side="left")
        self.notice_grid = dict(row=1, column=0, sticky="ew", padx=pad, pady=(0, px(12)))

        main = tk.Frame(root, bg=C["bg"])
        main.grid(row=2, column=0, sticky="nsew", padx=pad)
        main.grid_columnconfigure(0, weight=5, uniform="cols", minsize=px(470))
        main.grid_columnconfigure(1, weight=6, uniform="cols")
        main.grid_rowconfigure(0, weight=1)

        # cuentas
        left = Panel(main)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, px(7)))
        lb = left.body
        lh = tk.Frame(lb, bg=C["panel"])
        lh.pack(fill="x", padx=px(18), pady=(px(18), px(12)))
        lbl(lh, "Cuentas", "ink", "t").pack(side="left")
        self.acc_count = lbl(lh, "", "faint")
        self.acc_count.pack(side="left", padx=(px(8), 0))
        Btn(lh, "+ Añadir", self.open_add, "primary", padx=16, pady=7, font="b").pack(side="right")
        self.btn_verify = Btn(lh, "Verificar", self.verify, "ghost", padx=12, pady=7, font="s")
        self.btn_verify.pack(side="right", padx=(0, px(8)))
        self.btn_selall = Btn(lh, "Todas", self.select_all, "ghost", padx=12, pady=7, font="s")
        self.btn_selall.pack(side="right", padx=(0, px(8)))
        self.acc_sf = ScrollFrame(lb, C["panel"])
        self.acc_sf.pack(fill="both", expand=True, padx=(px(18), px(6)), pady=(0, px(16)))

        # juegos
        right = Panel(main)
        right.grid(row=0, column=1, sticky="nsew", padx=(px(7), 0))
        rb = right.body
        rh = tk.Frame(rb, bg=C["panel"])
        rh.pack(fill="x", padx=px(18), pady=(px(18), px(12)))
        lbl(rh, "Juegos", "ink", "t").pack(side="left")
        self.search_var = tk.StringVar()
        self.search = Field(rb, self.search_var, "Buscar un juego…")
        self.search.pack(fill="x", padx=px(18))
        self.search_var.trace_add("write", lambda *a: self.on_search_typed())
        self.search.entry.bind("<Return>", self._search_enter)
        self.chips = ChipBar(rb, self.on_category)
        self.chips.pack(fill="x", padx=px(18), pady=(px(12), 0))
        self.gmsg = lbl(rb, "", "muted", "s", bg=C["panel"], wraplength=px(560), justify="left")
        self.gmsg.pack(anchor="w", padx=px(20), pady=(px(8), 0))
        self.games_sf = ScrollFrame(rb, C["panel"])
        self.games_sf.pack(fill="both", expand=True, padx=(px(14), px(6)), pady=(px(6), px(16)))
        self.games_sf.canvas.bind("<Configure>", self._games_resized, add="+")

        # pie: servidor (opcional) + acciones
        foot = Panel(root, height=px(84))  # sin altura el panel quedaba en 0 px y el pie no se veía
        foot.grid(row=3, column=0, sticky="ew", padx=pad, pady=(px(12), px(16)))
        r1 = tk.Frame(foot.body, bg=C["panel"])
        r1.pack(fill="x", padx=px(18), pady=(px(14), px(14)))
        self.place_var, self.job_var = tk.StringVar(), tk.StringVar()
        st = self.m.store.settings()
        self.place_var.set(st.get("place", ""))
        self.job_var.set(st.get("job", ""))
        for v in (self.place_var, self.job_var):
            v.trace_add("write", self._settings_changed)
        self.btn_go = Btn(r1, "Lanzar", self.go, "primary", padx=30, pady=11, font="b", min_w=170, radius=13)
        self.btn_go.pack(side="right")
        self.btn_closesel = Btn(r1, "Cerrar seleccionadas", self.close_selected, "ghost", pady=10)
        self.btn_closesel.pack(side="right", padx=(0, px(8)))
        self.btn_del = Btn(r1, "Eliminar", self.delete_selected, "danger", pady=10)
        self.btn_del.pack(side="right", padx=(0, px(8)))
        # el campo del servidor se empaqueta el último: si falta sitio se recorta él, no los botones
        Field(r1, self.job_var, "Servidor privado / JobId (opcional)", width=290).pack(side="left")

        self.toast_cv = tk.Canvas(root, bg=C["bg"], highlightthickness=0, bd=0)

    # -- estado periódico
    def refresh(self):
        self.tick(reschedule=False)

    def tick(self, reschedule=True):
        try:
            self.apply_state(self.m.state())
        except Exception:  # noqa: BLE001
            log.exception("Error actualizando la interfaz")
        if reschedule:
            self.root.after(1500, self.tick)

    def apply_state(self, st):
        self.state = st
        update_notice = st.get("update_notice")
        if update_notice and update_notice["id"] != getattr(self, "_seen_update_notice", 0):
            self._seen_update_notice = update_notice["id"]
            self.toast(update_notice["text"], duration=8000)
        ids = {a["id"] for a in st["accounts"]}
        self.sel &= ids
        multi = st["multi"]
        on = multi["active"]
        self.multi_btn.set_text("Multi-instancia %s" % ("activa" if on else "inactiva"))
        self.multi_btn.set_dot(C["ok"] if on else C["bad"])
        n_open = st["roblox_count"]
        self.count_lbl.configure(text="%d instancia%s abierta%s" % (n_open, "" if n_open == 1 else "s", "" if n_open == 1 else "s"))
        want = st["settings"].get("multi", True)
        if want and not on and multi["detail"]:
            self.notice_lbl.configure(text=multi["detail"])
            self.notice.grid(**self.notice_grid)
        else:
            self.notice.grid_remove()
        self.acc_count.configure(text="%d" % len(st["accounts"]) if st["accounts"] else "")
        sig = (tuple((a["id"], a["alias"], a["username"], a["status"], a["tag"], a["pid"], a.get("avatar"),
                      when(a["last_launch"]), a["id"] in self.sel) for a in st["accounts"]), st["launching"])
        if sig != self.acc_sig:
            self.acc_sig = sig
            self.render_accounts()
        self.update_footer()
        psig = tuple((p["place_id"], p["name"]) for p in st["places"])
        if psig != self.places_sig:
            self.places_sig = psig
            if self.cat_id == "fav":
                self.show_favs()
            else:
                self.update_favorite_stars()

    def update_favorite_stars(self):
        favorite_ids = {p["place_id"] for p in self.m.store.places()}
        for card in self.game_cards:
            card.set_favorite(card.g["place_id"] in favorite_ids)

    # -- cuentas
    def render_accounts(self):
        st = self.state
        sf = self.acc_sf
        y = sf.canvas.yview()[0]
        for child in sf.inner.winfo_children():
            child.destroy()
        accounts = st["accounts"]
        if not accounts:
            box = tk.Frame(sf.inner, bg=C["panel"])
            box.pack(fill="x", pady=px(48))
            lbl(box, "Aún no tienes cuentas", "ink", "t", bg=C["panel"]).pack()
            lbl(box, "Añade una con su cookie, iniciando sesión en una ventana\no con un código rápido.", "muted",
                bg=C["panel"], justify="center").pack(pady=(px(8), px(16)))
            Btn(box, "+ Añadir cuenta", self.open_add, "primary", padx=20, pady=9, font="b").pack()
        for idx, a in enumerate(accounts):
            AccountRow(self, sf.inner, a, idx, a["id"] in self.sel).pack(fill="x", pady=(0, px(8)), padx=(0, px(2)))
        self.btn_selall.set_text("Ninguna" if accounts and len(self.sel) == len(accounts) else "Todas")
        self.btn_verify.set_enabled(bool(accounts))
        sf.inner.update_idletasks()
        sf.canvas.yview_moveto(y)

    def toggle(self, aid, idx, shift):
        accounts = self.state["accounts"]
        if shift and self.anchor is not None:
            a, b = sorted((self.anchor, idx))
            for acc in accounts[a:b + 1]:
                self.sel.add(acc["id"])
        else:
            self.sel.symmetric_difference_update({aid})
            self.anchor = idx
        self.acc_sig = None
        self.refresh()
        self.m.prewarm(list(self.sel))

    def select_all(self):
        ids = {a["id"] for a in self.state["accounts"]}
        self.sel = set() if self.sel == ids else ids
        self.acc_sig = None
        self.refresh()
        self.m.prewarm(list(self.sel))

    def rename(self, aid, current):
        name = self.ask_text("Cambiar nombre", "Nombre de la cuenta", current)
        if name and name != current:
            try:
                self.m.rename(aid, name)
            except ValueError as err:
                self.toast(str(err), True)
        self.refresh()

    def delete_selected(self):
        n = len(self.sel)
        if not n:
            return
        what = "esta cuenta" if n == 1 else "%d cuentas" % n
        answer = self.ask("Eliminar cuentas", "¿Eliminar %s de %s?\n\nSi cierras también la sesión en Roblox, la cookie "
                          "guardada deja de servir (es lo más seguro). Tu cuenta de Roblox no se borra." % (what, APP_TITLE),
                          [("Cancelar", None, "ghost"), ("Solo eliminar", "local", "ghost"),
                           ("Eliminar y cerrar sesión", "revoke", "primary")], width=520)
        if answer is None:
            return
        ids = list(self.sel)
        self.sel.clear()

        def done(res, err):
            if err or not res:
                self.toast("No se pudo eliminar.", True)
            elif answer == "revoke" and res["failed"]:
                self.toast("Eliminadas, pero no pude cerrar la sesión en %d. Ciérrala tú desde roblox.com → Ajustes." %
                           res["failed"], True)
            elif answer == "revoke":
                self.toast("Eliminadas y sesión cerrada en Roblox.")
            self.acc_sig = None
            self.refresh()
        self.bg(lambda: self.m.delete(ids, answer == "revoke"), done)

    def verify(self):
        try:
            self.m.verify(list(self.sel))
        except ValueError as err:
            if "actualización de roblox" not in str(err).lower():
                self.toast(str(err), True)
        self.refresh()

    def clear_cookie_clipboard(self):
        """Si el portapapeles aún tiene una cookie copiada, lo vacía."""
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        if "_|WARNING" not in text:
            return
        if IS_WINDOWS:
            try:
                import ctypes
                u = ctypes.windll.user32
                if u.OpenClipboard(None):
                    u.EmptyClipboard()
                    u.CloseClipboard()
            except Exception:  # noqa: BLE001
                pass
        else:
            self.root.clipboard_clear()

    def open_add(self):
        if not self.add_dialog:
            self.add_dialog = AddDialog(self)

    # -- pie
    def update_footer(self):
        st = self.state
        n, busy = len(self.sel), st["launching"]
        self.btn_go.set_text("Detener" if busy else ("Lanzar instancias seleccionadas" if n > 1 else "Lanzar"))
        self.btn_go.set_kind("danger" if busy else "primary")
        self.btn_go.set_enabled(busy or n > 0)
        self.btn_closesel.set_enabled(not busy and any(a["tag"] == "ok" and a["id"] in self.sel for a in st["accounts"]))
        self.btn_del.set_enabled(not busy and n > 0)

    def go(self):
        if self.state and self.state["launching"]:
            self.m.cancel.set()
            self.refresh()
            return
        self.do_launch(list(self.sel))

    def do_launch(self, ids):
        """Abre todas las cuentas a la vez: sin pausas entre ellas ni esperar a cada una."""
        try:
            r = self.m.launch(ids, self.place_var.get(), self.job_var.get(), 0, parallel=True)
            if r["started"] > 1 and not r["skipped"]:
                self.toast("Abriendo %d cuentas a la vez…" % r["started"])
            if r["skipped"]:
                self.toast("%d cuenta%s ya estaba%s abierta%s y no se ha tocado." % (
                    r["skipped"], "" if r["skipped"] == 1 else "s", "" if r["skipped"] == 1 else "n",
                    "" if r["skipped"] == 1 else "s"))
        except ValueError as err:
            self.toast(str(err), True)
        self.refresh()

    def close_ids(self, ids):
        self.bg(lambda: self.m.close(ids), lambda r, e: self.refresh())

    def close_selected(self):
        self.close_ids(list(self.sel))

    def close_all(self):
        def confirmed(answer):
            if answer:
                self.bg(lambda: self.m.close([], True), lambda r, e: self.refresh())

        self.ask_async("Cerrar todas las instancias", "¿Cerrar todas las instancias de Roblox abiertas, incluidas las que no se lanzaron "
                       "desde aquí?", [("Cancelar", None, "ghost"), ("Cerrar todos", "yes", "danger")], confirmed)

    def toggle_multi(self):
        on = bool(self.state and self.state["multi"]["active"])
        self.bg(lambda: self.m.set_multi(not on), lambda r, e: self.refresh())

    def retry_multi(self):
        self.bg(lambda: self.m.set_multi(True), lambda r, e: self.refresh())

    def _settings_changed(self, *_a):
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(500, self._save_settings)
        self._place_changed()

    def _save_settings(self):
        try:
            self.m.update_settings({"place": self.place_var.get(), "job": self.job_var.get()})
        except ValueError:
            pass

    # -- juegos
    def _sorts_done(self, res, err):
        sorts = res["sorts"] if res else []
        self.sorts_error = ""
        if err:
            self.sorts_error = str(err) if isinstance(err, RobloxError) else "No pude cargar las categorías."
            self.root.after(15000, lambda: self.bg(self.m.game_sorts, self._sorts_done))
        self.sorts = sorts
        self.cat_id = sorts[0]["id"] if sorts else "fav"
        searching = bool(self.search_var.get().strip())
        self.chips.set_items([("fav", "★ Favoritos")] + [(s["id"], s["name"]) for s in sorts],
                             None if searching else self.cat_id)
        if not searching:
            self.load_category()

    def on_category(self, cat_id):
        self.cat_id = cat_id
        self._cancel_search()
        self._suppress_search = True
        try:
            self.search_var.set("")
        finally:
            self._suppress_search = False
        self.load_category()

    def load_category(self):
        if self.cat_id == "fav":
            self.show_favs()
        elif self.cat_id:
            self.request_games(self.m.games_list, self.cat_id)

    def _cancel_search(self):
        if self.search_job:
            try:
                self.root.after_cancel(self.search_job)
            except tk.TclError:
                pass
            self.search_job = None

    def on_search_typed(self):
        if self._suppress_search:
            return
        self._cancel_search()
        self.search_job = self.root.after(300, self.run_search)

    def _search_enter(self, _event=None):
        self._cancel_search()
        self.run_search()
        return "break"

    def run_search(self):
        self.search_job = None
        q = self.search_var.get().strip()
        if len(q) >= 2:
            self.chips.set_current(None)
            self.request_games(self.m.games_search, q)
        elif not q and self.cat_id:
            self.chips.set_current(self.cat_id)
            self.load_category()
        elif q:
            self.greq += 1
            self.clear_cards()
            self.set_gmsg("Escribe al menos 2 caracteres para buscar.")

    def set_gmsg(self, text, bad=False):
        self.gmsg.configure(text=text, fg=C["bad"] if bad else C["muted"])

    def request_games(self, fn, arg):
        self.greq += 1
        mine = self.greq
        self.set_gmsg("Cargando juegos…")
        self.clear_cards()
        self.bg(lambda: fn(arg), lambda r, e: self._games_done(mine, r, e))

    def _games_done(self, mine, res, err):
        if mine != self.greq:
            return
        if err:
            self.set_gmsg(str(err) if isinstance(err, (RobloxError, ValueError)) else "Error inesperado (mira app.log).", True)
            return
        self.render_games(res["games"])
        self.set_gmsg("" if res["games"] else "No se encontró ningún juego.")

    def show_favs(self):
        self.greq += 1
        places = self.m.store.places()
        games = [{"universe_id": p.get("universe_id"), "place_id": p["place_id"], "name": p["name"],
                  "players": None, "icon": p.get("icon")} for p in places]
        self.render_games(games, fav=True)
        missing = [p for p in places if not p.get("icon")]
        if missing and not self._fav_icon_loading:
            self._fav_icon_loading = True
            self.bg(lambda: self.m.api.place_icons([p["place_id"] for p in missing]),
                    lambda r, e: self._favorite_icons_done(missing, r, e))
        msg = "" if games else "Aún no tienes favoritos. Elige un juego y pulsa «★ Favorito»."
        if self.sorts_error and not self.sorts:
            msg = self.sorts_error + " Reintentando… mientras tanto puedes buscar un juego."
        self.set_gmsg(msg, bool(self.sorts_error and not self.sorts))

    def _favorite_icons_done(self, places, icons, err):
        self._fav_icon_loading = False
        if err or not icons:
            return
        current_ids = {place["place_id"] for place in self.m.store.places()}
        for place in places:
            icon = icons.get(place["place_id"])
            if icon and place["place_id"] in current_ids:
                self.m.store.add_place(place["name"], place["place_id"], place.get("universe_id"), icon)
        if self.cat_id == "fav" and not self.search_var.get().strip():
            self.show_favs()

    def clear_cards(self):
        for card in self.game_cards:
            card.destroy()
        self.game_cards = []

    def render_games(self, games, fav=False):
        self.clear_cards()
        place = parse_place(self.place_var.get())
        favorite_ids = {p["place_id"] for p in self.m.store.places()}
        for g in games:
            selected = bool(place and g["place_id"] == place)
            self.game_cards.append(GameCard(self, self.games_sf.inner, g, selected, fav, favorite_ids))
        self.layout_games(force=True)
        self.games_sf.canvas.yview_moveto(0)

    def _games_resized(self, e=None):
        width = e.width if e else self.games_sf.canvas.winfo_width()
        cols = max(1, width // (px(GameCard.W) + px(12)))
        if cols == getattr(self, "_game_cols", None):
            return
        if self.layout_job:
            self.root.after_cancel(self.layout_job)
        self.layout_job = self.root.after_idle(self.layout_games)

    def layout_games(self, force=False):
        self.layout_job = None
        w = self.games_sf.canvas.winfo_width()
        cols = max(1, w // (px(GameCard.W) + px(12)))
        cols_changed = cols != getattr(self, "_game_cols", None)
        if not cols_changed and not force:
            return
        self._game_cols = cols
        if cols_changed:
            for c in range(16):
                self.games_sf.inner.grid_columnconfigure(c, weight=1 if c < cols else 0,
                                                        uniform="g" if c < cols else "")
        for i, card in enumerate(self.game_cards):
            card.grid(row=i // cols, column=i % cols, padx=px(5), pady=px(5))

    def pick_game(self, g):
        self.sel_game = g
        self.place_var.set(str(g["place_id"]))
        self._place_changed()

    def _place_changed(self, *_a):
        place = parse_place(self.place_var.get())
        if self.sel_game and place != self.sel_game["place_id"]:
            self.sel_game = None
        for card in self.game_cards:
            card.set_selected(bool(place and card.g["place_id"] == place))
        if self.state:
            self.update_footer()

    def toggle_game_fav(self, g):
        if any(p["place_id"] == g["place_id"] for p in self.m.store.places()):
            self.remove_fav(g)
            return
        try:
            self.m.add_place(g["name"], str(g["place_id"]), g)
            self.toast("Guardado en favoritos.")
            self.refresh()
        except ValueError as err:
            self.toast(str(err), True)

    def remove_fav(self, g):
        if self.fav_confirm is not None:
            try:
                self.fav_confirm.destroy()
            except tk.TclError:
                pass
        self.root.update_idletasks()
        width, height = max(1, self.root.winfo_width()), max(1, self.root.winfo_height())
        try:
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            backdrop = ImageGrab.grab(bbox=(x, y, x + width, y + height)).convert("RGB")
            backdrop = backdrop.resize((width, height), Image.Resampling.LANCZOS)
            tint = Image.new("RGB", (width, height), C["bg"])
            backdrop = Image.blend(backdrop, tint, 0.38)
            backdrop = backdrop.filter(ImageFilter.GaussianBlur(radius=max(1, px(1))))
            self._fav_backdrop = ImageTk.PhotoImage(backdrop)
            shade = tk.Label(self.root, image=self._fav_backdrop, bg=C["bg"], bd=0, highlightthickness=0)
        except Exception:  # noqa: BLE001
            log.exception("No se pudo capturar el fondo para confirmar la eliminación del favorito")
            self._fav_backdrop = None
            shade = tk.Frame(self.root, bg=C["card2"])
        shade.place(x=0, y=0, relwidth=1, relheight=1)
        panel = Panel(shade, fill=C["panel"], border=C["line2"], radius=16)
        panel.place(relx=0.5, rely=0.5, anchor="center", width=px(440), height=px(190))
        body = panel.body
        lbl(body, "Quitar favorito", "ink", "h").pack(anchor="w", padx=px(24), pady=(px(20), px(8)))
        lbl(body, "¿Quitar «%s» de favoritos?" % g["name"], "muted", wraplength=px(380),
            justify="left").pack(anchor="w", padx=px(24))
        buttons = tk.Frame(body, bg=C["panel"])
        buttons.pack(side="bottom", fill="x", padx=px(20), pady=px(16))
        def close():
            self.fav_confirm = None
            shade.destroy()
        def remove():
            self.m.store.delete_place(g["place_id"])
            close()
            self.refresh()
        Btn(buttons, "Quitar", remove, "danger").pack(side="right", padx=(px(8), 0))
        Btn(buttons, "Cancelar", close, "ghost").pack(side="right")
        self.fav_confirm = shade
        shade.bind("<Escape>", lambda _e: close())
        shade.focus_set()
    # -- salir
    def on_close(self):
        if self.m.launching() and not self.ask("Salir", "Hay un lanzamiento en curso. ¿Salir igualmente?",
                                               [("Cancelar", None, "ghost"), ("Salir", "yes", "danger")]):
            return
        self.m.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------------------
# Arranque
# --------------------------------------------------------------------------------------
_singleton_handle = None


def find_app_browser():
    bases = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]
    candidates = []
    for base in filter(None, bases):
        candidates.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
        candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    return next((c for c in candidates if os.path.isfile(c)), None)


def single_instance() -> bool:
    """Evita abrir dos veces la app: si ya hay una, la trae al frente."""
    global _singleton_handle
    if not IS_WINDOWS:
        return True
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    _singleton_handle = k32.CreateMutexW(None, False, "Local\\Luxkor.AccountManager.Singleton")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        user32 = ctypes.WinDLL("user32")
        hwnd = user32.FindWindowW(None, APP_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
        return False
    return True


def prepare_windows() -> None:
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Luxkor.AccountManager")
    except Exception:  # noqa: BLE001
        pass


def main(argv=None) -> int:
    for name in ("stdout", "stderr"):  # pythonw / exe sin consola
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))
    ap = argparse.ArgumentParser(description=APP_TITLE)
    ap.add_argument("--data-dir", help="carpeta de datos (por defecto %%APPDATA%%\\RobloxManager)")
    ap.add_argument("--make-icon", metavar="RUTA", help="guarda el icono .ico y sale")
    ap.add_argument("--version", action="version", version="%s %s" % (APP_TITLE, APP_VERSION))
    args = ap.parse_args(argv)
    if tk is None or Image is None:
        text = "Falta Pillow (o Tkinter). Abre una consola y ejecuta:  pip install pillow"
        if IS_WINDOWS:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, 0x10)
        else:
            print(text, file=sys.stderr)
        return 1
    if args.make_icon:
        save_ico(args.make_icon)
        return 0
    if args.data_dir:
        os.environ["ROBLOX_MANAGER_HOME"] = args.data_dir

    directory = data_dir()
    setup_logging(directory)
    prepare_windows()
    if not single_instance():
        return 0
    manager = Manager(Store(os.path.join(directory, "data.json")), RobloxAPI(), make_platform())
    manager.start()
    threading.Thread(target=sweep_login_profiles, daemon=True).start()
    try:
        App(manager).run()
    except Exception:  # noqa: BLE001
        log.exception("La aplicación se cerró por un error")
        raise
    finally:
        manager.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
