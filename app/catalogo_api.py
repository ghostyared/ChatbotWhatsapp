"""
Cliente del catálogo de sedes (TU API: sedes, precios, vacantes, aulas).
Todo lo específico de tu API vive aquí: rutas y credenciales salen de .env y, si tus campos
tienen otros nombres, solo hay que ajustar _norm_sede / _norm_oferta.

Contrato esperado (ver docs/API_CATALOGO.md):
  GET {CAT_PATH_SEDES}                  -> [{id, nombre, zona, distrito, direccion, telefono_asesor, horario_atencion, referencia, tipo}]
  GET {CAT_PATH_OFERTA}?sede=<id>       -> {sede:{...}, ofertas:[{nivel, grado, turno, matricula, pension, moneda,
                                            vacantes, aulas:[{nombre, capacidad, disponibles}], beneficios,
                                            planes_pago:[...], requisitos:[...]}]}
  GET {CAT_PATH_PLANES}?tipo=&sede=&nivel=      (opcional) -> [{nombre, descripcion, cuotas, monto_cuota, vigente_hasta, condiciones}]
  GET {CAT_PATH_REQUISITOS}?tipo=&sede=&nivel=  (opcional) -> ["Partida de nacimiento", ...]  o  [{requisito, detalle}]
"""
import logging
import threading
import time
import requests
from config import Config

logger = logging.getLogger(__name__)
_cache = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 256


class CatalogoError(Exception):
    """El catálogo no responde o está mal configurado."""


def _mock(path, params):
    sedes = [
        {"id": "1", "nombre": "Sede Norte", "zona": "Los Olivos", "direccion": "Av. Universitaria 1234",
         "telefono_asesor": "", "horario_atencion": "Lun-Vie 8:00-17:00", "tipo": "colegio"},
        {"id": "2", "nombre": "Sede Centro", "zona": "San Miguel", "direccion": "Av. La Marina 456",
         "telefono_asesor": "", "horario_atencion": "Lun-Sáb 8:00-13:00", "tipo": "colegio"},
        {"id": "3", "nombre": "Instituto Pedagógico", "zona": "Cercado", "direccion": "Jr. Lampa 789",
         "telefono_asesor": "", "horario_atencion": "Lun-Vie 9:00-18:00", "tipo": "instituto"},
    ]
    if path.endswith(Config.CAT_PATH_SEDES):
        return sedes
    if path.endswith(Config.CAT_PATH_PLANES):
        return [{"nombre": "Pago mensual", "descripcion": "10 cuotas de pensión", "cuotas": 10, "monto_cuota": 420},
                {"nombre": "Pago anual adelantado", "descripcion": "Paga todo en marzo", "condiciones": "Descuento según tabla vigente"}]
    if path.endswith(Config.CAT_PATH_REQUISITOS):
        return ["Partida de nacimiento", "DNI del estudiante y del apoderado", "Certificado de estudios"]
    sid = str(params.get("sede"))
    sede = next((x for x in sedes if x["id"] == sid), None)
    if not sede:
        return {"sede": None, "ofertas": []}
    if sede["tipo"] == "instituto":
        ofertas = [{"nivel": "Educación Inicial", "grado": "Carrera (10 ciclos)", "turno": "Noche", "matricula": 300,
                    "pension": 380, "moneda": "S/", "vacantes": 12, "aulas": [{"nombre": "A-1", "capacidad": 30, "disponibles": 12}]}]
    else:
        base = 0 if sid == "1" else 60
        ofertas = [
            {"nivel": "Inicial", "grado": "5 años", "turno": "Mañana", "matricula": 350 + base, "pension": 420 + base,
             "moneda": "S/", "vacantes": 6, "aulas": [{"nombre": "Aula Girasoles", "capacidad": 20, "disponibles": 6}]},
            {"nivel": "Primaria", "grado": "1.º", "turno": "Mañana", "matricula": 380 + base, "pension": 450 + base,
             "moneda": "S/", "vacantes": 0, "aulas": [{"nombre": "1A", "capacidad": 25, "disponibles": 0}]},
        ]
    return {"sede": sede, "ofertas": ofertas}


def _get(path, params=None):
    if Config.CAT_API_MOCK:
        return _mock(path, params or {})
    if not Config.CAT_API_URL:
        raise CatalogoError("CAT_API_URL no está configurada")
    clave = (path, tuple(sorted((params or {}).items())))
    ttl = Config.CAT_CACHE_SEGUNDOS
    ahora = time.time()
    if ttl > 0:
        with _cache_lock:
            hit = _cache.get(clave)
            if hit and ahora - hit[0] < ttl:
                return hit[1]
    headers = {"Accept": "application/json"}
    if Config.CAT_API_TOKEN:
        headers[Config.CAT_AUTH_HEADER] = f"{Config.CAT_AUTH_SCHEME} {Config.CAT_API_TOKEN}".strip()
    try:
        r = requests.get(Config.CAT_API_URL + path, params=params, headers=headers, timeout=Config.CAT_API_TIMEOUT)
        if r.status_code >= 400:
            raise CatalogoError(f"El catálogo respondió {r.status_code}")
        data = r.json()
    except requests.exceptions.RequestException as e:
        raise CatalogoError(f"Error de red con el catálogo: {e}") from e
    except ValueError as e:
        raise CatalogoError("El catálogo no devolvió JSON válido") from e
    if ttl > 0:
        ahora = time.time()
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX:
                expiradas = [k for k, (ts, _) in _cache.items() if ahora - ts >= ttl]
                for k in expiradas:
                    del _cache[k]
                if len(_cache) >= _CACHE_MAX:
                    _cache.clear()
            _cache[clave] = (ahora, data)
    return data


def _pick(d, *claves, default=None):
    for c in claves:
        if isinstance(d, dict) and d.get(c) not in (None, ""):
            return d[c]
    return default


def _num(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _norm_sede(d):
    return {
        "id": str(_pick(d, "id", "codigo", "sede_id", default="")),
        "nombre": _pick(d, "nombre", "name", default="Sede"),
        "zona": _pick(d, "zona", "distrito", "ubicacion", default=""),
        "direccion": _pick(d, "direccion", "address", default=""),
        "telefono_asesor": "".join(ch for ch in str(_pick(d, "telefono_asesor", "asesor_telefono", "whatsapp", default="")) if ch.isdigit()),
        "horario": _pick(d, "horario_atencion", "horario", default=""),
        "tipo": _pick(d, "tipo", default="colegio"),
        "referencia": _pick(d, "referencia", default=""),
    }


def _norm_oferta(d):
    aulas = []
    for a in (_pick(d, "aulas", default=[]) or []):
        aulas.append({"nombre": _pick(a, "nombre", "aula", default=""), "capacidad": _pick(a, "capacidad"),
                      "disponibles": _pick(a, "disponibles", "vacantes", default=None)})
    vac = _num(_pick(d, "vacantes", "cupos", "vacantes_disponibles"))
    return {
        "nivel": _pick(d, "nivel", default=""), "grado": _pick(d, "grado", default=""),
        "turno": _pick(d, "turno", default=""),
        "matricula": _num(_pick(d, "matricula", "matrícula")), "pension": _num(_pick(d, "pension", "pensión", "mensualidad")),
        "moneda": _pick(d, "moneda", default="S/"),
        "vacantes": int(vac) if vac is not None else None,
        "aulas": aulas, "beneficios": _pick(d, "beneficios", default=""),
        "planes_pago": _lista(_pick(d, "planes_pago", "planes", "promociones", default=[])),
        "requisitos": _lista(_pick(d, "requisitos", default=[])),
    }


def _lista(v):
    """Acepta lista de textos o de objetos; devuelve una lista (nunca falla)."""
    if isinstance(v, dict):
        v = v.get("items") or v.get("data") or [v]
    return [x for x in (v or []) if x not in (None, "", {})] if isinstance(v, list) else []


def listar_sedes(tipo=None):
    data = _get(Config.CAT_PATH_SEDES)
    if isinstance(data, dict):
        data = data.get("sedes") or data.get("data") or []
    sedes = [_norm_sede(x) for x in data if isinstance(x, dict)]
    if tipo:
        t = tipo.strip().lower()
        sedes = [x for x in sedes if t in (x["tipo"] or "").lower()]
    return sedes


def consultar_planes(tipo=None, sede_id=None, nivel=None):
    """Planes/promociones de pago mensual vigentes (endpoint opcional)."""
    return _lista(_get(Config.CAT_PATH_PLANES, {k: v for k, v in (("tipo", tipo), ("sede", sede_id), ("nivel", nivel)) if v}))


def consultar_requisitos(tipo=None, sede_id=None, nivel=None):
    """Requisitos para postular (endpoint opcional)."""
    return _lista(_get(Config.CAT_PATH_REQUISITOS, {k: v for k, v in (("tipo", tipo), ("sede", sede_id), ("nivel", nivel)) if v}))


def consultar_oferta(sede_id, nivel=None):
    """Precios, vacantes y aulas EN VIVO de una sede. `nivel` filtra por coincidencia parcial."""
    data = _get(Config.CAT_PATH_OFERTA, {"sede": str(sede_id)})
    sede = _norm_sede(data.get("sede")) if isinstance(data, dict) and data.get("sede") else None
    ofertas = [_norm_oferta(x) for x in ((data or {}).get("ofertas") or []) if isinstance(x, dict)]
    if nivel:
        n = nivel.strip().lower()
        filtradas = [o for o in ofertas if n in (o["nivel"] or "").lower() or n in (o["grado"] or "").lower()]
        ofertas = filtradas or ofertas
    return sede, ofertas
