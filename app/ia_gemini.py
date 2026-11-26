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


def _transcribir_groq(audio_bytes, mime_type="audio/ogg"):
    """Respaldo opcional vía Groq Whisper (rápido y con capa gratuita)."""
    if not Config.GROQ_API_KEY or not audio_bytes:
        return ""
    mime_limpio = (mime_type or "audio/ogg").split(";", 1)[0].strip().lower()
    ext = "ogg" if "ogg" in mime_limpio else ("mp3" if "mpeg" in mime_limpio else "m4a")
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {Config.GROQ_API_KEY}"},
            data={"model": Config.GROQ_WHISPER_MODEL, "language": "es"},
            files={"file": (f"nota.{ext}", audio_bytes, mime_limpio)},
            timeout=Config.IA_TIMEOUT,
        )
        if r.status_code == 200:
            return (r.json().get("text") or "").strip()
        logger.warning("Groq Whisper respondió %s: %s", r.status_code, r.text[:250])
    except Exception as e:
        logger.warning("Error transcribiendo audio con Groq Whisper: %s", e)
    return ""


def transcribir_audio(audio_bytes, mime_type="audio/ogg"):
    """Transcribe una nota de voz de WhatsApp: intenta Gemini multimodal (con modelos de respaldo)
    y si falla o falta clave, usa Groq Whisper si GROQ_API_KEY está configurada."""
    import base64
    if not audio_bytes:
        return ""
    mime_limpio = (mime_type or "audio/ogg").split(";", 1)[0].strip().lower()

    if Config.GEMINI_API_KEY:
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        body = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"inlineData": {"mimeType": mime_limpio, "data": b64}},
                    {"text": "Transcribe fielmente este audio en español. Devuelve únicamente el texto transcrito, sin comentarios ni comillas."},
                ],
            }],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 400},
        }
        modelos = []
        for m in (Config.GEMINI_MODEL, "gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.0-flash"):
            if m and m not in modelos:
                modelos.append(m)
        for modelo in modelos:
            url = f"{Config.GEMINI_BASE_URL}/models/{modelo}:generateContent"
            try:
                r = requests.post(
                    url, json=body, timeout=Config.IA_TIMEOUT,
                    headers={"x-goog-api-key": Config.GEMINI_API_KEY, "Content-Type": "application/json"},
                )
                if r.status_code == 200:
                    data = r.json()
                    partes = (data["candidates"][0].get("content") or {}).get("parts") or []
                    txt = "".join(p.get("text", "") for p in partes if not p.get("thought")).strip()
                    if txt:
                        return txt
                else:
                    logger.warning("Gemini audio (%s) respondió %s: %s", modelo, r.status_code, r.text[:200])
                    if r.status_code not in (404, 429, 503):
                        break
            except Exception as e:
                logger.warning("Error transcribiendo audio con Gemini (%s): %s", modelo, e)

    return _transcribir_groq(audio_bytes, mime_limpio)
