"""Cliente de OpenRouter (API compatible con OpenAI) con llamadas a herramientas.
Convierte el formato interno (estilo Gemini: parts/functionCall/functionResponse) a mensajes OpenAI y de vuelta."""
import json
import logging
import requests
from config import Config
from .ia_gemini import IaError

logger = logging.getLogger(__name__)


def _a_mensajes(system, contents):
    msgs, pend, n = [{"role": "system", "content": system}], [], 0
    for c in contents:
        partes = c.get("parts") or []
        if c.get("role") == "model":
            texto = "".join(p.get("text", "") for p in partes)
            llamadas = [p["functionCall"] for p in partes if "functionCall" in p]
            m = {"role": "assistant", "content": texto or None}
            if llamadas:
                pend, m["tool_calls"] = [], []
                for fc in llamadas:
                    n += 1
                    pend.append(f"call_{n}")
                    m["tool_calls"].append({"id": pend[-1], "type": "function", "function": {
                        "name": fc.get("name"), "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False)}})
            msgs.append(m)
        else:
            resps = [p["functionResponse"] for p in partes if "functionResponse" in p]
            if resps:
                for fr, cid in zip(resps, pend):
                    msgs.append({"role": "tool", "tool_call_id": cid,
                                 "content": json.dumps(fr.get("response"), ensure_ascii=False, default=str)})
            else:
                msgs.append({"role": "user", "content": "".join(p.get("text", "") for p in partes)})
    return msgs


def _a_herramientas(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                               "parameters": t.get("parameters") or {"type": "object", "properties": {}}}}
            for t in tools or []]


def _llamar(modelo, system, contents, tools, temperatura):
    cuerpo = {"model": modelo, "messages": _a_mensajes(system, contents), "temperature": temperatura, "max_tokens": 1500}
    if tools:
        cuerpo["tools"] = _a_herramientas(tools)
    headers = {"Authorization": f"Bearer {Config.OPENROUTER_API_KEY}", "Content-Type": "application/json",
               "X-Title": Config.BOT_NAME}
    if Config.OPENROUTER_REFERER:
        headers["HTTP-Referer"] = Config.OPENROUTER_REFERER
    try:
        r = requests.post(f"{Config.OPENROUTER_BASE_URL}/chat/completions", json=cuerpo, headers=headers, timeout=Config.IA_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise IaError(f"Red: {e}") from e
    # Si el modelo gratuito no soporta 'tools' (400/404), reintenta una vez sin herramientas para no quedar mudo
    if tools and r.status_code in (400, 404):
        logger.warning("OpenRouter (%s) rechazó tools (%s); reintentando sin tools", modelo, r.status_code)
        cuerpo.pop("tools", None)
        try:
            r = requests.post(f"{Config.OPENROUTER_BASE_URL}/chat/completions", json=cuerpo, headers=headers, timeout=Config.IA_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise IaError(f"Red: {e}") from e
    if r.status_code != 200:
        raise IaError(f"OpenRouter ({modelo}) respondió {r.status_code}: {r.text[:300]}")
    try:
        data = r.json()
        if data.get("error"):
            raise IaError(f"OpenRouter ({modelo}): {str(data['error'])[:300]}")
        msg = data["choices"][0]["message"]
        partes = []
        if msg.get("content"):
            partes.append({"text": msg["content"]})
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            partes.append({"functionCall": {"name": tc["function"]["name"], "args": args}})
        if not partes:
            raise IaError(f"OpenRouter ({modelo}) devolvió respuesta vacía")
        tokens = int((data.get("usage") or {}).get("total_tokens", 0))
    except IaError:
        raise
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise IaError("Respuesta de OpenRouter inesperada") from e
    return {"role": "model", "parts": partes}, tokens


def generar(system, contents, tools=None, temperatura=0.4):
    """Prueba cada modelo de OPENROUTER_MODEL hasta que uno responda."""
    ultimo = None
    for modelo in Config.OPENROUTER_MODELS:
        try:
            return _llamar(modelo, system, contents, tools, temperatura)
        except IaError as e:
            logger.warning("OpenRouter modelo %s falló: %s", modelo, e)
            ultimo = e
    raise ultimo or IaError("OPENROUTER_MODEL vacío")
