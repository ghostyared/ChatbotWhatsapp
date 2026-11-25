"""Cliente mínimo de Gemini (REST, sin SDK) con function calling. Solo habla con Google;
la lógica del negocio vive en app/asistente.py."""
import logging
import requests
from config import Config

logger = logging.getLogger(__name__)


class IaError(Exception):
    pass


def _limpiar_tools(tools):
    """Gemini REST v1beta rechaza parameters={type: object, properties: {}} vacío: omite parameters si no tiene propiedades."""
    limpias = []
    for t in tools or []:
        decl = {"name": t["name"], "description": t.get("description", "")}
        params = t.get("parameters")
        if isinstance(params, dict) and params.get("properties"):
            decl["parameters"] = params
        limpias.append(decl)
    return limpias


def generar(system, contents, tools=None, temperatura=0.4):
    """Una llamada a generateContent. Devuelve (content_del_modelo, tokens_usados)."""
    gen_cfg = {"temperature": temperatura, "maxOutputTokens": 1500}
    if "2.5" in (Config.GEMINI_MODEL or ""):
        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": gen_cfg,
    }
    if tools:
        body["tools"] = [{"functionDeclarations": _limpiar_tools(tools)}]
    url = f"{Config.GEMINI_BASE_URL}/models/{Config.GEMINI_MODEL}:generateContent"
    try:
        r = requests.post(url, json=body, timeout=Config.IA_TIMEOUT,
                          headers={"x-goog-api-key": Config.GEMINI_API_KEY, "Content-Type": "application/json"})
    except requests.exceptions.RequestException as e:
        raise IaError(f"Red: {e}") from e
    if r.status_code != 200:
        raise IaError(f"Gemini respondió {r.status_code}: {r.text[:300]}")
    try:
        data = r.json()
        cand = data["candidates"][0]
        content = cand.get("content") or {"role": "model", "parts": []}
        tokens = int((data.get("usageMetadata") or {}).get("totalTokenCount", 0))
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise IaError("Respuesta de Gemini inesperada") from e
    # Filtra partes de razonamiento interno si el modelo las devuelve
    partes = [p for p in (content.get("parts") or []) if not p.get("thought")]
    if not partes:
        raise IaError(f"Gemini devolvió respuesta vacía (finishReason={cand.get('finishReason')})")
    content = {"role": content.get("role", "model"), "parts": partes}
    return content, tokens
