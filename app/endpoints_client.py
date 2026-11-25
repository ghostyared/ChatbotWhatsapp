"""
Motor de ejecución para endpoints HTTP configurados desde el panel (/admin/endpoints).
Soporta sustitución segura de variables dinámicas ({dni}, {IdEstudiante}, {IdPeriodo}, etc.)
tanto dentro de cadenas JSON ("{dni}") como como valores tipados sin comillas ({IdEstudiante}).
"""
import json
import logging
import re
import time
from urllib.parse import quote_plus, urlparse

import requests

from config import Config
from .models import ApiEndpoint

logger = logging.getLogger(__name__)

_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_QUOTED_VAR_RE = re.compile(r'"\{([A-Za-z_][A-Za-z0-9_]*)\}"')

# Catálogo de variables dinámicas disponibles en el flujo "Soy Alumno" (documentación y UI)
VARIABLES_ALUMNO = [
    ("dni", "DNI ingresado por el estudiante", "78744420"),
    ("IdEstudiante", "ID del estudiante devuelto por PeriodosEstudiante", "8844"),
    ("IdEmpresa", "ID de la empresa / colegio / instituto seleccionado", "45695"),
    ("IdCarrera", "ID de la carrera o nivel educativo", "10"),
    ("IdCiclo", "ID del ciclo o grado", "23"),
    ("IdPeriodo", "ID del periodo académico seleccionado", "165"),
    ("IdInscripcion", "ID de la inscripción/matrícula", "59011"),
]


class EndpointError(Exception):
    """Error al renderizar o invocar un endpoint configurado."""


def extraer_variables(*textos):
    """Devuelve lista ordenada sin duplicados de nombres de variables {var} encontrados."""
    vistas, orden = set(), []
    for t in textos:
        for nombre in _VAR_RE.findall(t or ""):
            if nombre not in vistas:
                vistas.add(nombre)
                orden.append(nombre)
    return orden


def _valor_sin_comillas(valor):
    """Convierte un valor para insertarlo sin comillas en JSON (número/bool si aplica, si no string JSON)."""
    if valor is None:
        return "null"
    if isinstance(valor, bool):
        return "true" if valor else "false"
    if isinstance(valor, (int, float)):
        return str(valor)
    s = str(valor).strip()
    if re.fullmatch(r"-?\d+", s):
        return s
    if re.fullmatch(r"-?\d+\.\d+", s):
        return s
    if s.lower() in ("true", "false", "null"):
        return s.lower()
    return json.dumps(s, ensure_ascii=False)


def renderizar_url(url_template, variables=None):
    """Reemplaza {var} dentro de la URL escapando caracteres especiales."""
    variables = variables or {}

    def _rep(m):
        k = m.group(1)
        if k not in variables:
            raise EndpointError(f"Falta la variable '{k}' para la URL")
        return quote_plus(str(variables[k]))

    return _VAR_RE.sub(_rep, (url_template or "").strip())


def renderizar_body(body_template, variables=None):
    """Reemplaza variables dinámicas en body_template y devuelve el objeto Python (dict/list) listo para JSON.
    - '"{var}"' -> siempre se serializa como string JSON seguro (evita inyección JSON).
    - '{var}'   -> se serializa como número/booleano si corresponde, o string JSON seguro.
    """
    raw = (body_template or "").strip()
    if not raw:
        return None
    variables = variables or {}

    def _rep_quoted(m):
        k = m.group(1)
        if k not in variables:
            raise EndpointError(f"Falta la variable '{k}' para el cuerpo JSON")
        return json.dumps("" if variables[k] is None else str(variables[k]), ensure_ascii=False)

    def _rep_unquoted(m):
        k = m.group(1)
        if k not in variables:
            raise EndpointError(f"Falta la variable '{k}' para el cuerpo JSON")
        return _valor_sin_comillas(variables[k])

    # 1) Primero placeholders exactos entre comillas: "{dni}"
    paso1 = _QUOTED_VAR_RE.sub(_rep_quoted, raw)
    # 2) Luego placeholders dentro de cadenas parciales o sin comillas
    out = []
    en_cadena = False
    escapado = False
    i = 0
    n = len(paso1)
    while i < n:
        ch = paso1[i]
        if en_cadena:
            if escapado:
                out.append(ch)
                escapado = False
                i += 1
                continue
            if ch == "\\":
                out.append(ch)
                escapado = True
                i += 1
                continue
            if ch == '"':
                en_cadena = False
                out.append(ch)
                i += 1
                continue
            if ch == "{":
                m = _VAR_RE.match(paso1, i)
                if m:
                    k = m.group(1)
                    if k not in variables:
                        raise EndpointError(f"Falta la variable '{k}' para el cuerpo JSON")
                    # Escapar para incrustar dentro de una cadena JSON existente
                    escaped = json.dumps("" if variables[k] is None else str(variables[k]), ensure_ascii=False)[1:-1]
                    out.append(escaped)
                    i = m.end()
                    continue
            out.append(ch)
            i += 1
        else:
            if ch == '"':
                en_cadena = True
                out.append(ch)
                i += 1
                continue
            if ch == "{":
                m = _VAR_RE.match(paso1, i)
                if m:
                    out.append(_rep_unquoted(m))
                    i = m.end()
                    continue
            out.append(ch)
            i += 1

    renderizado = "".join(out)
    try:
        return json.loads(renderizado)
    except ValueError as e:
        raise EndpointError(f"El cuerpo renderizado no es JSON válido: {e}") from e


def validar_body_template(body_template):
    """Verifica que body_template produce JSON válido usando valores dummy para todas sus variables."""
    raw = (body_template or "").strip()
    if not raw:
        return True, None
    vars_dummy = {v: "1" for v in extraer_variables(raw)}
    try:
        renderizar_body(raw, vars_dummy)
        return True, None
    except EndpointError as e:
        return False, str(e)


def obtener_endpoint(clave, solo_activo=True):
    """Busca un ApiEndpoint por su clave en BD."""
    q = ApiEndpoint.query.filter_by(clave=clave)
    if solo_activo:
        q = q.filter_by(activo=True)
    return q.first()


def _token_env_para(url):
    """ACAD_API_TOKEN (del .env) solo para hosts permitidos en ACAD_TOKEN_HOSTS; nunca a dominios ajenos."""
    if not Config.ACAD_API_TOKEN:
        return ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        host = ""
    if host and host in Config.ACAD_TOKEN_HOSTS:
        return Config.ACAD_API_TOKEN
    logger.warning("Token de .env NO enviado a '%s' (no está en ACAD_TOKEN_HOSTS)", host or url)
    return ""


def ejecutar(clave_o_endpoint, variables=None, timeout_override=None):
    """Ejecuta un endpoint (por clave o instancia ApiEndpoint) con las variables dadas.
    Devuelve dict: {ok, status_code, data, raw_text, sent_body, url, elapsed_ms, error}.
    """
    ep = obtener_endpoint(clave_o_endpoint, solo_activo=False) if isinstance(clave_o_endpoint, str) else clave_o_endpoint
    if not ep:
        return {"ok": False, "status_code": None, "data": None, "raw_text": "",
                "sent_body": None, "url": "", "elapsed_ms": 0,
                "error": f"Endpoint '{clave_o_endpoint}' no encontrado"}

    metodo = (ep.metodo or "POST").strip().upper()
    timeout = int(timeout_override or ep.timeout or 10)
    try:
        url = renderizar_url(ep.url, variables)
        body = renderizar_body(ep.body_template, variables)
    except EndpointError as e:
        return {"ok": False, "status_code": None, "data": None, "raw_text": "",
                "sent_body": None, "url": ep.url or "", "elapsed_ms": 0, "error": str(e)}

    headers = {"Accept": "application/json"}
    if ep.headers_json:
        try:
            extra = json.loads(ep.headers_json)
            if isinstance(extra, dict):
                for k, v in extra.items():
                    headers[str(k)] = str(v)
        except ValueError:
            pass

    token = (ep.token or "").strip() or _token_env_para(url)
    auth_header = (ep.auth_header or "Authorization").strip()
    auth_scheme = (ep.auth_scheme or "").strip()
    if token and auth_header:
        headers[auth_header] = f"{auth_scheme} {token}".strip() if auth_scheme else token

    kwargs = {"headers": headers, "timeout": timeout}
    if metodo == "GET":
        if isinstance(body, dict):
            kwargs["params"] = body
    else:
        if body is not None:
            kwargs["json"] = body

    t0 = time.monotonic()
    try:
        r = requests.request(metodo, url, **kwargs)
    except requests.exceptions.RequestException as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        logger.warning("Error de red en endpoint '%s': %s", ep.clave, e)
        return {"ok": False, "status_code": None, "data": None, "raw_text": "",
                "sent_body": body, "url": url, "elapsed_ms": elapsed,
                "error": f"Error de red: {e}"}

    elapsed = int((time.monotonic() - t0) * 1000)
    data = None
    try:
        data = r.json()
    except ValueError:
        pass

    ok = 200 <= r.status_code < 300
    err = None if ok else f"HTTP {r.status_code}: {r.text[:300]}"
    if not ok:
        logger.warning("Endpoint '%s' respondió %s", ep.clave, r.status_code)
    return {
        "ok": ok,
        "status_code": r.status_code,
        "data": data,
        "raw_text": r.text[:4000],
        "sent_body": body,
        "url": url,
        "elapsed_ms": elapsed,
        "error": err,
    }
