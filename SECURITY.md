# Seguridad

## Reportar una vulnerabilidad

No abras un *issue* público. Usa **Security → Report a vulnerability** (reporte privado de GitHub) en este repositorio.
Describe el problema y cómo reproducirlo. Nunca incluyas cookies reales: sustitúyelas por texto de ejemplo.

## Qué protege el programa

| Medida | Detalle |
|---|---|
| Cookies cifradas en disco | DPAPI de Windows, ligadas a tu usuario. Un `data.json` copiado a otro PC o usuario no se puede descifrar. |
| La interfaz nunca ve la cookie | Solo se muestran nombre y avatar. Se descifra únicamente al lanzar o verificar. |
| La cookie solo viaja a `roblox.com` por HTTPS | Cualquier otro destino (o HTTP) se rechaza antes de enviar nada. |
| Sin redirecciones | Una redirección no puede reenviar la cookie a otro sitio. |
| Sin servidor local | No hay puertos abiertos ni páginas web locales: es una app de escritorio. |
| Cookies validadas antes de guardarse | Se comprueba con Roblox; una cookie falsa o caducada no se guarda. |
| Contraseñas descartadas | En líneas `usuario:contraseña:cookie` solo se conserva la cookie. |
| Logs sin secretos | El código no escribe cookies en `app.log` y un filtro tapa cualquier cookie que se colara. |
| Cerrar sesión al eliminar | Al borrar una cuenta puedes invalidar su cookie con el cierre de sesión de Roblox (si falla, la app te avisa). |
| Portapapeles | Tras añadir una cuenta se vacía el portapapeles si aún contiene la cookie. |
| Herramientas del sistema | `tasklist` y `taskkill` se ejecutan con ruta completa de `System32`. |
| Imágenes | Solo de `*.rbxcdn.com` por HTTPS, sin redirecciones y con límite de tamaño. |
| Datos de lanzamiento validados | El ID de juego es numérico y el JobId debe tener formato UUID; no se pasa nada a una consola. |

## Qué NO protege (limitaciones conocidas)

- **Malware que se ejecute con tu usuario de Windows** puede descifrar las cookies igual que lo hace este programa (así funciona DPAPI).
  Si tu PC está infectado, tus cuentas están en riesgo. Mantén Windows y el antivirus al día.
- **Una cookie robada equivale a la cuenta** y se salta la contraseña y el 2FA. Si sospechas que se filtró: cierra todas las sesiones
  en roblox.com → Ajustes → Seguridad y cambia la contraseña.
- **Método «Navegador»:** mientras dura el inicio de sesión (hasta 10 minutos), el navegador temporal expone un puerto de depuración
  en `127.0.0.1` con puerto aleatorio. Otros programas de tu PC podrían conectarse a él. Los sitios web, en principio, no pueden (las versiones actuales de Chrome/Edge
  rechazan por defecto conexiones con `Origin`). El perfil temporal se borra al terminar, y los restos de un cierre brusco se limpian al abrir la app.
- **El `.exe` no está firmado.** Descárgalo solo desde *Releases* de este repositorio y comprueba su SHA-256 / procedencia (ver README).
  El empaquetado `--onefile` se extrae en la carpeta temporal en cada arranque; si te preocupa, compílalo con `--onedir`.
- **Servicios no documentados:** código rápido, listas y búsqueda de juegos usan endpoints no oficiales que Roblox puede cambiar.
- No hay contraseña maestra: quien tenga acceso a tu sesión de Windows abierta puede usar la app.

## Recomendaciones

1. Activa la verificación en dos pasos en tus cuentas de Roblox.
2. No compartas `%APPDATA%\RobloxManager\data.json` ni subas esa carpeta a ningún sitio.
3. Usa solo cuentas tuyas. Desconfía de quien te pida «una cookie» o un código rápido.
4. Si dejas de usar una cuenta, elimínala eligiendo **Sí** en «cerrar también la sesión en Roblox».

## Auditoría de la versión 1.1.0

Revisión del código centrada en lo que podría comprometer una cuenta. Corregido en 1.1.0 (cada punto tiene prueba en `tests/test_security.py`):

| # | Hallazgo | Riesgo | Corrección |
|---|---|---|---|
| 1 | Las peticiones seguían redirecciones y `urllib` conserva la cabecera `Cookie` | Alto | No se siguen redirecciones; la cookie solo va a `roblox.com` por HTTPS |
| 2 | `tasklist`/`taskkill` se buscaban por nombre (una carpeta con un `.exe` falso podía ganar) | Medio | Ruta completa en `System32` |
| 3 | El login por navegador aceptaba cualquier dominio que terminara en `roblox.com` (p. ej. `notroblox.com`) | Medio | Solo `roblox.com` y sus subdominios |
| 4 | Un perfil temporal del navegador (con sesión) podía quedar en disco tras un cierre brusco | Medio | Limpieza al arrancar |
| 5 | La cookie podía llegar a un mensaje de error del log | Bajo | Filtro de censura en el log |
| 6 | Las cookies copiadas se quedaban en el portapapeles | Bajo | Se vacía tras añadir la cuenta |
| 7 | Imágenes sin límite de tamaño ni control de redirecciones | Bajo | Límite de píxeles, sin redirecciones |
| 8 | Eliminar una cuenta no invalidaba su cookie | Bajo | Opción de cerrar la sesión en Roblox |
| 9 | El modo de desarrollo (cookies sin cifrar) existía también en el `.exe` | Bajo | Desactivado en builds compilados |
