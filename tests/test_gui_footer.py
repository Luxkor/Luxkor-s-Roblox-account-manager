"""Prueba de la ventana: filas sin «Lanzar», pie de una sola fila y un único botón de lanzar."""
import time
import unittest

from helpers import rm
from test_parallel import FakeApi, FakePlat, make


class GuiApi(FakeApi):
    def explore_sorts(self, session):
        raise rm.RobloxError("sin red en las pruebas")

    def legacy_sorts(self):
        raise rm.RobloxError("sin red en las pruebas")


def _display_ok():
    try:
        root = rm.tk.Tk()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


class Launching(unittest.TestCase):
    def test_a_game_must_be_chosen(self):
        mgr, ids = make(GuiApi(delay=0), FakePlat(), n=1)
        with self.assertRaises(ValueError) as ctx:
            mgr.launch(ids, "", "", 0, parallel=True)
        self.assertIn("Elige primero un juego", str(ctx.exception))


@unittest.skipUnless(_display_ok(), "sin pantalla disponible")
class Window(unittest.TestCase):
    def pump(self, app, cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end and not cond():
            app.root.update()
            time.sleep(0.01)
        self.assertTrue(cond(), "tiempo agotado")

    def test_clean_rows_single_launch_button_and_remembered_game(self):
        mgr, ids = make(GuiApi(delay=0), FakePlat(), n=2)
        app = rm.App(mgr)
        try:
            app.root.update()
            rows = [w for w in app.acc_sf.inner.winfo_children() if isinstance(w, rm.AccountRow)]
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertFalse(hasattr(row, "b_go"))             # sin botón «Lanzar» junto al perfil
            for gone in ("btn_all", "delay_var", "game_lbl", "pick_lbl", "btn_clear", "add_fav"):
                self.assertFalse(hasattr(app, gone), gone)         # nada de pausa, nombre del juego ni botones duplicados
            self.assertEqual(app.btn_go.text, "Lanzar")
            self.assertFalse(app.btn_go.enabled)                   # sin cuentas seleccionadas
            app.pick_game({"name": "Brookhaven RP", "place_id": 1818, "universe_id": 1, "players": 5, "icon": None})
            app.toggle(ids[0], 0, False)
            app.root.update()
            self.assertEqual(app.btn_go.text, "Lanzar")            # con una cuenta: solo «Lanzar»
            self.assertTrue(app.btn_go.enabled)
            app.toggle(ids[1], 1, False)
            app.root.update()
            self.assertEqual(app.btn_go.text, "Lanzar instancias seleccionadas")
            # un solo botón: abre todas a la vez con el juego elegido
            app.go()
            mgr.worker.join(10)
            self.assertEqual(len(mgr.plat.uris), 2)
            self.assertTrue(all("1818" in u for u in mgr.plat.uris))
            self.pump(app, lambda: mgr.store.settings().get("place") == "1818")
        finally:
            app.root.destroy()
        # al reabrir, el juego elegido sigue siendo el mismo
        rm._PHOTOS.clear()  # las imágenes cacheadas pertenecían a la ventana anterior (en la app real solo hay una)
        app2 = rm.App(mgr)
        try:
            app2.root.update()
            self.assertEqual(app2.place_var.get(), "1818")
        finally:
            app2.root.destroy()


if __name__ == "__main__":
    unittest.main()
