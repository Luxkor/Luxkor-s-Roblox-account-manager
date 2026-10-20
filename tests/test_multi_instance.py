"""El bloqueo de multi-instancia: Roblox cierra la otra ventana si existe ROBLOX_singletonEvent."""
import ctypes
import unittest
import uuid
from ctypes import wintypes

from helpers import rm

ERROR_INVALID_HANDLE = 6


@unittest.skipUnless(rm.IS_WINDOWS, "solo en Windows")
class Guard(unittest.TestCase):
    def setUp(self):
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.k32.CreateEventW.restype = wintypes.HANDLE
        self.k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self.k32.OpenMutexW.restype = wintypes.HANDLE
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        tag = uuid.uuid4().hex[:10]
        self.mutex, self.event = "TEST_singletonMutex_" + tag, "TEST_singletonEvent_" + tag
        self.plat = rm.WindowsPlatform(self.mutex, self.event)

    def tearDown(self):
        self.plat.multi_disable()

    def create_event(self):
        ctypes.set_last_error(0)
        handle = self.k32.CreateEventW(None, True, False, self.event)
        return handle, ctypes.get_last_error()

    def test_roblox_cannot_create_the_event_while_we_hold_the_name(self):
        ok, detail = self.plat.multi_enable()
        self.assertTrue(ok, detail)
        handle, err = self.create_event()  # lo que haría un Roblox nuevo al arrancar
        self.assertFalse(handle)
        self.assertEqual(err, ERROR_INVALID_HANDLE)
        self.assertEqual(self.plat.multi_enable(), (True, ""))  # activar dos veces no rompe nada
        self.plat.multi_disable()
        handle, _err = self.create_event()  # al desactivar, el nombre vuelve a quedar libre
        self.assertTrue(handle)
        self.k32.CloseHandle(handle)

    def test_refuses_and_cleans_up_when_a_roblox_already_holds_the_event(self):
        roblox_event, _ = self.create_event()  # simula un Roblox que ya estaba abierto
        self.assertTrue(roblox_event)
        try:
            ok, detail = self.plat.multi_enable()
            self.assertFalse(ok)
            self.assertIn("Roblox", detail)
            self.assertIn("Reintentar", detail)
            # no se queda a medias: el mutex que se había tomado se libera
            self.assertFalse(self.k32.OpenMutexW(0x100000, False, self.mutex))
        finally:
            self.k32.CloseHandle(roblox_event)
        ok, detail = self.plat.multi_enable()  # y al cerrar ese Roblox ya se puede reintentar
        self.assertTrue(ok, detail)


if __name__ == "__main__":
    unittest.main()
