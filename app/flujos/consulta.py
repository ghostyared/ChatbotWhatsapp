"""Flujo guiado: consulta de pagos (deudas) o notas -> año (lista) -> periodo (lista) -> DNI -> [verificación] -> reporte."""
import logging
import re
from datetime import datetime, timedelta

from config import Config
from .. import api_academica as api
from ..models import AuditoriaConsulta, RespuestaBot, db, utcnow
from ..services import enviar_botones, enviar_lista, enviar_texto
from . import estado

logger = logging.getLogger(__name__)
MAX_MSG = 3800


# ------------------------------------------------------------------ utilidades
def _mask(dni):
    return "****" + dni[-4:] if dni else ""


def _bloqueado(usu):
    return bool(usu.bloqueado_acad_hasta and usu.bloqueado_acad_hasta > utcnow())


def _auditar(ctx, tipo, dni, anio, periodo, resultado):
    db.session.add(AuditoriaConsulta(telefono=ctx.tel, tipo=tipo, dni_mask=_mask(dni), anio=anio,
                                     periodo=periodo, resultado=resultado))
    db.session.commit()
    logger.info("consulta %s tel=%s dni=%s %s %s -> %s", tipo, ctx.tel, _mask(dni), anio, periodo, resultado)


def _parse_fecha(txt):
    txt = (txt or "").strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d%m%Y"):
        try:
            return datetime.strptime(txt, fmt).date()
        except ValueError:
            pass
    return None


def _fecha_visible(valor):
    f = _parse_fecha(str(valor)) if valor else None
    return f.strftime("%d/%m/%Y") if f else (str(valor) if valor else "-")


def _dinero(moneda, monto):
    return f"{moneda} {monto:,.2f}"


def _nombre_visible(est):
    nombre = est.get("nombre") or "Estudiante"
    if Config.ACAD_VERIFY_MODE == "none":       # sin segundo control: no revelar el nombre completo
        return " ".join(p[0] + "***" for p in nombre.split())
    return nombre


def _verificar(est, tel, fecha):
    """Segundo control (además del DNI). Si la API no devuelve el dato necesario, se deniega."""
    modo = Config.ACAD_VERIFY_MODE
    if modo == "none":
        return True
    if modo == "fecha_nacimiento":
        real = _parse_fecha(str(est.get("fecha_nacimiento") or ""))
        if not real:
            logger.error("La API no devolvió estudiante.fecha_nacimiento: se deniega la consulta")
            return False
        return fecha == real
    if modo == "telefono":
        mio = re.sub(r"\D", "", tel)[-9:]
        tels = [re.sub(r"\D", "", t)[-9:] for t in est.get("telefonos", [])]
        if not tels:
            logger.error("La API no devolvió estudiante.telefonos: se deniega la consulta")
        return bool(mio) and mio in tels
    return False


def _enviar_largo(tel, lineas, registro):
    bloque = ""
    for linea in lineas:
        if len(bloque) + len(linea) + 1 > MAX_MSG:
            enviar_texto(tel, bloque.rstrip(), registro=registro)
            bloque = ""
        bloque += linea + "\n"
    if bloque.strip():
        enviar_texto(tel, bloque.rstrip(), registro=registro)


def _no_disponible(ctx):
    estado.terminar(ctx.tel)
    enviar_botones(ctx.tel, "😕 El servicio de consulta no está disponible en este momento. Inténtalo más tarde.",
                   [("f_asesor", "🙋 Hablar con asesor"), ("ir_inicio", "🏠 Inicio")])


# ------------------------------------------------------------------ inicio
def iniciar_deudas(ctx):
    _iniciar(ctx, "deudas")


def iniciar_notas(ctx):
    _iniciar(ctx, "notas")


def _iniciar(ctx, tipo):
    if _bloqueado(ctx.usu):
        enviar_texto(ctx.tel, "🔒 Superaste el máximo de intentos. Vuelve a intentarlo más tarde o contacta a un asesor.")
        return
    intro = RespuestaBot.query.filter_by(clave="acc_" + tipo).first()
    if intro and intro.texto:
        enviar_texto(ctx.tel, intro.texto)
    estado.iniciar(ctx.tel, "consulta", "anio", {"tipo": tipo})
    _pedir_anio(ctx)


def _pedir_anio(ctx):
    try:
        anios = api.listar_anios()
    except api.ApiError as e:
        logger.error("No se pudo obtener la lista de años: %s", e)
        return _no_disponible(ctx)
    if not anios:
        return _no_disponible(ctx)
    enviar_lista(ctx.tel, "¿De qué *año* quieres consultar?", "Elegir año", [(f"anio:{a}", a) for a in anios], "Años")


def _pedir_periodo(ctx, anio):
    try:
        periodos = api.listar_periodos(anio)
    except api.ApiError as e:
        logger.error("No se pudo obtener la lista de periodos: %s", e)
        return _no_disponible(ctx)
    if not periodos:
        return _no_disponible(ctx)
    enviar_lista(ctx.tel, f"Año *{anio}*. ¿Qué *periodo*?", "Elegir periodo",
                 [(f"periodo:{p}", p) for p in periodos], "Periodos")


# ------------------------------------------------------------------ pasos
def continuar(ctx, st):
    d = estado.datos(st)
    paso = st.paso

    if paso == "anio":
        if ctx.btn_id.startswith("anio:"):
            anio = ctx.btn_id[5:]
            estado.avanzar(st, "periodo", anio=anio)
            _pedir_periodo(ctx, anio)
        else:
            _pedir_anio(ctx)
        return True

    if paso == "periodo":
        if ctx.btn_id.startswith("periodo:"):
            estado.avanzar(st, "dni", periodo=ctx.btn_id[8:])
            enviar_texto(ctx.tel, "Escribe el *DNI del estudiante* (solo números).")
        else:
            _pedir_periodo(ctx, d.get("anio", ""))
        return True

    if paso == "dni":
        dni = (ctx.txt or "").strip() if ctx.tipo == "text" else ""
        if dni.isdigit() and len(dni) in Config.ID_LENGTHS:
            if Config.ACAD_VERIFY_MODE == "fecha_nacimiento":
                estado.avanzar(st, "fecha", dni=dni)
                enviar_texto(ctx.tel, "Por seguridad, escribe la *fecha de nacimiento* del estudiante (DD/MM/AAAA).")
                return True
            return _consultar(ctx, st, dni, None)
        largos = " o ".join(str(n) for n in Config.ID_LENGTHS)
        enviar_texto(ctx.tel, f"El DNI debe tener {largos} dígitos y solo números. Inténtalo de nuevo o escribe *cancelar*.")
        return True

    if paso == "fecha":
        fecha = _parse_fecha(ctx.txt) if ctx.tipo == "text" else None
        if not fecha:
            enviar_texto(ctx.tel, "No entendí la fecha. Usa el formato *DD/MM/AAAA* (ej. 20/05/2010) o escribe *cancelar*.")
            return True
        return _consultar(ctx, st, d.get("dni", ""), fecha)

    estado.terminar(ctx.tel)
    return False


# ------------------------------------------------------------------ consulta
def _fallo(ctx, tipo, dni, anio, periodo):
    usu = ctx.usu
    usu.intentos_acad = (usu.intentos_acad or 0) + 1
    if usu.intentos_acad >= Config.ACAD_MAX_ATTEMPTS:
        usu.bloqueado_acad_hasta = utcnow() + timedelta(minutes=Config.ACAD_LOCK_MINUTES)
        usu.intentos_acad = 0
    db.session.commit()
    _auditar(ctx, tipo, dni, anio, periodo, "no_coincide")
    estado.terminar(ctx.tel)
    # Mismo mensaje si el DNI no existe o el segundo dato no coincide (no revela cuál falló)
    enviar_botones(ctx.tel, "No encontré un estudiante con esos datos. Revisa la información e inténtalo de nuevo.",
                   [("acc_" + tipo, "🔁 Intentar de nuevo"), ("f_asesor", "🙋 Hablar con asesor"), ("ir_inicio", "🏠 Inicio")])
    return True


def _consultar(ctx, st, dni, fecha):
    d = estado.datos(st)
    tipo, anio, periodo = d.get("tipo", "deudas"), d.get("anio", ""), d.get("periodo", "")
    if _bloqueado(ctx.usu):
        estado.terminar(ctx.tel)
        enviar_texto(ctx.tel, "🔒 Superaste el máximo de intentos. Vuelve a intentarlo más tarde o contacta a un asesor.")
        return True

    necesita_deudas = tipo == "deudas" or Config.ACAD_NOTAS_REQUIERE_PAGOS_AL_DIA
    try:
        base = api.consultar_deudas(anio, periodo, dni) if necesita_deudas else api.consultar_notas(anio, periodo, dni)
    except api.NoEncontrado:
        return _fallo(ctx, tipo, dni, anio, periodo)
    except api.ApiError as e:
        logger.error("Fallo de la API académica: %s", e)
        _auditar(ctx, tipo, dni, anio, periodo, "error")
        _no_disponible(ctx)
        return True

    if not _verificar(base["estudiante"], ctx.tel, fecha):
        return _fallo(ctx, tipo, dni, anio, periodo)
    ctx.usu.intentos_acad = 0
    db.session.commit()
    estado.terminar(ctx.tel)
    nombre = _nombre_visible(base["estudiante"])

    if tipo == "deudas":
        _auditar(ctx, tipo, dni, anio, periodo, "ok")
        _reporte_deudas(ctx, base, nombre, periodo)
        return True

    # --- notas ---
    if Config.ACAD_NOTAS_REQUIERE_PAGOS_AL_DIA and base["total_pendiente"] > 0:
        _auditar(ctx, tipo, dni, anio, periodo, "pagos_pendientes")
        enviar_botones(ctx.tel,
                       f"📝 Para ver las notas de *{nombre}* debes estar al día en tus pagos.\n\n"
                       f"Pendiente: *{_dinero(base['moneda'], base['total_pendiente'])}*.\n"
                       "Si ya pagaste, envía tu comprobante y el área contable lo validará.",
                       [("comp_enviar", "📤 Enviar comprobante"), ("acc_deudas", "💳 Ver mis pagos"), ("ir_inicio", "🏠 Inicio")])
        return True
    try:
        notas = api.consultar_notas(anio, periodo, dni)
    except api.NoEncontrado:
        _auditar(ctx, tipo, dni, anio, periodo, "ok")
        enviar_botones(ctx.tel, f"Aún no hay notas registradas para *{periodo}*.",
                       [("acc_notas", "📝 Otra consulta"), ("ir_inicio", "🏠 Inicio")])
        return True
    except api.ApiError as e:
        logger.error("Fallo de la API académica (notas): %s", e)
        _auditar(ctx, tipo, dni, anio, periodo, "error")
        _no_disponible(ctx)
        return True
    _auditar(ctx, tipo, dni, anio, periodo, "ok")
    _reporte_notas(ctx, notas, nombre, periodo)
    return True


# ------------------------------------------------------------------ reportes
def _reporte_deudas(ctx, data, nombre, periodo):
    m = data["moneda"]
    lineas = [f"💳 *Estado de pagos*", f"👤 {nombre}", f"📅 Periodo {periodo}", ""]
    if data["deudas"]:
        for x in data["deudas"]:
            venc = f" — vence {_fecha_visible(x['vencimiento'])}" if x["vencimiento"] else ""
            icono = "✅" if x["estado"].lower() in ("pagado", "pagada", "cancelado") else "⏳"
            lineas.append(f"{icono} {x['concepto']}: {_dinero(m, x['monto'])}{venc} ({x['estado']})")
        lineas.append("")
    if data["total_pendiente"] > 0:
        lineas.append(f"*Total pendiente: {_dinero(m, data['total_pendiente'])}*")
        botones = [("comp_enviar", "📤 Enviar comprobante"), ("acc_notas", "📝 Mis notas"), ("ir_inicio", "🏠 Inicio")]
    else:
        lineas.append("✅ *¡Estás al día en tus pagos!*")
        botones = [("acc_notas", "📝 Mis notas"), ("acc_deudas", "💳 Otra consulta"), ("ir_inicio", "🏠 Inicio")]
    _enviar_largo(ctx.tel, lineas, "[Reporte de pagos enviado]")
    enviar_botones(ctx.tel, "¿Necesitas algo más?", botones)


def _reporte_notas(ctx, data, nombre, periodo):
    lineas = ["📝 *Notas*", f"👤 {nombre}", f"📅 Periodo {periodo}", ""]
    if not data["cursos"]:
        lineas.append("No hay notas registradas para este periodo.")
    for c in data["cursos"]:
        obs = f" — {c['observacion']}" if c["observacion"] else ""
        lineas.append(f"• {c['curso']}: *{c['nota'] if c['nota'] is not None else '-'}*{obs}")
    if data.get("promedio") is not None:
        lineas += ["", f"*Promedio: {data['promedio']}*"]
    _enviar_largo(ctx.tel, lineas, "[Reporte de notas enviado]")
    enviar_botones(ctx.tel, "¿Necesitas algo más?",
                   [("acc_notas", "📝 Otra consulta"), ("acc_deudas", "💳 Mis pagos"), ("ir_inicio", "🏠 Inicio")])
