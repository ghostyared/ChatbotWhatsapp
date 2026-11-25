"""
Flujo guiado "SOY ALUMNO" conectado a los endpoints configurables (/admin/endpoints):
  1. Solicita DNI -> POST periodos_estudiante (PeriodosEstudiante.php)
  2. Confirma que es el estudiante encontrado
  3. Si tiene varios periodos/empresas, permite elegir cuál consultar (si tiene 1, lo selecciona automáticamente)
  4. Guarda IdEstudiante, IdEmpresa, IdCarrera, IdCiclo, IdPeriodo y muestra menú:
       - Notas          (endpoint: consultar_notas)
       - Cuotas de pago (endpoint: consultar_pagos -> ConsultarPagos.php)
       - Comunicados    (endpoint: consultar_comunicados)
"""
import logging
import unicodedata
from datetime import datetime, timedelta

from config import Config
from .. import endpoints_client as ep_cli
from ..models import AuditoriaConsulta, RespuestaBot, db, utcnow
from ..services import enviar_botones, enviar_lista, enviar_texto
from . import estado

logger = logging.getLogger(__name__)
MAX_MSG = 3800


# ------------------------------------------------------------------ utilidades
def esta_activo():
    """True si el endpoint principal 'periodos_estudiante' está activo en BD (y no estamos en modo mock)."""
    if Config.ACAD_API_MOCK:
        return False
    try:
        return ep_cli.obtener_endpoint("periodos_estudiante", solo_activo=True) is not None
    except Exception:
        return False


def _mask(dni):
    return "****" + dni[-4:] if dni else ""


def _bloqueado(usu):
    return bool(usu.bloqueado_acad_hasta and usu.bloqueado_acad_hasta > utcnow())


def _auditar(ctx, tipo, dni, anio, periodo, resultado):
    db.session.add(AuditoriaConsulta(
        telefono=ctx.tel, tipo=tipo, dni_mask=_mask(dni),
        anio=str(anio or "")[:10], periodo=str(periodo or "")[:20], resultado=resultado,
    ))
    db.session.commit()
    logger.info("alumno %s tel=%s dni=%s periodo=%s -> %s", tipo, ctx.tel, _mask(dni), periodo, resultado)


def _pick_ci(d, *claves, default=None):
    """Busca claves en un dict sin distinguir mayúsculas/minúsculas."""
    if not isinstance(d, dict):
        return default
    mapa = {str(k).lower(): v for k, v in d.items()}
    for c in claves:
        v = mapa.get(c.lower())
        if v not in (None, ""):
            return v
    return default


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


_MESES_ES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def _fecha_visible(valor):
    if not valor:
        return ""
    s = str(valor).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(s[:19], fmt)
            return f"{dt.day:02d} {_MESES_ES[dt.month - 1]} {dt.year}"
        except ValueError:
            pass
    return s[:10]


def _enviar_largo(tel, lineas, registro):
    bloque = ""
    for linea in lineas:
        if len(bloque) + len(linea) + 1 > MAX_MSG:
            enviar_texto(tel, bloque.rstrip(), registro=registro)
            bloque = ""
        bloque += linea + "\n"
    if bloque.strip():
        enviar_texto(tel, bloque.rstrip(), registro=registro)


def _limpiar_etiqueta(txt):
    return str(txt or "").replace("_", " ").strip()


def _sin_tildes(txt):
    s = str(txt or "").lower().strip()
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _es_pedagogico(sel):
    """True si la matrícula corresponde al Instituto Pedagógico (no Colegio)."""
    emp = _sin_tildes((sel or {}).get("NombreEmpresa"))
    car = _sin_tildes((sel or {}).get("Carrera"))
    if "colegio" in emp:
        return False
    palabras_inst = ("instituto", "pedagogic", "iespp", "eespp", "superior")
    return any(w in emp or w in car for w in palabras_inst)


def _registrar_fallo(ctx, dni, motivo="no_coincide"):
    usu = ctx.usu
    usu.intentos_acad = (usu.intentos_acad or 0) + 1
    if usu.intentos_acad >= Config.ACAD_MAX_ATTEMPTS:
        usu.bloqueado_acad_hasta = utcnow() + timedelta(minutes=Config.ACAD_LOCK_MINUTES)
        usu.intentos_acad = 0
    db.session.commit()
    _auditar(ctx, "alumno", dni, "", "", motivo)


def _variables_de(d):
    sel = d.get("seleccion") or {}
    return {
        "dni": d.get("dni", ""),
        "IdEstudiante": sel.get("IdEstudiante") or d.get("IdEstudiante") or "",
        "IdEmpresa": sel.get("IdEmpresa") or "",
        "IdCarrera": sel.get("IdCarrera") or "",
        "IdCiclo": sel.get("IdCiclo") or "",
        "IdPeriodo": sel.get("IdPeriodo") or "",
        "IdInscripcion": sel.get("IdInscripcion") or "",
    }


# ------------------------------------------------------------------ inicio
def iniciar(ctx, intencion_inicial=None):
    if _bloqueado(ctx.usu):
        enviar_texto(ctx.tel, "🔒 Superaste el máximo de intentos. Vuelve a intentarlo más tarde o contacta a un asesor.")
        return
    estado.iniciar(ctx.tel, "alumno", "dni", {"intencion": intencion_inicial or ""})
    if intencion_inicial == "notas":
        texto = ("📝 *Consulta de Notas (Colegio)*\n\nPor favor, escribe tu número de *DNI* (solo números) para buscar tu matrícula y mostrarte tu boleta.")
    elif intencion_inicial == "pagos":
        texto = ("💳 *Consulta de Cuotas de Pago*\n\nPor favor, escribe tu número de *DNI* (solo números) para buscar tu matrícula y revisar tus cuotas.")
    else:
        intro = RespuestaBot.query.filter_by(clave="acc_alumno").first()
        texto = (intro.texto if intro and intro.texto else
                 "🎓 *Portal del Alumno*\n\nPor favor, escribe tu número de *DNI* (solo números) para buscar tus matrículas y periodos.\n\n"
                 "_Si ya realizaste un pago y deseas enviar tu comprobante, usa el botón de abajo._")
    enviar_botones(ctx.tel, texto, [("comp_enviar", "📤 Enviar comprobante"), ("ir_inicio", "🏠 Inicio")])


def iniciar_pagos(ctx):
    iniciar(ctx, intencion_inicial="pagos")


def iniciar_notas(ctx):
    iniciar(ctx, intencion_inicial="notas")


# ------------------------------------------------------------------ pasos
def continuar(ctx, st):
    d = estado.datos(st)
    paso = st.paso

    if paso == "dni":
        return _paso_dni(ctx, st)
    if paso == "confirmar":
        return _paso_confirmar(ctx, st, d)
    if paso == "periodo":
        return _paso_periodo(ctx, st, d)
    if paso == "menu":
        return _paso_menu(ctx, st, d)

    estado.terminar(ctx.tel)
    return False


def _supero_tope_diario(tel):
    """Evita enumeración masiva de DNIs limitando las consultas académicas por teléfono/día."""
    if Config.ACAD_MAX_CONSULTAS_DIA <= 0:
        return False
    inicio_dia = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    hoy = AuditoriaConsulta.query.filter(
        AuditoriaConsulta.telefono == tel,
        AuditoriaConsulta.fecha >= inicio_dia,
    ).count()
    return hoy >= Config.ACAD_MAX_CONSULTAS_DIA


def _paso_dni(ctx, st):
    if _bloqueado(ctx.usu):
        estado.terminar(ctx.tel)
        enviar_texto(ctx.tel, "🔒 Superaste el máximo de intentos. Vuelve a intentarlo más tarde o contacta a un asesor.")
        return True

    if _supero_tope_diario(ctx.tel):
        estado.terminar(ctx.tel)
        enviar_texto(ctx.tel, "🔒 Alcanzaste el límite diario de consultas académicas. Si necesitas ayuda adicional, solicita un asesor.")
        return True

    dni = (ctx.txt or "").strip() if ctx.tipo == "text" else ""
    if not (dni.isdigit() and len(dni) in Config.ID_LENGTHS):
        largos = " o ".join(str(n) for n in Config.ID_LENGTHS)
        enviar_texto(ctx.tel, f"El DNI debe tener {largos} dígitos y solo números. Inténtalo de nuevo o escribe *cancelar*.")
        return True

    res = ep_cli.ejecutar("periodos_estudiante", {"dni": dni})
    if not res["ok"] or not isinstance(res["data"], dict):
        logger.error("Error consultando periodos_estudiante: %s", res.get("error"))
        _auditar(ctx, "alumno", dni, "", "", "error")
        enviar_botones(ctx.tel, "😕 El servicio de consulta no está disponible en este momento. Inténtalo más tarde.",
                       [("acc_alumno", "🔁 Reintentar"), ("f_asesor", "🙋 Hablar con asesor"), ("ir_inicio", "🏠 Inicio")])
        estado.terminar(ctx.tel)
        return True

    data = res["data"]
    est = data.get("estudiante") if isinstance(data.get("estudiante"), dict) else {}
    periodos_raw = data.get("periodos") if isinstance(data.get("periodos"), list) else []

    if not data.get("success") or not est or not periodos_raw:
        _registrar_fallo(ctx, dni, "no_coincide")
        enviar_botones(ctx.tel, "No encontré matrículas registradas para ese DNI. Verifica el número e inténtalo de nuevo.",
                       [("acc_alumno", "🔁 Intentar de nuevo"), ("f_asesor", "🙋 Hablar con asesor"), ("ir_inicio", "🏠 Inicio")])
        estado.terminar(ctx.tel)
        return True

    # No reiniciamos intentos_acad en cada DNI válido (evita evadir el bloqueo alternando 1 DNI válido)
    _auditar(ctx, "alumno", dni, "", "", "ok")

    nombre = (
        est.get("NombreCompleto")
        or " ".join(p for p in (est.get("Nombres"), est.get("ApellidoP"), est.get("ApellidoM")) if p)
        or "Estudiante"
    ).strip()

    periodos = []
    for p in periodos_raw[:10]:
        if not isinstance(p, dict):
            continue
        periodos.append({
            "IdInscripcion": p.get("IdInscripcion"),
            "IdEstudiante": p.get("IdEstudiante") or est.get("IdEstudiante"),
            "IdEmpresa": p.get("IdEmpresa"),
            "NombreEmpresa": str(p.get("NombreEmpresa") or "Institución").strip(),
            "TipoEmpresa": p.get("TipoEmpresa"),
            "IdPeriodo": p.get("IdPeriodo"),
            "NombrePeriodo": str(p.get("NombrePeriodo") or "").strip(),
            "IdCarrera": p.get("IdCarrera"),
            "Carrera": _limpiar_etiqueta(p.get("Carrera")),
            "IdCiclo": p.get("IdCiclo"),
            "Ciclo": _limpiar_etiqueta(p.get("Ciclo")),
            "CorreoInstitucional": str(p.get("CorreoInstitucional") or "").strip(),
        })

    if not periodos:
        _registrar_fallo(ctx, dni, "no_coincide")
        enviar_botones(ctx.tel, "No encontré periodos activos para ese DNI.",
                       [("acc_alumno", "🔁 Intentar de nuevo"), ("ir_inicio", "🏠 Inicio")])
        estado.terminar(ctx.tel)
        return True

    estado.avanzar(
        st, "confirmar",
        dni=dni,
        IdEstudiante=est.get("IdEstudiante"),
        nombre_estudiante=nombre,
        periodos=periodos,
    )

    if len(periodos) == 1:
        p0 = periodos[0]
        detalle = (
            f"🏫 *{p0['NombreEmpresa']}*\n"
            f"📚 {p0['Carrera'] or '-'} · {p0['Ciclo'] or '-'}\n"
            f"📅 Periodo: *{p0['NombrePeriodo']}*"
        )
    else:
        detalle = f"📋 Matrículas encontradas: *{len(periodos)}* periodo(s)"

    enviar_botones(
        ctx.tel,
        f"👤 Encontré al estudiante:\n*{nombre}*\n{detalle}\n\n¿Confirmas que eres tú?",
        [("alu_conf:si", "✅ Sí, soy yo"), ("alu_conf:no", "❌ No, cambiar DNI")],
        registro="[Confirmación de estudiante enviada]",
    )
    return True


def _ejecutar_tras_seleccion(ctx, st, d, sel):
    """Tras elegir periodo, ejecuta directamente la intención inicial (notas/pagos) o muestra el menú."""
    intencion = d.get("intencion") or ""
    estado.avanzar(st, "menu", seleccion=sel, intencion="")
    d_act = estado.datos(st)
    if intencion == "pagos":
        _consultar_pagos(ctx, st, d_act)
    elif intencion == "notas":
        if _es_pedagogico(sel):
            enviar_botones(
                ctx.tel,
                "📝 *Consulta de notas*\n\nPor el momento la consulta de notas por WhatsApp está disponible únicamente para estudiantes de *Colegio*.\n"
                "Para el *Instituto Pedagógico*, comunícate con secretaría académica o un asesor.",
                [("alu_op:pagos", "💳 Cuotas de pago"), ("acc_asesor", "📞 Hablar con asesor"), ("ir_inicio", "🏠 Inicio")],
            )
        else:
            _consultar_generico(ctx, st, d_act, "consultar_notas", "notas", "📝 *Notas*", _formatear_notas)
    else:
        _mostrar_menu_alumno(ctx, d_act.get("nombre_estudiante", "Estudiante"), sel)


def _paso_confirmar(ctx, st, d):
    resp_txt = (ctx.txt or "").strip().lower()
    if ctx.btn_id == "alu_conf:no" or resp_txt in ("no", "cambiar", "cambiar dni"):
        estado.avanzar(st, "dni", dni="", periodos=[], seleccion=None)
        enviar_texto(ctx.tel, "Entendido. Escribe nuevamente tu número de *DNI* (solo números) o escribe *cancelar*.")
        return True

    if ctx.btn_id == "alu_conf:si" or resp_txt in ("si", "sí", "confirmo", "ok"):
        periodos = d.get("periodos") or []
        if len(periodos) == 1:
            _ejecutar_tras_seleccion(ctx, st, d, periodos[0])
        else:
            estado.avanzar(st, "periodo")
            _pedir_periodo(ctx, periodos)
        return True

    enviar_botones(
        ctx.tel,
        "Por favor confirma si los datos corresponden a tu matrícula:",
        [("alu_conf:si", "✅ Sí, soy yo"), ("alu_conf:no", "❌ No, cambiar DNI")],
    )
    return True


def _pedir_periodo(ctx, periodos):
    filas = []
    for idx, p in enumerate(periodos[:10]):
        titulo = f"{p['NombrePeriodo']} - {p['NombreEmpresa']}"[:24]
        desc = f"{p['NombreEmpresa']} · {p['Carrera']} ({p['Ciclo']})"[:72]
        filas.append((f"alu_per:{idx}", titulo, desc))
    enviar_lista(
        ctx.tel,
        "Tienes más de una matrícula registrada.\n\nSelecciona la *institución y periodo* que deseas consultar:",
        "Elegir matrícula",
        filas,
        "Mis matrículas",
    )


def _paso_periodo(ctx, st, d):
    periodos = d.get("periodos") or []
    if ctx.btn_id.startswith("alu_per:"):
        try:
            idx = int(ctx.btn_id.split(":", 1)[1])
            sel = periodos[idx]
        except (ValueError, IndexError):
            _pedir_periodo(ctx, periodos)
            return True
        _ejecutar_tras_seleccion(ctx, st, d, sel)
        return True

    _pedir_periodo(ctx, periodos)
    return True


def _mostrar_menu_alumno(ctx, nombre, sel):
    texto = (
        f"✅ *{nombre}*\n"
        f"🏫 {sel.get('NombreEmpresa', '')}\n"
        f"📚 {sel.get('Carrera', '-')} · {sel.get('Ciclo', '-')}\n"
        f"📅 Periodo: *{sel.get('NombrePeriodo', '')}*\n\n"
        "¿Qué deseas consultar?"
    )
    enviar_botones(
        ctx.tel,
        texto,
        [("alu_op:notas", "📝 Notas"), ("alu_op:pagos", "💳 Cuotas de pago"), ("alu_op:comunicados", "📢 Comunicados")],
        registro="[Menú de consultas de alumno enviado]",
    )


def _paso_menu(ctx, st, d):
    sel = d.get("seleccion") or {}
    nombre = d.get("nombre_estudiante") or "Estudiante"
    op = ctx.btn_id

    if op == "alu_menu":
        estado.avanzar(st, "menu")
        _mostrar_menu_alumno(ctx, nombre, sel)
        return True

    if op == "alu_cambiar_per":
        periodos = d.get("periodos") or []
        if len(periodos) > 1:
            estado.avanzar(st, "periodo")
            _pedir_periodo(ctx, periodos)
        else:
            _mostrar_menu_alumno(ctx, nombre, sel)
        return True

    if op == "alu_op:pagos":
        _consultar_pagos(ctx, st, d)
        return True

    if op == "alu_op:notas":
        if _es_pedagogico(sel):
            enviar_botones(
                ctx.tel,
                "📝 *Consulta de notas*\n\nPor el momento la consulta de notas por WhatsApp está disponible únicamente para estudiantes de *Colegio*.\n"
                "Para el *Instituto Pedagógico*, comunícate con secretaría académica o un asesor.",
                [("alu_op:pagos", "💳 Cuotas de pago"), ("f_asesor", "🙋 Hablar con asesor"), ("ir_inicio", "🏠 Inicio")],
            )
            return True
        _consultar_generico(ctx, st, d, "consultar_notas", "notas", "📝 *Notas*", _formatear_notas)
        return True

    if op == "alu_op:comunicados":
        _consultar_generico(ctx, st, d, "consultar_comunicados", "comunicados", "📢 *Comunicados*", _formatear_comunicados)
        return True

    if ctx.tipo == "text" and not op:
        from ..asistente import _detectar_intencion_directa
        if _detectar_intencion_directa(ctx.txt) == "comprobante":
            estado.terminar(ctx.tel)
            return False

    _mostrar_menu_alumno(ctx, nombre, sel)
    return True


# ------------------------------------------------------------------ consultas específicas
def _consultar_pagos(ctx, st, d):
    sel = d.get("seleccion") or {}
    dni = d.get("dni", "")
    nombre = d.get("nombre_estudiante") or "Estudiante"
    periodo_nom = sel.get("NombrePeriodo", "")
    variables = _variables_de(d)

    ep = ep_cli.obtener_endpoint("consultar_pagos", solo_activo=True)
    if not ep:
        enviar_botones(
            ctx.tel,
            "💳 La consulta de *cuotas de pago* no está habilitada temporalmente.",
            [("alu_menu", "⬅️ Volver al menú"), ("ir_inicio", "🏠 Inicio")],
        )
        return

    res = ep_cli.ejecutar(ep, variables)
    if not res["ok"] or res["data"] is None:
        logger.error("Error consultando consultar_pagos: %s", res.get("error"))
        _auditar(ctx, "pagos", dni, "", periodo_nom, "error")
        enviar_botones(
            ctx.tel,
            "😕 No se pudieron obtener tus cuotas de pago en este momento. Inténtalo más tarde.",
            [("alu_menu", "🔁 Volver"), ("f_asesor", "🙋 Hablar con asesor"), ("ir_inicio", "🏠 Inicio")],
        )
        return

    estado.avanzar(st, "menu")
    _auditar(ctx, "pagos", dni, "", periodo_nom, "ok")
    lineas, hay_pendiente = _formatear_pagos(res["data"], nombre, sel)
    _enviar_largo(ctx.tel, lineas, "[Reporte de cuotas de pago enviado]")
    botones = [("comp_enviar", "📤 Enviar comprobante"), ("alu_menu", "🔁 Otra consulta"), ("ir_inicio", "🏠 Inicio")]
    if not hay_pendiente and len(d.get("periodos") or []) > 1:
        botones = [("alu_menu", "🔁 Otra consulta"), ("alu_cambiar_per", "📅 Otro periodo"), ("ir_inicio", "🏠 Inicio")]
    enviar_botones(ctx.tel, "¿Deseas realizar otra consulta?", botones)


def _consultar_generico(ctx, st, d, clave_ep, tipo_aud, titulo, formateador):
    sel = d.get("seleccion") or {}
    dni = d.get("dni", "")
    nombre = d.get("nombre_estudiante") or "Estudiante"
    periodo_nom = sel.get("NombrePeriodo", "")
    variables = _variables_de(d)

    ep = ep_cli.obtener_endpoint(clave_ep, solo_activo=True)
    if not ep:
        enviar_botones(
            ctx.tel,
            f"{titulo}\n\nEsta consulta estará disponible muy pronto. Mientras tanto puedes revisar tus cuotas de pago.",
            [("alu_op:pagos", "💳 Cuotas de pago"), ("alu_menu", "⬅️ Opciones"), ("ir_inicio", "🏠 Inicio")],
        )
        return

    res = ep_cli.ejecutar(ep, variables)
    if not res["ok"] or res["data"] is None:
        logger.error("Error consultando %s: %s", clave_ep, res.get("error"))
        _auditar(ctx, tipo_aud, dni, "", periodo_nom, "error")
        enviar_botones(
            ctx.tel,
            f"😕 No se pudo consultar {tipo_aud} en este momento. Inténtalo más tarde.",
            [("alu_menu", "🔁 Volver"), ("f_asesor", "🙋 Hablar con asesor"), ("ir_inicio", "🏠 Inicio")],
        )
        return

    estado.avanzar(st, "menu")
    _auditar(ctx, tipo_aud, dni, "", periodo_nom, "ok")
    lineas = formateador(res["data"], nombre, sel)
    _enviar_largo(ctx.tel, lineas, f"[Reporte de {tipo_aud} enviado]")
    enviar_botones(ctx.tel, "¿Deseas realizar otra consulta?",
                   [("alu_menu", "🔁 Otra consulta"), ("comp_enviar", "📤 Enviar comprobante"), ("ir_inicio", "🏠 Inicio")])


# ------------------------------------------------------------------ formateadores tolerantes
def _extraer_lista(data, claves):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for c in claves:
            v = _pick_ci(data, c)
            if isinstance(v, list):
                return v
    return []


def _formatear_pagos(data, nombre, sel):
    moneda = str(_pick_ci(data, "moneda", "simbolo", default="S/")).strip() or "S/"
    items = _extraer_lista(data, ("cuotas", "pagos", "deudas", "cronograma", "detalle", "items", "data", "resultado"))
    resumen = data.get("resumen") if isinstance(data, dict) and isinstance(data.get("resumen"), dict) else {}

    lineas = [
        "💳 *Cuotas de pago*",
        f"👤 {nombre}",
        f"🏫 {sel.get('NombreEmpresa', '')} · Periodo {sel.get('NombrePeriodo', '')}",
        "",
    ]

    if not items:
        msg = _pick_ci(data, "mensaje", "message", "detalle")
        lineas.append(str(msg) if msg else "No se encontraron cuotas registradas para este periodo.")
        return lineas, False

    lineas.append("📋 *Cuota | Vence | Monto | Estado*")
    calculado = 0.0
    tiene_pendientes = False

    for idx, it in enumerate(items, 1):
        if not isinstance(it, dict):
            lineas.append(f"• {it}")
            continue

        concepto = str(_pick_ci(
            it, "nombrecuota", "concepto", "descripcion", "nombreconcepto", "cuota", "detalle", "mes", "tipopago",
            default=f"Cuota {idx}",
        )).strip()

        estado_raw = _pick_ci(it, "estado", "situacion", "estadopago", "status")
        pagado_flag = _pick_ci(it, "pagado", "cancelado")
        if estado_raw is None and pagado_flag is not None:
            estado_txt = "CANCELADO" if str(pagado_flag).lower() in ("1", "true", "si", "sí") else "PENDIENTE"
        else:
            estado_txt = str(estado_raw or "").strip().upper()

        es_pagado = estado_txt in ("PAGADO", "PAGADA", "CANCELADO", "CANCELADA", "ANULADO", "EXONERADO")
        es_vencida = estado_txt in ("DEUDA", "VENCIDO", "VENCIDA", "MORA", "MOROSO")

        if es_pagado:
            icono = "✅"
            monto = _num(_pick_ci(it, "montopagado", "montocancelar", "monto", "importe", "total", "valor"))
        else:
            icono = "🔴" if es_vencida else "⏳"
            tiene_pendientes = True
            monto = _num(_pick_ci(it, "deudapendiente", "montocancelar", "montopendiente", "montopagar", "saldo", "monto", "importe", "deuda", "total", "valor"))
            if monto is not None:
                calculado += monto

        venc = _fecha_visible(_pick_ci(
            it, "fechafinalpago", "fechavencimiento", "fecha_vencimiento", "vencimiento", "fechavenc", "fechalimite", "fecha",
        )) or "-"
        monto_str = f"{moneda} {monto:,.2f}" if monto is not None else "-"
        est_str = estado_txt or "-"
        lineas.append(f"{icono} *{concepto}* | {venc} | {monto_str} | {est_str}")

    lineas.append("")
    if resumen:
        canceladas = resumen.get("cuotas_canceladas")
        vencidas = resumen.get("cuotas_vencidas")
        pendientes_cnt = resumen.get("cuotas_pendientes")
        total_pagado = _num(resumen.get("total_pagado"))
        total_vencida = _num(resumen.get("total_deuda_vencida"))
        total_pend = _num(resumen.get("total_deuda_pendiente"))

        lineas.append("📊 *Resumen:*")
        if canceladas is not None:
            pag_txt = f" ({moneda} {total_pagado:,.2f})" if total_pagado is not None else ""
            lineas.append(f"• Canceladas: *{canceladas}*{pag_txt}")
        if vencidas is not None and int(vencidas or 0) > 0:
            venc_txt = f" ({moneda} {total_vencida:,.2f})" if total_vencida is not None else ""
            lineas.append(f"• 🔴 Vencidas / Deuda: *{vencidas}*{venc_txt}")
        if pendientes_cnt is not None:
            lineas.append(f"• ⏳ Por vencer: *{pendientes_cnt}*")

        pend = total_pend if total_pend is not None else calculado
        if pend > 0:
            lineas.append(f"• *Total pendiente: {moneda} {pend:,.2f}*")
            tiene_pendientes = True
        else:
            lineas.append("✅ *¡Estás al día en tus pagos!*")
            tiene_pendientes = False
    else:
        total_pendiente = _num(_pick_ci(data, "total_pendiente", "totalpendiente", "total_deuda", "totaldeuda", "saldo_total", "saldototal"))
        pend = total_pendiente if total_pendiente is not None else calculado
        if pend > 0:
            lineas.append(f"*Total pendiente: {moneda} {pend:,.2f}*")
            tiene_pendientes = True
        elif not tiene_pendientes:
            lineas.append("✅ *¡Estás al día en tus pagos!*")

    return lineas, tiene_pendientes


def _fmt_nota_val(v):
    if v is None or v == "" or str(v).strip() in ("—", "-"):
        return "-"
    if isinstance(v, (int, float)):
        return f"{float(v):.1f}"
    try:
        return f"{float(str(v).replace(',', '.')):.1f}"
    except ValueError:
        return str(v).strip()


_ABREV_CURSO = (
    ("razonamiento", "Raz."),
    ("comprensión", "Comp."),
    ("comprension", "Comp."),
    ("educación", "Educ."),
    ("educacion", "Educ."),
    ("personal", "Pers."),
    ("matemático", "Matem."),
    ("matematico", "Matem."),
    ("tecnología", "Tec."),
    ("tecnologia", "Tec."),
    ("lectora", "Lect."),
    ("comunicación", "Comunic."),
    ("comunicacion", "Comunic."),
    ("computación", "Comput."),
    ("computacion", "Comput."),
    ("laboratorio", "Laborat."),
    ("ciencia y ", "Ciencia "),
)


def _abreviar_curso(nombre, ancho=12):
    txt = " ".join(str(nombre or "Curso").strip().split())
    # Capitalización legible (primera letra de cada palabra, respetando conectores cortos)
    palabras = []
    for i, w in enumerate(txt.lower().split()):
        if i > 0 and w in ("y", "de", "del", "la", "el", "en", "para"):
            palabras.append(w)
        else:
            palabras.append(w.capitalize())
    res = " ".join(palabras)
    if len(res) <= ancho:
        return res
    low = res.lower()
    for orig, corto in _ABREV_CURSO:
        if orig in low:
            idx = low.find(orig)
            res = res[:idx] + corto + res[idx + len(orig):]
            low = res.lower()
            if len(res) <= ancho:
                return res
    return (res[:ancho - 1].rstrip() + ".") if len(res) > ancho else res


def _formatear_notas(data, nombre, sel):
    mat = data.get("matricula") if isinstance(data, dict) and isinstance(data.get("matricula"), dict) else {}
    est = data.get("estudiante") if isinstance(data, dict) and isinstance(data.get("estudiante"), dict) else {}
    nombre_vis = est.get("NombreCompleto") or nombre
    empresa_vis = mat.get("NombreEmpresa") or sel.get("NombreEmpresa", "")
    carrera_vis = _limpiar_etiqueta(mat.get("Carrera") or sel.get("Carrera", ""))
    ciclo_vis = _limpiar_etiqueta(mat.get("Ciclo") or sel.get("Ciclo", ""))
    periodo_vis = mat.get("NombrePeriodo") or sel.get("NombrePeriodo", "")

    cols = data.get("columnas_subperiodos") if isinstance(data, dict) and isinstance(data.get("columnas_subperiodos"), list) else []
    formato = str(_pick_ci(data, "formato_principal", default="")).strip().lower()
    items = _extraer_lista(data, ("cursos", "notas", "calificaciones", "detalle", "items", "data"))

    cab_meta = " · ".join(x for x in (carrera_vis, ciclo_vis, f"Periodo {periodo_vis}" if periodo_vis else "") if x)
    lineas = [
        "📝 *Boleta de Notas*",
        f"👤 {nombre_vis}",
        f"🏫 {empresa_vis}",
    ]
    if cab_meta:
        lineas.append(f"📚 {cab_meta}")
    lineas.append("")

    if not items:
        msg = _pick_ci(data, "mensaje", "message")
        lineas.append(str(msg) if msg else "Aún no hay notas registradas para este periodo.")
        return lineas

    if cols:
        # Tabla monoespaciada (32 cols para 4 bimestres: alineación perfecta en WhatsApp móvil)
        ancho_curso = 12 if len(cols) <= 4 else 10
        ancho_col = 4
        sep_col = " "
        cab = f"{'CURSO':<{ancho_curso}}" + "".join(f"{sep_col}{str(c):>{ancho_col}}" for c in cols)
        tabla = ["```", cab, "-" * len(cab)]
        for it in items:
            if not isinstance(it, dict):
                tabla.append(str(it)[:len(cab)])
                continue
            curso = _abreviar_curso(
                _pick_ci(it, "curso", "asignatura", "materia", "nombrecurso", "nombre", default="Curso"),
                ancho=ancho_curso,
            )
            nums = it.get("notas_numericas") if isinstance(it.get("notas_numericas"), dict) else {}
            celdas = []
            for col in cols:
                val = nums.get(col) if col in nums else it.get(col)
                celdas.append(f"{_fmt_nota_val(val):>{ancho_col}}")
            tabla.append(f"{curso:<{ancho_curso}}{sep_col}{sep_col.join(celdas)}")
        tabla.append("```")
        lineas.extend(tabla)

        total_pts = data.get("total_pts") if isinstance(data, dict) and isinstance(data.get("total_pts"), dict) else {}
        if total_pts:
            lineas.append("")
            lineas.append("📊 *Puntaje acumulado:*")
            sub_pts = []
            for col in cols:
                pv = _num(total_pts.get(col))
                if pv is not None and pv > 0:
                    sub_pts.append(f"{col}: {pv:,.2f}")
            if sub_pts:
                lineas.append("• " + " | ".join(sub_pts))
            tot_v = _num(total_pts.get("Total"))
            tot_c = data.get("total_cursos") or len(items)
            if tot_v is not None:
                lineas.append(f"• *Total puntos: {tot_v:,.2f}* ({tot_c} cursos)")
        return lineas

    # Fallback para formatos simples (curso + nota)
    for it in items:
        if not isinstance(it, dict):
            lineas.append(f"• {it}")
            continue
        curso = _pick_ci(it, "curso", "asignatura", "materia", "nombrecurso", "nombre", default="Curso")
        nota = _pick_ci(it, "nota", "promfinal", "promedio", "calificacion", "notafinal", default="—")
        obs = _pick_ci(it, "observacion", "estado", default="")
        obs_str = f" — {obs}" if obs else ""
        lineas.append(f"• {curso}: *{nota}*{obs_str}")
    prom = _pick_ci(data, "promedio", "promediogeneral")
    if prom is not None:
        lineas += ["", f"*Promedio: {prom}*"]
    return lineas


def _formatear_comunicados(data, nombre, sel):
    items = _extraer_lista(data, ("comunicados", "avisos", "noticias", "mensajes", "items", "data"))
    lineas = [
        "📢 *Comunicados*",
        f"👤 {nombre}",
        f"🏫 {sel.get('NombreEmpresa', '')}",
        "",
    ]
    if not items:
        msg = _pick_ci(data, "mensaje", "message")
        lineas.append(str(msg) if msg else "No hay comunicados nuevos en este momento.")
        return lineas
    for it in items[:10]:
        if not isinstance(it, dict):
            lineas.append(f"• {it}")
            continue
        tit = _pick_ci(it, "titulo", "asunto", "nombre", default="Comunicado")
        fecha = _fecha_visible(_pick_ci(it, "fecha", "fechapublicacion", "creado"))
        cuerpo = _pick_ci(it, "descripcion", "mensaje", "contenido", "texto", default="")
        cab = f"📌 *{tit}*" + (f" ({fecha})" if fecha else "")
        lineas.append(cab)
        if cuerpo:
            lineas.append(str(cuerpo)[:400])
        lineas.append("")
    return lineas
