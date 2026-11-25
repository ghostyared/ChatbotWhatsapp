"""
Cliente de la API académica (deudas y notas). TODO lo específico de tu API vive aquí:
rutas y credenciales salen de .env; si tu API responde con otros nombres de campo,
solo hay que ajustar las funciones _normalizar_* de este archivo.

Contrato esperado (ver docs/API_ACADEMICA.md):
  GET {ACAD_PATH_ANIOS}                         -> ["2026","2025"]
  GET {ACAD_PATH_PERIODOS}?anio=2026            -> ["2026-I","2026-II"]
  GET {ACAD_PATH_DEUDAS}?anio=&periodo=&dni=    -> {estudiante:{nombre,fecha_nacimiento,telefonos}, deudas:[...], total_pendiente}
  GET {ACAD_PATH_NOTAS}?anio=&periodo=&dni=     -> {estudiante:{...}, cursos:[{curso,nota,observacion}], promedio}
  404 si el estudiante no existe.
"""
import logging
from datetime import date
import requests
from config import Config

logger = logging.getLogger(__name__)


class ApiError(Exception):
    pass


class ApiNoDisponible(ApiError):
    """La API no responde o está mal configurada (no es culpa del usuario)."""


class NoEncontrado(ApiError):
    """No existe el estudiante / no hay datos para esos filtros."""


# ---------------------------------------------------------------- HTTP
def _get(path, params=None):
    if Config.ACAD_API_MOCK:
        return _mock(path, params or {})
    if not Config.ACAD_API_URL:
        raise ApiNoDisponible("ACAD_API_URL no está configurada")
    headers = {"Accept": "application/json"}
    if Config.ACAD_API_TOKEN:
        valor = f"{Config.ACAD_AUTH_SCHEME} {Config.ACAD_API_TOKEN}".strip()
        headers[Config.ACAD_AUTH_HEADER] = valor
    try:
        r = requests.get(Config.ACAD_API_URL + path, params=params, headers=headers,
                         timeout=Config.ACAD_API_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise ApiNoDisponible(f"Error de red con la API académica: {e}") from e
    if r.status_code == 404:
        raise NoEncontrado()
    if r.status_code in (401, 403):
        raise ApiNoDisponible("La API rechazó las credenciales del bot (401/403)")
    if r.status_code >= 400:
        raise ApiNoDisponible(f"La API respondió {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise ApiNoDisponible("La API no devolvió JSON válido") from e


# ---------------------------------------------------------------- listas
def _lista_texto(data, claves=("items", "data", "anios", "periodos")):
    if isinstance(data, dict):
        for k in claves:
            if k in data:
                data = data[k]
                break
    out = []
    for it in data if isinstance(data, list) else []:
        if isinstance(it, dict):
            it = it.get("codigo") or it.get("anio") or it.get("periodo") or it.get("nombre")
        if it is not None and str(it).strip():
            out.append(str(it).strip())
    return out


def listar_anios():
    if Config.ACAD_ANIOS_FIJOS:
        return Config.ACAD_ANIOS_FIJOS
    if not Config.ACAD_API_URL and not Config.ACAD_API_MOCK:
        y = date.today().year
        return [str(y), str(y - 1), str(y - 2)]
    return sorted(set(_lista_texto(_get(Config.ACAD_PATH_ANIOS))), reverse=True)[:10]


def listar_periodos(anio):
    if Config.ACAD_PERIODOS_FIJOS:
        return [p if p.startswith(str(anio)) else f"{anio}-{p}" for p in Config.ACAD_PERIODOS_FIJOS][:10]
    if not Config.ACAD_API_URL and not Config.ACAD_API_MOCK:
        return [f"{anio}-I", f"{anio}-II"]
    return _lista_texto(_get(Config.ACAD_PATH_PERIODOS, {"anio": anio}))[:10]


# ---------------------------------------------------------------- consultas
def _normalizar_estudiante(raw):
    e = raw.get("estudiante") if isinstance(raw, dict) else None
    e = e if isinstance(e, dict) else {}
    tels = e.get("telefonos") or ([e["telefono"]] if e.get("telefono") else [])
    return {
        "nombre": str(e.get("nombre") or "").strip(),
        "fecha_nacimiento": e.get("fecha_nacimiento"),
        "telefonos": [str(t) for t in tels],
    }


def consultar_deudas(anio, periodo, dni):
    raw = _get(Config.ACAD_PATH_DEUDAS, {"anio": anio, "periodo": periodo, "dni": dni})
    deudas = []
    for d in raw.get("deudas", []) or []:
        deudas.append({
            "concepto": str(d.get("concepto", "")).strip(),
            "monto": float(d.get("monto") or 0),
            "vencimiento": d.get("vencimiento"),
            "estado": str(d.get("estado", "")).strip(),
        })
    total = raw.get("total_pendiente")
    if total is None:
        total = sum(d["monto"] for d in deudas if d["estado"].lower() in ("pendiente", "vencido", "vencida"))
    return {"estudiante": _normalizar_estudiante(raw), "deudas": deudas,
            "total_pendiente": float(total), "moneda": raw.get("moneda", "S/")}


def consultar_notas(anio, periodo, dni):
    raw = _get(Config.ACAD_PATH_NOTAS, {"anio": anio, "periodo": periodo, "dni": dni})
    cursos = [{"curso": str(c.get("curso", "")).strip(), "nota": c.get("nota"),
               "observacion": str(c.get("observacion") or "").strip()} for c in raw.get("cursos", []) or []]
    return {"estudiante": _normalizar_estudiante(raw), "cursos": cursos, "promedio": raw.get("promedio")}


# ---------------------------------------------------------------- MOCK (solo demo)
def _mock(path, params):
    if path == Config.ACAD_PATH_ANIOS:
        return ["2026", "2025"]
    if path == Config.ACAD_PATH_PERIODOS:
        a = params.get("anio", "2026")
        return [f"{a}-I", f"{a}-II"]
    est = {
        "12345678": {"nombre": "Ana Pérez Quispe", "fecha_nacimiento": "2010-05-20", "telefonos": ["51900000001"]},
        "11111111": {"nombre": "Luis Rojas Díaz", "fecha_nacimiento": "2011-03-02", "telefonos": ["51900000002"]},
    }.get(params.get("dni"))
    if not est:
        raise NoEncontrado()
    if path == Config.ACAD_PATH_DEUDAS:
        if params["dni"] == "11111111":
            return {"estudiante": est, "deudas": [], "total_pendiente": 0}
        return {"estudiante": est, "moneda": "S/", "total_pendiente": 350.0, "deudas": [
            {"concepto": "Pensión marzo", "monto": 350.0, "vencimiento": "2026-03-15", "estado": "Pendiente"},
            {"concepto": "Matrícula", "monto": 400.0, "vencimiento": "2026-02-01", "estado": "Pagado"}]}
    if path == Config.ACAD_PATH_NOTAS:
        return {"estudiante": est, "promedio": 15.5, "cursos": [
            {"curso": "Matemática", "nota": 16, "observacion": ""},
            {"curso": "Comunicación", "nota": 15, "observacion": ""}]}
    raise NoEncontrado()
