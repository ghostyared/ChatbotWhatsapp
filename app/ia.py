"""Enrutador de proveedores de IA: principal y respaldo (Config.IA_PROVIDER / IA_PROVIDER_RESPALDO)."""
import logging
from config import Config
from . import ia_gemini, ia_openrouter
from .ia_gemini import IaError

logger = logging.getLogger(__name__)
ULTIMO_PROVEEDOR = ""
ULTIMO_ERROR = ""


def proveedores():
    lista = [p for p in (Config.IA_PROVIDER, Config.IA_PROVIDER_RESPALDO) if p]
    return list(dict.fromkeys(lista))


def generar(system, contents, tools=None, temperatura=0.4):
    global ULTIMO_PROVEEDOR, ULTIMO_ERROR
    errores = []
    for prov in proveedores():
        modulo = {"gemini": ia_gemini, "openrouter": ia_openrouter}.get(prov)
        if not modulo:
            continue
        try:
            res = modulo.generar(system, contents, tools, temperatura)
            ULTIMO_PROVEEDOR = prov
            ULTIMO_ERROR = ""
            return res
        except IaError as e:
            logger.warning("Proveedor de IA '%s' falló: %s", prov, e)
            errores.append(f"{prov}: {e}")
    msg = " | ".join(errores) if errores else "No hay proveedor de IA configurado"
    ULTIMO_ERROR = msg[:400]
    raise IaError(msg)
