"""Favoritos: el nombre y el icono se ponen al día con lo que Roblox muestra ahora (antes se quedaban como al guardarlos)."""
import os
import tempfile
import unittest
import urllib.parse

from helpers import JsonHandler, rm, serve


class Fake(JsonHandler):
    """Roblox de mentira: nombre e icono 'actuales' de cada juego, y un modo sin servicio."""
    current = {}      # universe_id -> nombre actual
    icons = {}        # place_id -> URL de icono actual
    universes = {}    # place_id -> universe_id
    down = False
    hits = []

    def do_GET(self):
        Fake.hits.append(self.path.split("?")[0])
        if Fake.down:
            return self.send_json({}, 500)
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path.startswith("/universes/v1/places/"):
            pid = int(u.path.split("/")[4])
            if pid in Fake.universes:
                return self.send_json({"universeId": Fake.universes[pid]})
            return self.send_json({}, 404)
        if u.path == "/v1/games":
            ids = [int(i) for i in q["universeIds"][0].split(",")]
            return self.send_json({"data": [{"id": i, "name": Fake.current[i], "rootPlaceId": 1000 + i, "playing": 3}
                                            for i in ids if i in Fake.current]})
        if u.path == "/v1/places/gameicons":
            ids = [int(i) for i in q["placeIds"][0].split(",")]
            return self.send_json({"data": [{"targetId": i, "imageUrl": Fake.icons[i], "state": "Completed"}
                                            for i in ids if i in Fake.icons]})
        self.send_json({}, 404)


class Favorites(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = serve(Fake)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Fake.current = {7: "Nombre Nuevo"}
        Fake.icons = {70: "https://tr.rbxcdn.com/nuevo/150/150/Image/Png"}
        Fake.universes = {70: 7}
        Fake.down, Fake.hits = False, []
        base = self.base
        api = rm.RobloxAPI(users_base=base, auth_base=base, apis_base=base, thumbs_base=base, games_base=base,
                           strict=False, img_ok=lambda u: True)
        store = rm.Store(os.path.join(tempfile.mkdtemp(prefix="rm-fav-"), "data.json"))
        self.mgr = rm.Manager(store, api, rm.NullPlatform())

    def place(self, pid):
        return next(p for p in self.mgr.store.places() if p["place_id"] == pid)

    def test_name_and_icon_are_refreshed(self):
        self.mgr.store.add_place("Nombre Viejo", 70, 7, "https://tr.rbxcdn.com/viejo/150/150/Image/Png")
        self.assertTrue(self.mgr.refresh_favorites())
        self.assertEqual(self.place(70)["name"], "Nombre Nuevo")
        self.assertEqual(self.place(70)["icon"], "https://tr.rbxcdn.com/nuevo/150/150/Image/Png")

    def test_refreshed_data_is_saved_to_disk(self):
        self.mgr.store.add_place("Nombre Viejo", 70, 7, "https://tr.rbxcdn.com/viejo/150/150/Image/Png")
        self.mgr.refresh_favorites()
        again = rm.Store(self.mgr.store.path)
        self.assertEqual(again.places()[0]["name"], "Nombre Nuevo")

    def test_not_asked_again_within_the_ttl(self):
        self.mgr.store.add_place("Nombre Viejo", 70, 7, "https://tr.rbxcdn.com/viejo/150/150/Image/Png")
        self.assertTrue(self.mgr.refresh_favorites())
        Fake.hits.clear()
        self.assertFalse(self.mgr.refresh_favorites())
        self.assertEqual(Fake.hits, [])  # ni una petición más
        Fake.current[7] = "Otro cambio"
        self.assertTrue(self.mgr.refresh_favorites(force=True))
        self.assertEqual(self.place(70)["name"], "Otro cambio")

    def test_favorite_without_universe_is_resolved_by_place(self):
        self.mgr.store.add_place("Juego 70", 70)  # añadido a mano: sin universo ni icono
        self.assertTrue(self.mgr.refresh_favorites())
        fav = self.place(70)
        self.assertEqual((fav["name"], fav["universe_id"]), ("Nombre Nuevo", 7))
        self.assertTrue(fav["icon"].endswith("/Png"))

    def test_no_change_means_no_write(self):
        self.mgr.store.add_place("Nombre Nuevo", 70, 7, "https://tr.rbxcdn.com/nuevo/150/150/Image/Png")
        before = os.path.getmtime(self.mgr.store.path)
        self.assertFalse(self.mgr.refresh_favorites())
        self.assertEqual(os.path.getmtime(self.mgr.store.path), before)

    def test_without_service_nothing_changes_and_it_retries_soon(self):
        self.mgr.store.add_place("Nombre Viejo", 70, 7, "https://tr.rbxcdn.com/viejo/150/150/Image/Png")
        Fake.down = True
        self.assertFalse(self.mgr.refresh_favorites())
        self.assertEqual(self.place(70)["name"], "Nombre Viejo")
        Fake.down = False  # al volver la red, el siguiente intento ya sí actualiza (no espera al TTL)
        self.assertTrue(self.mgr.refresh_favorites())
        self.assertEqual(self.place(70)["name"], "Nombre Nuevo")

    def test_missing_icon_is_retried_after_a_minute_not_every_time(self):
        Fake.icons = {}
        self.mgr.store.add_place("Nombre Viejo", 70, 7)
        self.mgr.refresh_favorites()
        self.assertEqual(self.place(70)["name"], "Nombre Nuevo")  # el nombre sí se actualiza
        Fake.hits.clear()
        self.assertFalse(self.mgr.refresh_favorites())
        self.assertEqual(Fake.hits, [])

    def test_one_write_for_many_favorites(self):
        for pid in (70, 71, 72):
            self.mgr.store.add_place("X", pid)
        calls = []
        original = self.mgr.store._save
        self.mgr.store._save = lambda: (calls.append(1), original())[1]
        changed = self.mgr.store.update_places({70: {"name": "A"}, 71: {"name": "B"}, 72: {"name": "C"}})
        self.assertEqual((changed, len(calls)), (3, 1))


if __name__ == "__main__":
    unittest.main()
