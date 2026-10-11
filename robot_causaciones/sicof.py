"""Automatización de la interfaz de SICOF (funciona con --ui v1 y --ui v2).

Los localizadores usan solo lo que ambas interfaces conservan: etiquetas visibles,
textos de botones, títulos (h1) y el rol `alert`. Nunca ids ni clases CSS.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

PATRON_CAUSACION = re.compile(r"CXP-\d{4}-\d{6}")
TITULO_FORMULARIO = "Registrar causación de factura"
TITULO_REVISION = "Confirmar causación"
TITULO_REGISTRADA = "Causación registrada"
TITULO_NO_DISPONIBLE = "Servicio temporalmente no disponible"
TITULO_CONSULTA = "Consulta de causaciones"


class FallaSistema(Exception):
    """Falla transitoria (503, timeout, conexión): se reintenta."""


class Rechazo(Exception):
    """La factura no es causable (validación propia o mensaje de SICOF): no se reintenta."""


class ErrorConfiguracion(Exception):
    """Credenciales inválidas u otro problema que detiene todo el lote."""


class Sicof:
    def __init__(self, page: Page, url: str, usuario: str, clave: str) -> None:
        self.page = page
        self.url = url.rstrip("/")
        self.usuario = usuario
        self.clave = clave

    # ------------------------------------------------------------------ navegación
    def _esperar_pantalla(self, *titulos: str) -> tuple[str, str]:
        """Espera un título (h1) de `titulos` o un mensaje con rol alert.

        Devuelve ("alerta", texto) o ("pantalla", titulo).
        """
        patron = re.compile("|".join(re.escape(t) for t in titulos), re.IGNORECASE)
        titulo = self.page.get_by_role("heading", name=patron)
        alerta = self.page.get_by_role("alert")
        titulo.or_(alerta).first.wait_for(state="visible")
        if alerta.count():
            return "alerta", alerta.first.inner_text().strip()
        return "pantalla", titulo.first.inner_text().strip()

    def _login(self) -> None:
        self.page.goto(f"{self.url}/login")
        self.page.get_by_label("Usuario", exact=True).fill(self.usuario)
        self.page.get_by_label("Contraseña", exact=True).fill(self.clave)
        self.page.get_by_role("button", name="Ingresar", exact=True).click()
        tipo, texto = self._esperar_pantalla(TITULO_FORMULARIO)
        if tipo == "alerta":
            raise ErrorConfiguracion(f"No se pudo ingresar a SICOF: {texto}")

    def _ir(self, ruta: str) -> None:
        """Navega a `ruta`; si SICOF pide ingreso (sin sesión), inicia sesión y reintenta."""
        self.page.goto(f"{self.url}{ruta}")
        if "/login" in self.page.url:
            self._login()
            self.page.goto(f"{self.url}{ruta}")

    # ------------------------------------------------------------------ consultas
    def buscar_causacion(self, nit: str, factura: str) -> str | None:
        """Número de causación existente para el par (NIT, factura), o None."""
        self._ir(f"/causaciones?factura={quote(factura)}")
        self.page.get_by_role("heading", name=TITULO_CONSULTA).wait_for(state="visible")
        for fila in self.page.locator("table tbody tr").all():
            celdas = [c.strip() for c in fila.locator("td").all_inner_texts()]
            # Columnas: Causación, NIT, Proveedor, Factura, ...
            if len(celdas) >= 4 and celdas[1] == nit and celdas[3].upper() == factura.upper():
                return celdas[0]
        return None

    # ------------------------------------------------------------------ registro
    def registrar(self, nit: str, factura: str, fecha: date, valor: int, centro: str) -> str:
        """Recorre formulario -> revisión -> confirmación y devuelve el número de causación."""
        self._ir("/causaciones/nueva")
        self._esperar_pantalla(TITULO_FORMULARIO)

        # Un centro de costo fuera de la lista desplegable es un rechazo, no una falla del portal.
        lista = self.page.get_by_label("Centro de costo", exact=True)
        codigos = lista.locator("option").evaluate_all("opciones => opciones.map(o => o.value)")
        if centro not in codigos:
            raise Rechazo(f"El centro de costo {centro or '(vacío)'} no existe en SICOF")

        # Los campos están deshabilitados mientras SICOF carga el maestro de proveedores:
        # fill() y click() esperan a que se habiliten.
        self.page.get_by_label("NIT del proveedor", exact=True).fill(nit)
        self.page.get_by_label("Número de factura", exact=True).fill(factura)
        self.page.get_by_label("Fecha de factura", exact=True).fill(fecha.strftime("%d/%m/%Y"))
        self.page.get_by_label("Valor total", exact=True).fill(str(valor))
        lista.select_option(value=centro)
        self.page.get_by_role("button", name="Continuar", exact=True).click()

        tipo, texto = self._esperar_pantalla(TITULO_REVISION)
        if tipo == "alerta":
            raise Rechazo(texto)

        self.page.get_by_role("button", name="Confirmar causación", exact=True).click()
        tipo, texto = self._esperar_pantalla(TITULO_REGISTRADA, TITULO_NO_DISPONIBLE)
        if tipo == "alerta":
            raise Rechazo(texto)
        if TITULO_NO_DISPONIBLE.lower() in texto.lower():
            raise FallaSistema("SICOF-503 Servicio temporalmente no disponible")

        coincidencia = PATRON_CAUSACION.search(self.page.locator("body").inner_text())
        if not coincidencia:
            raise FallaSistema("No se encontró el número de causación en la pantalla de confirmación")
        return coincidencia.group(0)

    def intentar(self, nit: str, factura: str, fecha: date, valor: int, centro: str) -> tuple[str, str]:
        """Un intento completo. Devuelve (estado, número); lanza Rechazo o FallaSistema.

        La consulta previa va dentro del intento: si un intento anterior sí llegó a
        registrar la causación pero falló la respuesta, el reintento no la duplica.
        """
        try:
            existente = self.buscar_causacion(nit, factura)
            if existente:
                return "YA_CAUSADA", existente
            return "CAUSADA", self.registrar(nit, factura, fecha, valor, centro)
        except PlaywrightError as error:  # incluye TimeoutError y errores de conexión
            raise FallaSistema(str(error).strip().splitlines()[0]) from error
