"""Flujo guiado: envío de comprobante de pago (DNI del estudiante -> foto/PDF) para revisión de contabilidad."""
import hashlib
import logging
import os
import uuid

from config import Config
from ..models import Comprobante, RespuestaBot, db
from ..services import MediaError, descargar_media, enviar_botones, enviar_texto
from . import estado

logger = logging.getLogger(__name__)

TIPOS = {  # firma binaria -> (mime, extensión). Se valida el contenido real, no solo lo que declara el cliente.
    b"\xff\xd8\xff": ("image/jpeg", ".jpg"),
    b"\x89PNG\r\n\x1a\n": ("image/png", ".png"),
    b"%PDF-": ("application/pdf", ".pdf"),
}


def _detectar(data):
    for firma, tipo in TIPOS.items():
        if data.startswith(firma):
            return tipo
    return None


def iniciar(ctx):
    intro = RespuestaBot.query.filter_by(clave="comp_enviar").first()
    if intro and intro.texto:
        enviar_texto(ctx.tel, intro.texto)
    estado.iniciar(ctx.tel, "comprobante", "dni")
    enviar_texto(ctx.tel, "Escribe el *DNI del estudiante* al que corresponde el pago (solo números).")


def continuar(ctx, st):
    if st.paso == "dni":
        dni = (ctx.txt or "").strip() if ctx.tipo == "text" else ""
        if dni.isdigit() and len(dni) in Config.ID_LENGTHS:
            estado.avanzar(st, "archivo", dni=dni)
            enviar_texto(ctx.tel, f"Ahora envía la *foto o el PDF* del comprobante (máx. {Config.COMPROBANTE_MAX_MB} MB). "
                                  "Puedes agregar el monto o una nota como descripción del archivo.")
        else:
            largos = " o ".join(str(n) for n in Config.ID_LENGTHS)
            enviar_texto(ctx.tel, f"El DNI debe tener {largos} dígitos y solo números. Inténtalo de nuevo o escribe *cancelar*.")
        return True

    if st.paso == "archivo":
        if ctx.tipo in ("image", "document") and ctx.media and ctx.media.get("id"):
            return _guardar(ctx, st)
        enviar_texto(ctx.tel, "Necesito la *foto o el PDF* del comprobante. Envíalo como imagen o documento, o escribe *cancelar*.")
        return True

    estado.terminar(ctx.tel)
    return False


def _guardar(ctx, st):
    max_bytes = Config.COMPROBANTE_MAX_MB * 1024 * 1024
    try:
        data, _ = descargar_media(ctx.media["id"], max_bytes)
    except MediaError as e:
        logger.warning("No se pudo descargar el comprobante de %s: %s", ctx.tel, e)
        enviar_texto(ctx.tel, f"No pude recibir el archivo (máx. {Config.COMPROBANTE_MAX_MB} MB). Inténtalo de nuevo.")
        return True

    tipo = _detectar(data)
    if not tipo:
        enviar_texto(ctx.tel, "Formato no admitido. Envía el comprobante como *JPG, PNG o PDF*.")
        return True
    mime, ext = tipo

    sha = hashlib.sha256(data).hexdigest()
    duplicado = Comprobante.query.filter_by(sha256=sha).first() is not None
    nombre_archivo = uuid.uuid4().hex + ext
    en_bd = Config.COMPROBANTE_ALMACEN == "bd"
    if not en_bd:
        os.makedirs(Config.COMPROBANTES_DIR, exist_ok=True)
        fd = os.open(os.path.join(Config.COMPROBANTES_DIR, nombre_archivo), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)

    c = Comprobante(telefono=ctx.tel, nombre_wa=ctx.nombre, dni_estudiante=estado.datos(st).get("dni", ""),
                    archivo=nombre_archivo, mime=mime, tamano=len(data), sha256=sha, duplicado=duplicado,
                    nota=(ctx.media.get("caption") or "")[:300], contenido=data if en_bd else None)
    db.session.add(c)
    db.session.commit()
    estado.terminar(ctx.tel)
    logger.info("comprobante %s recibido de %s (dup=%s)", c.codigo, ctx.tel, duplicado)
    enviar_botones(ctx.tel, f"✅ Recibimos tu comprobante *{c.codigo}*.\nEl área contable lo revisará y te avisaremos por este chat.",
                   [("comp_enviar", "📤 Enviar otro"), ("acc_deudas", "💳 Mis pagos"), ("ir_inicio", "🏠 Inicio")])
    return True
