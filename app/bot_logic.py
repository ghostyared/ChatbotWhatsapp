import json
import re
import unicodedata
from config import Config
from .services import enviar_texto, enviar_botones
from .flujos import ACCIONES, CONTINUADORES, estado as estado_flujo
from .flujos.contexto import Contexto
from .models import RespuestaBot, Usuario, Colaborador, db, utcnow
from datetime import timedelta

# Menús de respaldo si el nodo aún no existe en la BD (el panel/editor los sobrescribe).
DEFAULT_MENUS = {
    'inicio': [("m_gest", "📚 Vacantes"), ("m_segu", "🎓 Soy alumno"), ("m_bene", "📍 Contacto")],
    'm_gest': [("v_colegio", "🏫 Colegio"), ("v_instituto", "🎓 Instituto"), ("f_matricula", "📝 Matrícula")],
    'm_segu': [("acc_deudas", "💳 Mis pagos"), ("acc_notas", "📝 Mis notas"), ("comp_enviar", "📤 Enviar comprobante")],
    'm_bene': [("f_horarios", "🕒 Horarios"), ("f_ubicacion", "📍 Ubicación"), ("f_asesor", "🙋 Asesor")],
}
PALABRAS_CANCELAR = {"cancelar", "salir", "anular"}
BOTONES_VOLVER = [("ir_inicio", "🏠 Inicio")]
IDS_INICIO = ("ir_inicio", "inicio")


def normalizar_texto(texto):
    if not texto:
        return ""
    texto = texto.lower().strip()
    return ''.join(c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn')


def _formatear(texto, kwargs):
    """Reemplaza {clave} solo para las claves conocidas (sin str.format: evita errores con llaves)."""
    texto = texto or ""
    for k, v in (kwargs or {}).items():
        texto = texto.replace("{" + k + "}", str(v))
    return texto


def _botones_de(nodo, clave):
    if nodo and nodo.botones_json:
        try:
            items = json.loads(nodo.botones_json)
            tuplas = [(str(b['id']), str(b['title'])) for b in items if b.get('id') and b.get('title')]
            if tuplas:
                return tuplas[:3]
        except (ValueError, TypeError, AttributeError):
            pass
    return DEFAULT_MENUS.get(clave)


def enviar_nodo(tel, clave, kwargs=None):
    """Envía el nodo `clave` (texto + link + botones). Devuelve False si no existe."""
    nodo = RespuestaBot.query.filter_by(clave=clave).first()
    if not nodo and clave not in DEFAULT_MENUS:
        return False
    texto = _formatear(nodo.texto if nodo else "Selecciona una opción:", kwargs)
    if nodo and nodo.link:
        texto += f"\n🔗 {nodo.link}"
    botones = _botones_de(nodo, clave) or BOTONES_VOLVER
    enviar_botones(tel, texto, botones)
    return True


def _persona_activa(dni):
    """Colaborador activo con ese DNI (prioriza el registro que esté activo)."""
    personas = Colaborador.query.filter_by(dni=dni).all()
    return next((p for p in personas if (p.estado or '').strip().lower() not in Config.INACTIVE_STATES), None)


def _id_bloqueado(usu):
    return bool(usu.bloqueado_id_hasta and usu.bloqueado_id_hasta > utcnow())


def _validar_id(tel, usu, txt_norm):
    """Valida DNI/ID contra la base de colaboradores, con límite de intentos por teléfono."""
    if _id_bloqueado(usu):
        enviar_texto(tel, "🤖 Superaste el máximo de intentos. Vuelve a intentarlo más tarde o contacta a soporte.")
        return

    persona = _persona_activa(txt_norm)
    if persona:
        usu.dni = txt_norm
        usu.intentos_id = 0
        usu.bloqueado_id_hasta = None
        db.session.commit()
        enviar_nodo(tel, 'bienvenida', {
            'nombre': persona.nombre, 'empresa': persona.empresa or Config.COMPANY_NAME, 'cargo': persona.cargo or '',
        })
        return

    usu.intentos_id = (usu.intentos_id or 0) + 1
    if usu.intentos_id >= Config.ID_MAX_ATTEMPTS:
        usu.bloqueado_id_hasta = utcnow() + timedelta(minutes=Config.ID_LOCK_MINUTES)
        usu.intentos_id = 0
    db.session.commit()
    enviar_texto(tel, f"🤖 No logré validar ese {Config.ID_LABEL}. Revisa que esté bien escrito o contacta a soporte. ✨")


def _nombre_para(usu, nombre_wa):
    if usu.dni:
        persona = Colaborador.query.filter_by(dni=usu.dni).first()
        if persona and persona.nombre:
            return persona.nombre
    return nombre_wa


def _palabras(txt_norm):
    return set(re.findall(r"[a-z0-9ñ]+", txt_norm))


def brain_gethi(tel, txt, btn_id, nombre_wa, tipo='text', media=None):
    """Cerebro del bot: validación opcional de ID, flujos guiados (pagos, notas, comprobantes),
    menús dirigidos por BD y palabras clave."""
    txt_norm = normalizar_texto(txt)

    usu = Usuario.query.filter_by(telefono=tel).first()
    if not usu:
        usu = Usuario(telefono=tel, nombre_wa=nombre_wa)
        db.session.add(usu)
        db.session.commit()
    usu.ultima_vez = utcnow()
    db.session.commit()

    # Modo híbrido: un humano atiende, el bot calla
    if usu.bot_pausado:
        return

    ctx = Contexto(tel=tel, usu=usu, nombre=nombre_wa, txt=txt or "", btn_id=btn_id or "", tipo=tipo, media=media)
    requiere_id = Config.REQUIRE_ID_VALIDATION

    # A. Validación opcional del DNI/ID contra la tabla de colaboradores
    if requiere_id and not usu.dni:
        if tipo == 'text' and txt_norm.isdigit() and len(txt_norm) in Config.ID_LENGTHS:
            _validar_id(tel, usu, txt_norm)
        else:
            enviar_texto(tel, (
                f"¡Hola *{nombre_wa}*! ✨ Soy *{Config.BOT_NAME}*, el asistente virtual de {Config.COMPANY_NAME}. 🏢\n\n"
                f"Por favor, *ingresa tu número de {Config.ID_LABEL}* para continuar."))
        return

    nombre = _nombre_para(usu, nombre_wa)
    ctx.nombre = nombre
    palabras = _palabras(txt_norm)

    # B. Volver al inicio / cancelar cualquier flujo en curso
    menu_kw = {normalizar_texto(k) for k in Config.MENU_KEYWORDS}
    es_cancelar = tipo == 'text' and bool(palabras & PALABRAS_CANCELAR)
    # Si la IA está activa, frases como "Hola, quiero info de precios" pasan a la IA;
    # solo un saludo/comando puro ("hola", "menu", "inicio", "info") abre el menú estático.
    es_menu_puro = (
        tipo == 'text'
        and bool(palabras & menu_kw)
        and (not Config.IA_ENABLED or len(palabras - menu_kw) == 0)
    )
    if btn_id in IDS_INICIO or es_cancelar or es_menu_puro:
        estado_flujo.terminar(tel)
        enviar_nodo(tel, 'inicio', {'nombre': nombre})
        return

    # C. Botón que inicia un flujo guiado
    if btn_id in ("m_segu", "acc_deudas", "acc_notas"):
        from .flujos import alumno
        if alumno.esta_activo():
            estado_flujo.terminar(tel)
            if btn_id == "acc_deudas":
                alumno.iniciar_pagos(ctx)
            elif btn_id == "acc_notas":
                alumno.iniciar_notas(ctx)
            else:
                alumno.iniciar(ctx)
            return

    if btn_id in ACCIONES:
        estado_flujo.terminar(tel)
        ACCIONES[btn_id](ctx)
        return

    # D. Flujo guiado en curso: él decide qué hacer con el mensaje
    st = estado_flujo.obtener(tel)
    if st and st.flujo in CONTINUADORES and CONTINUADORES[st.flujo](ctx, st):
        return

    # E. Archivos fuera de un flujo
    if tipo in ('image', 'document'):
        enviar_botones(tel, "📎 Recibí tu archivo, pero no sé a qué trámite corresponde. "
                            "Si es un comprobante de pago, inicia el envío desde aquí:",
                       [("comp_enviar", "📤 Enviar comprobante"), ("ir_inicio", "🏠 Inicio")])
        return
    if tipo not in ('text', 'interactive'):
        enviar_texto(tel, "🤖 Por ahora solo puedo leer mensajes de texto y botones. Escribe *Hola* para ver el menú. ✨")
        return

    # F. Botones de menú (nodos del editor)
    if btn_id:
        if not enviar_nodo(tel, btn_id, {'nombre': nombre}):
            enviar_texto(tel, "🤖 Opción no disponible en este momento. Escribe *Hola* para volver al menú.")
        return

    # G. Cambio de ID (solo si la validación de colaboradores está activa)
    if requiere_id and txt_norm.isdigit() and len(txt_norm) in Config.ID_LENGTHS:
        _validar_id(tel, usu, txt_norm)
        return

    # H. Texto libre: asistente de ventas con IA (si está activo) y, si no, mensaje por defecto
    if txt_norm:
        from . import asistente
        if asistente.responder(tel, txt, usu, ctx=ctx):
            return
        enviar_texto(tel, "🤖 No logré entender eso. Escribe *Hola* o usa los botones para navegar. ✨")
