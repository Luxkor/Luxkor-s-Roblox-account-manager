"""Mejoras de rendimiento: guardado diferido, caché de imágenes en disco, logo desde archivo y sondeo de PIDs."""
import io
import json
import os
import tempfile
import time
import unittest

from helpers import JsonHandler, rm, serve
from test_parallel import FakeApi, FakePlat, make


def new_store():
    return rm.Store(os.path.join(tempfile.mkdtemp(prefix="rm-perf-"), "data.json"))


def add_account(store, n=1):
    for i in range(n):
        store.upsert({"id": 100 + i, "name": "u%d" % i}, "cookie%d" % i)
    return [a["id"] for a in store.accounts()]


class DeferredSave(unittest.TestCase):
    def counting(self, store):
        calls = []
        original = store._save
        store._save = lambda: (calls.append(1), original())[1]
        return calls

    def test_deferred_changes_share_one_write(self):
        store = new_store()
        ids = add_account(store, 3)
        calls = self.counting(store)
        for aid in ids:
            store.update_account(aid, last_launch=1, defer=True)
        store.update_settings(defer=True, place="1818")
        self.assertEqual(calls, [])                        # nada escrito todavía…
        self.assertEqual(store.get(ids[0])["last_launch"], 1)  # …pero en memoria ya está
        store.flush()
        self.assertEqual(len(calls), 1)                    # una sola escritura para todo
        again = rm.Store(store.path)
        self.assertEqual(again.get(ids[2])["last_launch"], 1)
        self.assertEqual(again.settings()["place"], "1818")

    def test_flush_with_nothing_pending_does_not_write(self):
        store = new_store()
        add_account(store)
        calls = self.counting(store)
        store.flush()
        self.assertEqual(calls, [])

    def test_timer_writes_on_its_own(self):
        store = new_store()
        ids = add_account(store)
        store.SAVE_DELAY = 0.05
        store.update_account(ids[0], last_launch=5, defer=True)
        end = time.time() + 5
        while time.time() < end and rm.Store(store.path).get(ids[0])["last_launch"] != 5:
            time.sleep(0.05)
        self.assertEqual(rm.Store(store.path).get(ids[0])["last_launch"], 5)

    def test_important_changes_still_save_immediately_and_include_pending_ones(self):
        store = new_store()
        ids = add_account(store)
        store.update_account(ids[0], last_launch=9, defer=True)
        store.upsert({"id": 555, "name": "otra"}, "cookie-x")   # añadir cuenta: guardado inmediato
        disk = json.load(open(store.path, encoding="utf-8"))
        self.assertEqual(len(disk["accounts"]), 2)
        self.assertEqual(disk["accounts"][0]["last_launch"], 9)  # lo pendiente viaja con la escritura
        calls = self.counting(store)
        store.flush()
        self.assertEqual(calls, [])                              # y ya no queda nada pendiente

    def test_manager_stop_flushes(self):
        mgr, ids = make(FakeApi(delay=0), FakePlat(), n=1)
        mgr.store.update_account(ids[0], last_launch=7, defer=True)
        mgr.stop()
        self.assertEqual(rm.Store(mgr.store.path).get(ids[0])["last_launch"], 7)

    def test_launch_does_not_write_per_account_but_keeps_data(self):
        mgr, ids = make(FakeApi(delay=0), FakePlat(), n=3)
        calls = self.counting(mgr.store)
        mgr.launch(ids, "1818", "", 0, parallel=True)
        mgr.worker.join(10)
        self.assertEqual(calls, [])
        self.assertTrue(all(mgr.store.get(i)["last_launch"] for i in ids))
        mgr.stop()
        self.assertTrue(all(rm.Store(mgr.store.path).get(i)["last_launch"] for i in ids))


class ImageDiskCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rm-img-")
        rm.IMAGE_DISK_DIR = self.dir
        rm.IMG_OK = lambda u: True
        self.hits = 0
        outer = self

        class Img(JsonHandler):
            def do_GET(self):
                outer.hits += 1
                from PIL import Image
                buf = io.BytesIO()
                Image.new("RGB", (8, 8), (200, 10, 10)).save(buf, "PNG")
                self.send_response(200)
                self.send_header("Content-Length", str(len(buf.getvalue())))
                self.end_headers()
                self.wfile.write(buf.getvalue())
        self.srv, self.base = serve(Img)

    def tearDown(self):
        rm.IMAGE_DISK_DIR = None
        rm.IMG_OK = rm.roblox_img_ok
        self.srv.shutdown()

    def test_second_fetch_comes_from_disk(self):
        url = self.base + "/hash/150/150/Image/Png"
        first = rm.fetch_image(url)
        self.assertEqual((self.hits, first.size), (1, (8, 8)))
        again = rm.fetch_image(url)
        self.assertEqual((self.hits, again.size), (1, (8, 8)))  # sin segunda petición
        self.assertEqual(len([n for n in os.listdir(self.dir) if n.endswith(".img")]), 1)

    def test_a_different_url_is_a_different_file(self):
        rm.fetch_image(self.base + "/a")
        rm.fetch_image(self.base + "/b")
        self.assertEqual(self.hits, 2)

    def test_damaged_copy_is_downloaded_again(self):
        url = self.base + "/x"
        rm.fetch_image(url)
        path = rm._disk_image_path(url)
        with open(path, "wb") as fh:
            fh.write(b"esto no es una imagen")
        img = rm.fetch_image(url)
        self.assertEqual((self.hits, img.size), (2, (8, 8)))

    def test_disallowed_url_is_refused_even_if_cached(self):
        url = self.base + "/y"
        rm.fetch_image(url)
        rm.IMG_OK = lambda u: False
        with self.assertRaises(rm.RobloxError):
            rm.fetch_image(url)

    def test_prune_keeps_the_most_recent(self):
        for i in range(6):
            path = os.path.join(self.dir, "f%d.img" % i)
            open(path, "wb").write(b"x")
            os.utime(path, (1000 + i, 1000 + i))
        rm.prune_image_cache(limit=3)
        self.assertEqual(sorted(os.listdir(self.dir)), ["f3.img", "f4.img", "f5.img"])

    def test_no_disk_cache_means_old_behaviour(self):
        rm.IMAGE_DISK_DIR = None
        url = self.base + "/z"
        rm.fetch_image(url)
        rm.fetch_image(url)
        self.assertEqual(self.hits, 2)


class Logo(unittest.TestCase):
    def test_logo_file_is_identical_to_the_drawn_one(self):
        from PIL import ImageChops
        loaded, drawn = rm.load_logo(256), rm.draw_logo(256)
        self.assertEqual(loaded.size, (256, 256))
        self.assertIsNone(ImageChops.difference(loaded.convert("RGBA"), drawn.convert("RGBA")).getbbox())

    def test_falls_back_to_drawing_when_the_file_is_missing(self):
        original = rm.asset_path
        rm.asset_path = lambda name: os.path.join(tempfile.gettempdir(), "no-existe-" + name)
        try:
            self.assertEqual(rm.load_logo(256).size, (256, 256))
        finally:
            rm.asset_path = original


class PidPolling(unittest.TestCase):
    def test_wait_new_pid_polls_fast_and_can_be_cancelled(self):
        mgr, _ids = make(FakeApi(delay=0), FakePlat(), n=1)
        mgr.plat.list_pids = lambda: []
        mgr.cancel.set()
        started = time.time()
        self.assertIsNone(mgr._wait_new_pid(set(), 15))
        self.assertLess(time.time() - started, 1)


if __name__ == "__main__":
    unittest.main()
