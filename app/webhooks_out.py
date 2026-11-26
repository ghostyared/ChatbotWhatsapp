"""
Motor de Webhooks Salientes por eventos para integraciones externas (n8n, Zapier, Make, ERP, Google Calendar, Power BI).
Envía JSON firmado con HMAC-SHA256 en el header X-EVO-Signature.
"""
import hashlib
import hmac
import json
import logging

import requests

from .crypto import descifrar_token
from .models import WebhookSaliente, utcnow

logger = logging.getLogger(__name__)


def disparar_evento(evento, datos):
    """Notifica a todos los WebhookSaliente activos suscritos a `evento` (o '*'). Devuelve cantidad enviada."""
    try:
        hooks = WebhookSaliente.query.filter(
            WebhookSaliente.activo.is_(True),
            WebhookSaliente.evento.in_((evento, '*')),
        ).all()
    except Exception:
        return 0
    if not hooks:
        return 0

    body_dict = {
        "event": evento,
        "timestamp": utcnow().isoformat() + "Z",
        "data": datos or {},
    }
    raw = json.dumps(body_dict, ensure_ascii=False, default=str).encode("utf-8")
    enviados = 0
    for h in hooks:
        if not (h.url or "").startswith(("http://", "https://")):
            continue
        headers = {
            "Content-Type": "application/json",
            "X-EVO-Event": evento,
        }
        sec = descifrar_token(h.secret or "")
        if sec:
            sig = hmac.new(sec.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            headers["X-EVO-Signature"] = f"sha256={sig}"
        try:
            r = requests.post(h.url, data=raw, headers=headers, timeout=6)
            if 200 <= r.status_code < 300:
                enviados += 1
            else:
                logger.warning("Webhook saliente '%s' (%s) respondió %s", h.nombre, evento, r.status_code)
        except requests.RequestException as e:
            logger.warning("Error enviando webhook saliente '%s' (%s): %s", h.nombre, evento, e)
    return enviados
