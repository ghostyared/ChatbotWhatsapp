import logging
import requests
from sqlalchemy.exc import IntegrityError
from config import Config
from .models import db, Mensaje

logger = logging.getLogger(__name__)

# Límites de la Cloud API de WhatsApp
MAX_TEXTO = 4096
MAX_BODY_BOTONES = 1024
MAX_TITULO_BOTON = 20
MAX_BOTONES = 3


def registrar_msg(tel, nom, txt, direccion, wa_id=None, estado_envio=None, media_tipo=None, media_url=None):
    """
    Guarda un mensaje. Devuelve 'ok' si se guardó, 'dup' si el wa_id ya existía
    (reintento de Meta) o 'error' si falló el guardado.
    """
    try:
        if wa_id and Mensaje.query.filter_by(wa_id=wa_id).first():
            return 'dup'
        db.session.add(Mensaje(
            telefono=str(tel),
            nombre=nom or "Usuario WA",
            contenido=txt or "Interacción con botón",
            direccion=direccion,
            wa_id=wa_id,
            estado_envio=estado_envio,
            media_tipo=media_tipo,
            media_url=media_url,
        ))
        db.session.commit()
        return 'ok'
    except IntegrityError:
        db.session.rollback()
        return 'dup'
    except Exception:
        db.session.rollback()
        logger.exception("Error al guardar mensaje en BD")
        return 'error'


def enviar_api(payload, registro=None):
    """Envía a WhatsApp y registra la SALIDA con wa_id (para checks de entrega/lectura) solo si Meta aceptó."""
    headers = {"Authorization": f"Bearer {Config.TOKEN_PERMANENTE}", "Content-Type": "application/json"}
    try:
        response = requests.post(Config.URL_API, json=payload, headers=headers, timeout=Config.WA_API_TIMEOUT)
    except requests.exceptions.Timeout:
        logger.error("Timeout al contactar la API de WhatsApp")
        return None
    except requests.exceptions.RequestException as e:
        logger.error("Error de red con la API de WhatsApp: %s", e)
        return None

    if response.status_code == 200:
        tipo = payload.get('type')
        txt_bot = ""
        m_tipo, m_url = None, None
        if registro is not None:
            txt_bot = registro
        elif tipo == 'text':
            txt_bot = payload.get('text', {}).get('body', '')
        elif tipo == 'interactive':
            txt_bot = payload.get('interactive', {}).get('body', {}).get('text', '')
        elif tipo in ('document', 'image', 'video'):
            obj = payload.get(tipo, {})
            m_tipo = tipo
            m_url = obj.get('link') or ''
            etq = {'document': 'Documento', 'image': 'Imagen', 'video': 'Video'}[tipo]
            nombre_f = obj.get('filename') or obj.get('link') or obj.get('id') or ''
            txt_bot = f"[{etq}: {nombre_f}] {obj.get('caption', '')}".strip()
        elif tipo == 'template':
            tpl = payload.get('template', {})
            txt_bot = f"[Plantilla HSM: {tpl.get('name', '')}]"

        out_wa_id = None
        try:
            msgs = response.json().get('messages') or []
            if msgs and isinstance(msgs[0], dict):
                out_wa_id = msgs[0].get('id')
        except Exception:
            out_wa_id = None

        registrar_msg(
            payload['to'], Config.bot_sender(), txt_bot, 'SALIDA',
            wa_id=out_wa_id, estado_envio='sent', media_tipo=m_tipo, media_url=m_url,
        )
    else:
        # No se registra el token ni el cuerpo completo; solo código y mensaje de Meta
        logger.warning("API WhatsApp respondió %s: %s", response.status_code, response.text[:500])
    return response


def enviar_texto(tel, txt, registro=None):
    return enviar_api({
        "messaging_product": "whatsapp", "to": tel, "type": "text",
        "text": {"body": (txt or "")[:MAX_TEXTO], "preview_url": True},
    }, registro=registro)


def enviar_botones(tel, txt, lista, registro=None):
    """lista = [(id, título), ...] (máx. 3; títulos de máx. 20 caracteres)."""
    btns = [
        {"type": "reply", "reply": {"id": str(idx)[:256], "title": tit[:MAX_TITULO_BOTON]}}
        for idx, tit in lista[:MAX_BOTONES]
    ]
    payload = {
        "messaging_product": "whatsapp", "to": tel, "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": (txt or " ")[:MAX_BODY_BOTONES]},
            "footer": {"text": Config.bot_sender()[:60]},
            "action": {"buttons": btns},
        },
    }
    return enviar_api(payload, registro=registro)


def enviar_lista(tel, txt, texto_boton, filas, titulo_seccion="Opciones"):
    """Lista desplegable de WhatsApp. filas = [(id, título<=24, descripción<=72), ...] (máx. 10)."""
    rows = []
    for fila in filas[:10]:
        idx, tit = fila[0], fila[1]
        row = {"id": str(idx)[:200], "title": str(tit)[:24]}
        if len(fila) > 2 and fila[2]:
            row["description"] = str(fila[2])[:72]
        rows.append(row)
    payload = {
        "messaging_product": "whatsapp", "to": tel, "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": (txt or " ")[:MAX_BODY_BOTONES]},
            "footer": {"text": Config.bot_sender()[:60]},
            "action": {"button": texto_boton[:20], "sections": [{"title": titulo_seccion[:24], "rows": rows}]},
        },
    }
    return enviar_api(payload)


def subir_media(data_bytes, mime_type, filename="archivo"):
    """Sube un archivo (imagen/video/PDF) a WhatsApp Media API y devuelve su media_id."""
    url = f"https://graph.facebook.com/{Config.GRAPH_API_VERSION}/{Config.PHONE_ID}/media"
    headers = {"Authorization": f"Bearer {Config.TOKEN_PERMANENTE}"}
    try:
        r = requests.post(
            url,
            headers=headers,
            data={"messaging_product": "whatsapp"},
            files={"file": (filename, data_bytes, mime_type)},
            timeout=Config.WA_MEDIA_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json().get("id")
        logger.warning("Meta Media API respondió %s: %s", r.status_code, r.text[:300])
    except requests.exceptions.RequestException as e:
        logger.error("Error subiendo media a WhatsApp: %s", e)
    return None


def enviar_imagen(tel, url=None, media_id=None, caption="", registro=None):
    """Envía una imagen (por URL HTTPS o media_id subido a Meta)."""
    img = {"id": media_id} if media_id else {"link": url}
    if caption:
        img["caption"] = caption[:1024]
    return enviar_api({
        "messaging_product": "whatsapp", "to": tel, "type": "image", "image": img,
    }, registro=registro)


def enviar_video(tel, url=None, media_id=None, caption="", registro=None):
    """Envía un video MP4 (por URL HTTPS o media_id subido a Meta)."""
    vid = {"id": media_id} if media_id else {"link": url}
    if caption:
        vid["caption"] = caption[:1024]
    return enviar_api({
        "messaging_product": "whatsapp", "to": tel, "type": "video", "video": vid,
    }, registro=registro)


def enviar_documento(tel, url=None, filename="brochure.pdf", caption="", registro=None, media_id=None):
    """Envía un documento PDF/brochure por enlace HTTPS o media_id a través de WhatsApp Cloud API."""
    doc = {"id": media_id} if media_id else {"link": url}
    doc["filename"] = (filename or "documento.pdf")[:120]
    if caption:
        doc["caption"] = caption[:1024]
    payload = {
        "messaging_product": "whatsapp",
        "to": tel,
        "type": "document",
        "document": doc,
    }
    return enviar_api(payload, registro=registro)


def enviar_plantilla(tel, nombre_template, idioma="es", componentes=None, registro=None):
    """Envía una plantilla HSM aprobada por Meta (respeta opt_out del contacto)."""
    from .models import Usuario
    usu = Usuario.query.filter_by(telefono=str(tel)).first()
    if usu and getattr(usu, "opt_out", False):
        logger.info("Envío HSM bloqueado por opt_out para %s", tel)
        return None
    tpl = {"name": nombre_template, "language": {"code": idioma or "es"}}
    if componentes:
        tpl["components"] = componentes
    payload = {
        "messaging_product": "whatsapp",
        "to": tel,
        "type": "template",
        "template": tpl,
    }
    return enviar_api(payload, registro=registro)


# ---------- Descarga de archivos recibidos (comprobantes) ----------
class MediaError(Exception):
    """No se pudo obtener el archivo (red, tamaño, URL inesperada)."""


def descargar_media(media_id, max_bytes):
    """Descarga un archivo de WhatsApp (2 pasos: metadatos -> URL temporal). Devuelve (bytes, mime)."""
    headers = {"Authorization": f"Bearer {Config.TOKEN_PERMANENTE}"}
    base = f"https://graph.facebook.com/{Config.GRAPH_API_VERSION}"
    try:
        meta = requests.get(f"{base}/{media_id}", headers=headers, timeout=Config.WA_API_TIMEOUT)
        if meta.status_code != 200:
            raise MediaError(f"Meta respondió {meta.status_code} al consultar el archivo")
        info = meta.json()
        url = info.get("url", "")
        if not url.startswith("https://"):
            raise MediaError("URL de descarga inesperada")
        if info.get("file_size") and int(info["file_size"]) > max_bytes:
            raise MediaError("Archivo demasiado grande")
        data = bytearray()
        with requests.get(url, headers=headers, timeout=Config.WA_MEDIA_TIMEOUT, stream=True) as r:
            if r.status_code != 200:
                raise MediaError(f"Descarga falló ({r.status_code})")
            for chunk in r.iter_content(64 * 1024):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise MediaError("Archivo demasiado grande")
        return bytes(data), info.get("mime_type", "")
    except requests.exceptions.RequestException as e:
        raise MediaError(f"Error de red: {e}") from e
