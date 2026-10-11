"""Robot de causación de facturas en SICOF.

Uso (desde esta carpeta):  uv run python main.py
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt, wait_fixed

from excel_io import Fila, Resultado, escribir_resultados, leer_cola, parsear_fecha, parsear_valor
from sicof import ErrorConfiguracion, FallaSistema, Rechazo, Sicof

RAIZ = Path(__file__).resolve().parent
ENTRADA = RAIZ / "datos" / "por_radicar.xlsx"
SALIDA = RAIZ / "salida"
EVIDENCIAS = SALIDA / "evidencias"
MAX_INTENTOS = 3

log = logging.getLogger("robot")


@dataclass
class Config:
    url: str
    usuario: str
    clave: str
    headless: bool


def cargar_config() -> Config:
    load_dotenv(RAIZ / ".env")
    return Config(
        url=os.getenv("PORTAL_URL", "http://127.0.0.1:8020"),
        usuario=os.getenv("PORTAL_USUARIO", "operador.rpa"),
        clave=os.getenv("PORTAL_CLAVE", "CxpSicof2026"),
        headless=os.getenv("BOT_HEADLESS", "true").strip().lower() not in ("0", "false", "no"),
    )


def configurar_log() -> None:
    SALIDA.mkdir(parents=True, exist_ok=True)
    formato = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    archivo = logging.FileHandler(SALIDA / "bot.log", mode="w", encoding="utf-8")
    consola = logging.StreamHandler(sys.stdout)
    for manejador in (archivo, consola):
        manejador.setFormatter(formato)
        log.addHandler(manejador)
    log.setLevel(logging.INFO)


def validar_antes_de_sicof(fila: Fila) -> tuple[int, date]:
    """Valida valor y fecha sin tocar SICOF. Lanza Rechazo si algo no cumple."""
    if not fila.factura:
        raise Rechazo("El número de factura es obligatorio")
    valor = parsear_valor(fila.valor)
    if valor is None:
        raise Rechazo("El valor debe ser un número entero mayor que cero")
    fecha = parsear_fecha(fila.fecha)
    if fecha is None:
        raise Rechazo("La fecha de factura no es una fecha válida (se esperaba yyyy-MM-dd)")
    if fecha > date.today():
        raise Rechazo("La fecha de factura no puede ser posterior a la fecha actual")
    return valor, fecha


def procesar(sicof: Sicof, fila: Fila) -> Resultado:
    base = dict(id_lista=fila.id_lista, factura=fila.factura, nit=fila.nit)
    try:
        valor, fecha = validar_antes_de_sicof(fila)

        def avisar_reintento(estado: RetryCallState) -> None:
            motivo = estado.outcome.exception() if estado.outcome else "desconocido"
            log.warning(
                "REINTENTO factura=%s nit=%s intento=%d/%d motivo=%s",
                fila.factura, fila.nit, estado.attempt_number, MAX_INTENTOS, motivo,
            )

        reintentos = Retrying(
            stop=stop_after_attempt(MAX_INTENTOS),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(FallaSistema),
            before_sleep=avisar_reintento,
            reraise=True,
        )
        estado, numero = reintentos(sicof.intentar, fila.nit, fila.factura, fecha, valor, fila.centro)
        return Resultado(**base, estado=estado, causacion=numero)
    except Rechazo as rechazo:
        return Resultado(**base, estado="RECHAZADA", detalle=str(rechazo))
    except FallaSistema as falla:
        capturar_evidencia(sicof, fila)
        return Resultado(
            **base,
            estado="FALLA_SISTEMA",
            detalle=f"Se agotaron los {MAX_INTENTOS} intentos: {falla}",
        )


def capturar_evidencia(sicof: Sicof, fila: Fila) -> None:
    EVIDENCIAS.mkdir(parents=True, exist_ok=True)
    nombre = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{fila.id_lista}_{fila.factura}")
    ruta = EVIDENCIAS / f"{nombre}.png"
    try:
        sicof.page.screenshot(path=str(ruta), full_page=True)
        log.info("Captura guardada en %s", ruta)
    except Exception as error:  # la captura nunca debe detener el lote
        log.warning("No se pudo guardar la captura de %s: %s", fila.factura, error)


def main() -> int:
    config = cargar_config()
    configurar_log()
    cola = leer_cola(ENTRADA)
    log.info("Inicio: %d facturas en %s (portal %s)", len(cola), ENTRADA.name, config.url)

    resultados: list[Resultado] = []
    try:
        with sync_playwright() as pw:
            navegador = pw.chromium.launch(headless=config.headless)
            pagina = navegador.new_context().new_page()
            pagina.set_default_timeout(10_000)
            sicof = Sicof(pagina, config.url, config.usuario, config.clave)
            for fila in cola:
                resultado = procesar(sicof, fila)
                resultados.append(resultado)
                log.info(
                    "factura=%s nit=%s estado=%s causacion=%s detalle=%s",
                    resultado.factura, resultado.nit, resultado.estado,
                    resultado.causacion or "-", resultado.detalle or "-",
                )
            navegador.close()
    except ErrorConfiguracion as error:
        log.error("%s", error)
        return 1
    finally:
        escribir_resultados(SALIDA / "resultados.xlsx", resultados)

    resumen: dict[str, int] = {}
    for r in resultados:
        resumen[r.estado] = resumen.get(r.estado, 0) + 1
    log.info("Fin: %s", ", ".join(f"{k}={v}" for k, v in sorted(resumen.items())) or "sin filas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
