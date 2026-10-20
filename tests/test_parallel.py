"""Pruebas del modo «Abrir a la vez»: peticiones en paralelo, sin pausas y cada proceso con su cuenta."""
import os
import re
import sys
import tempfile
import time
import unittest

from helpers import rm


class FakeApi:
    def __init__(self, delay=0.4, expired=()):
        self.delay, self.expired = delay, set(expired)

    def warm(self, cookie):
        pass

    def auth_ticket(self, cookie, place):
        time.sleep(self.delay)
        if cookie in self.expired:
            raise rm.AuthExpired("caducada")
        return "T" + cookie[-4:]


class FakePlat(rm.NullPlatform):
    """Simula Windows: cada launch() crea un 'RobloxPlayerBeta' cuya línea de comandos lleva el trackerId."""

    def __init__(self, cmdlines=True, descending=False):
        self.procs, self.uris, self.cmdlines = {}, [], cmdlines
        self.next, self.step = (5000, -1) if descending else (1000, 1)

    def launch(self, uri):
        self.uris.append(uri)
        tracker = re.search(r"browsertrackerid:(\d+)", uri).group(1)
        self.next += self.step
        self.procs[self.next] = "RobloxPlayerBeta.exe --app -b %s" % tracker

    def list_pids(self):
        return sorted(self.procs)

    def cmdline(self, pid):
        return self.procs.get(pid) if self.cmdlines else None


def make(api, plat, n=4):
    store = rm.Store(os.path.join(tempfile.mkdtemp(prefix="rm-par-"), "data.json"))
    ids = []
    for i in range(n):
        store.upsert({"id": 900 + i, "name": "Cuenta%d" % i, "displayName": "C%d" % i},
                     "_|WARNING:-X" + "A" * 100 + "%04d" % i)
        ids.append(store.accounts()[-1]["id"])
    mgr = rm.Manager(store, api, plat)
    mgr.PID_WAIT = 4
    return mgr, ids


def wait(mgr):
    mgr.worker.join(15)
    assert not mgr.worker.is_alive(), "el lanzamiento no terminó"


class OpenAtOnce(unittest.TestCase):
    def test_tickets_in_parallel_and_all_launched(self):
        mgr, ids = make(FakeApi(delay=0.4), FakePlat())
        t0 = time.time()
        res = mgr.launch(ids, "1818", "", "5", parallel=True)
        wait(mgr)
        elapsed = time.time() - t0
        self.assertEqual((res["started"], res["parallel"]), (4, True))
        self.assertLess(elapsed, 1.3, "debería tardar ~0,4 s (en paralelo), no 1,6 s (una a una)")
        self.assertEqual(len(mgr.plat.uris), 4)
        trackers = [re.search(r"browsertrackerid:(\d+)", u).group(1) for u in mgr.plat.uris]
        self.assertEqual(len(set(trackers)), 4)  # cada ventana con su propio trackerId
        self.assertEqual({mgr.status[i]["text"] for i in ids}, {"Abierta"})

    def test_each_process_goes_to_its_own_account_even_out_of_order(self):
        plat = FakePlat(descending=True)  # los PID salen al revés que el orden de lanzamiento
        mgr, ids = make(FakeApi(delay=0.05), plat)
        mgr.launch(ids, "1818", "", "5", parallel=True)
        wait(mgr)
        for aid in ids:
            ticket = "T" + mgr.store.cookie(aid)[-4:]
            uri = next(u for u in plat.uris if "gameinfo:%s+" % ticket in u)
            tracker = re.search(r"browsertrackerid:(\d+)", uri).group(1)
            self.assertIn(tracker, plat.procs[mgr.pids[aid]])

    def test_fallback_by_order_when_cmdline_is_unreadable(self):
        plat = FakePlat(cmdlines=False)
        mgr, ids = make(FakeApi(delay=0.05), plat)
        mgr.launch(ids, "1818", "", "5", parallel=True)
        wait(mgr)
        order = []  # cuentas en el orden REAL en que se lanzaron (cada una sale en cuanto tiene su ticket)
        for uri in plat.uris:
            ticket = re.search(r"gameinfo:([^+]+)\+", uri).group(1)
            order.append(next(i for i in ids if "T" + mgr.store.cookie(i)[-4:] == ticket))
        self.assertEqual([mgr.pids[i] for i in order], sorted(plat.procs))  # por orden de aparición
        self.assertEqual({mgr.status[i]["tag"] for i in ids}, {"ok"})

    def test_one_expired_cookie_does_not_block_the_rest(self):
        mgr, ids = make(FakeApi(delay=0.05), FakePlat())
        mgr.api.expired = {mgr.store.cookie(ids[1])}
        mgr.launch(ids, "1818", "", "5", parallel=True)
        wait(mgr)
        self.assertEqual(mgr.status[ids[1]], {"text": "Cookie caducada", "tag": "bad"})
        self.assertEqual({mgr.status[i]["text"] for i in (ids[0], ids[2], ids[3])}, {"Abierta"})
        self.assertEqual(len(mgr.plat.uris), 3)

    def test_unconfirmed_when_a_window_never_appears(self):
        class Lossy(FakePlat):
            def launch(self, uri):
                if len(self.uris) == 1:  # la segunda se "pierde": no aparece ningún proceso
                    self.uris.append(uri)
                    return
                super().launch(uri)
        mgr, ids = make(FakeApi(delay=0.05), Lossy(), n=3)
        mgr.PID_WAIT = 1.2
        mgr.launch(ids, "1818", "", "5", parallel=True)
        wait(mgr)
        texts = [mgr.status[i]["text"] for i in ids]
        self.assertEqual(texts.count("Abierta"), 2)
        self.assertEqual(texts.count("Lanzada, sin confirmar"), 1)

    def test_does_not_touch_the_saved_pause_and_refuses_double_launch(self):
        mgr, ids = make(FakeApi(delay=0.6), FakePlat(), n=2)
        mgr.store.update_settings(delay=7.0)
        mgr.launch(ids, "1818", "", "3", parallel=True)
        self.assertEqual(mgr.store.settings()["delay"], 7.0)  # la pausa de "una a una" queda como estaba
        with self.assertRaises(ValueError):
            mgr.launch(ids, "1818", "", "3", parallel=True)  # ya hay un lanzamiento en curso
        wait(mgr)

    def test_one_by_one_mode_still_uses_the_pause(self):
        mgr, ids = make(FakeApi(delay=0.01), FakePlat(), n=2)
        res = mgr.launch(ids, "1818", "", "0.6")
        self.assertFalse(res["parallel"])
        wait(mgr)
        self.assertEqual(mgr.store.settings()["delay"], 0.6)
        self.assertEqual({mgr.status[i]["text"] for i in ids}, {"Abierta"})


@unittest.skipUnless(rm.IS_WINDOWS, "solo en Windows")
class WindowsCmdline(unittest.TestCase):
    def test_can_read_own_command_line(self):
        cmd = rm.process_cmdline(os.getpid())
        if cmd is None:
            self.skipTest("este Windows no permite leer la línea de comandos")
        self.assertIn(os.path.basename(sys.executable).lower().split(".")[0], cmd.lower())


if __name__ == "__main__":
    unittest.main()
