"""
Flujo guiado de derivación a Call Center / Asesor humano:
  1. Pregunta qué institución/servicio busca y su nombre (o si prefiere otro número/horario para llamarlo)
  2. Crea/actualiza un Lead con prioridad ALTA en el CRM (/admin/leads)
  3. Notifica al asesor (WhatsApp y/o webhook 'notificar_callcenter' si está activo)
  4. Pausa el bot para que el agente de Call Center atienda el chat o lo llame.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import Config
from .. import endpoints_client as ep_cli
from ..models import Cita, Lead, LeadEvento, UsuarioSistema, db, utcnow
from ..services import enviar_botones, enviar_texto
from . import estado

logger = logging.getLogger(__name__)


def asignar_asesor_round_robin(empresa_id=None):
    """Elige al asesor (callcenter) activo y en línea con menos leads abiertos para la empresa dada."""
    candidatos = UsuarioSistema.query.filter(
        UsuarioSistema.activo.is_(True),
        UsuarioSistema.en_linea.is_(True),
        UsuarioSistema.rol == 'callcenter',
    ).all()
    if not candidatos:
        return None
    if empresa_id:
        especificos = [u for u in candidatos if u.empresa_id == empresa_id]
        generales = [u for u in candidatos if u.empresa_id is None]
        pool = especificos or generales or candidatos
    else:
        pool = candidatos

    mejor, menor_carga = None, None
    for u in pool:
        carga = Lead.query.filter(
            Lead.asignado_a == u.username,
            Lead.estado.in_(('nuevo', 'contactado', 'visita')),
        ).count()
        if carga >= (u.max_chats or 10):
            continue
        if menor_carga is None or carga < menor_carga:
            mejor, menor_carga = u.username, carga
    return mejor


def en_horario_atencion(ahora_local=None):
    """Devuelve True si la hora actual (en APP_TIMEZONE) está dentro del horario de atención humana."""
    if ahora_local is None:
        try:
            tz = ZoneInfo(Config.APP_TIMEZONE)
        except Exception:
            tz = None
        ahora_local = datetime.now(tz)
    return (
        ahora_local.weekday() in Config.ATENCION_DIAS
        and Config.ATENCION_HORA_INICIO <= ahora_local.hour < Config.ATENCION_HORA_FIN
    )


def notificar_callcenter(lead):
    """Notifica al asesor por WhatsApp (ASESOR_TELEFONO) y opcionalmente vía endpoint 'notificar_callcenter'."""
    avisado = False
    if Config.ASESOR_TELEFONO:
        texto = (
            f"📞 *Solicitud de Asesor / Llamada #{lead.id}*\n"
            f"👤 {lead.nombre or 'Sin nombre'}\n"
            f"📱 WhatsApp: +{lead.telefono}\n"
            f"{'☎️ Tel. llamada: ' + lead.telefono_contacto + chr(10) if lead.telefono_contacto else ''}"
            f"🏫 {lead.sede_nombre or 'Por definir'} · {lead.nivel or '-'}\n"
            f"🔥 Prioridad: *{(lead.prioridad or 'alta').upper()}*\n"
            f"📝 {lead.resumen_ia or lead.comentario or ''}"
        )
        resp = enviar_texto(Config.ASESOR_TELEFONO, texto, registro=f"[Aviso Call Center] lead #{lead.id}")
        avisado = resp is not None and resp.status_code == 200

    # Webhook opcional hacia CRM externo / Teams / central de Call Center
    ep = ep_cli.obtener_endpoint("notificar_callcenter", solo_activo=True)
    if ep:
        res = ep_cli.ejecutar(ep, {
            "lead_id": lead.id,
            "nombre": lead.nombre or "",
            "telefono": lead.telefono_contacto or lead.telefono or "",
            "sede": lead.sede_nombre or lead.sede_id or "",
            "nivel": lead.nivel or "",
            "prioridad": lead.prioridad or "alta",
            "resumen": (lead.resumen_ia or lead.comentario or "")[:500],
        })
        if res["ok"]:
            avisado = True
        else:
            logger.warning("Webhook notificar_callcenter falló: %s", res.get("error"))
    return avisado


def consultar_dni_externo(dni):
    """Consulta nombres y apellidos oficiales usando el endpoint 'consultar_dni_externo' de /admin/endpoints."""
    ep = ep_cli.obtener_endpoint("consultar_dni_externo", solo_activo=True)
    if not ep:
        return {"ok": False, "error": "endpoint_inactivo"}
    res = ep_cli.ejecutar(ep, {"dni": dni})
    if not res.get("ok") or not isinstance(res.get("data"), dict):
        logger.warning("Consulta DNI externo falló para ****%s: %s", dni[-4:], res.get("error"))
        return {"ok": False, "error": res.get("error") or "sin_respuesta"}
    data = res["data"]
    r = data.get("result") if isinstance(data.get("result"), dict) else data
    if not data.get("success") or not isinstance(r, dict):
        return {"ok": False, "error": "no_encontrado"}
    nombres = str(r.get("name") or r.get("nombres") or "").strip()
    paternal = str(r.get("paternal") or r.get("apellido_paterno") or "").strip()
    maternal = str(r.get("maternal") or r.get("apellido_materno") or "").strip()
    completo = " ".join(p for p in (nombres, paternal, maternal) if p).strip()
    if not completo:
        return {"ok": False, "error": "sin_nombre"}
    return {
        "ok": True,
        "dni": str(r.get("dni") or dni).strip()[:20],
        "nombres": nombres[:120],
        "paternal": paternal[:80],
        "maternal": maternal[:80],
        "nombre_completo": completo[:200],
    }


def _pedir_identificacion(ctx, st, d, intro):
    """Pide DNI (8 dígitos) para validar identidad o confirma directamente si ya está verificado en BD."""
    if ctx.usu.dni and getattr(ctx.usu, "nombre_completo", None):
        estado.avanzar(
            st, "confirmar_dni",
            dni=ctx.usu.dni,
            nombres=ctx.usu.nombres or "",
            paternal=ctx.usu.apellido_paterno or "",
            maternal=ctx.usu.apellido_materno or "",
            nombre_completo=ctx.usu.nombre_completo,
        )
        enviar_botones(
            ctx.tel,
            f"{intro}\n\n👤 Tengo registrado tu DNI *****{ctx.usu.dni[-4:]}*:\n"
            f"*{ctx.usu.nombre_completo}*\n\n¿Confirmas que eres tú?",
            [("ase_dni_ok:si", "✅ Sí, soy yo"), ("ase_dni_ok:no", "✏️ Cambiar DNI")],
            registro="[Confirmación de DNI guardado enviada]",
        )
        return
    estado.avanzar(st, "datos")
    enviar_texto(
        ctx.tel,
        f"{intro}\n\n🪪 Para registrar tu atención con tus datos oficiales, escribe tu número de *DNI* (8 dígitos).\n"
        "_Si no cuentas con DNI peruano, escribe tu nombre completo._",
    )


def _finalizar_registro(ctx, d, nombre_lead, detalle, dni="", nombres="", paternal="", maternal=""):
    """Guarda identidad en Usuario, crea/actualiza Lead + Cita, asigna Round-Robin y confirma al usuario."""
    sede = d.get("sede") or "General"
    tipo_cita = d.get("tipo_cita") or "Llamada"
    dia = d.get("dia") or "Próximo día hábil"
    franja = d.get("franja") or ""
    franja_completa = f"{dia} · {franja}".strip(" ·") if franja else ""
    fuera_horario = bool(d.get("fuera_horario"))

    if dni:
        ctx.usu.dni = dni[:20]
        ctx.usu.nombres = (nombres or ctx.usu.nombres or "")[:120]
        ctx.usu.apellido_paterno = (paternal or ctx.usu.apellido_paterno or "")[:80]
        ctx.usu.apellido_materno = (maternal or ctx.usu.apellido_materno or "")[:80]
        ctx.usu.nombre_completo = (nombre_lead or ctx.usu.nombre_completo or "")[:200]
        ctx.usu.dni_verificado = True

    lead = Lead.query.filter_by(telefono=ctx.tel).filter(
        Lead.estado.in_(("nuevo", "contactado", "visita"))
    ).first() or Lead(telefono=ctx.tel)
    lead.nombre = (nombre_lead or ctx.nombre or "Sin nombre")[:120]
    if dni:
        lead.dni = dni[:20]
    lead.sede_nombre = sede[:120]
    lead.comentario = detalle[:400]
    if franja_completa:
        lead.visita_pref = franja_completa[:120]
    lead.resumen_ia = (
        f"{tipo_cita} agendada ({franja_completa}) · {sede}: {detalle}"
        if franja_completa else f"Solicitó asesor en vivo · {sede}: {detalle}"
    )
    lead.quiere_asesor = True
    lead.prioridad = "alta"
    lead.score = "caliente"
    lead.empresa_id = lead.empresa_id or ctx.usu.empresa_id
    if not lead.asignado_a:
        auto_asesor = asignar_asesor_round_robin(lead.empresa_id)
        if auto_asesor:
            lead.asignado_a = auto_asesor
    lead.resultado_llamada = "cita_agendada" if franja_completa else "pendiente"
    lead.estado = "visita" if franja_completa else (lead.estado or "nuevo")
    db.session.add(lead)
    db.session.commit()
    if lead.asignado_a:
        ctx.usu.asignado_a = lead.asignado_a
        db.session.add(LeadEvento(lead_id=lead.id, tipo='asignacion', detalle=f'Auto-asignado (Round-Robin) a {lead.asignado_a}', usuario='sistema'))
        db.session.commit()

    cita_creada = None
    if franja_completa:
        cita_creada = Cita(
            lead_id=lead.id,
            empresa_id=lead.empresa_id,
            telefono=ctx.tel,
            dni=dni[:20] if dni else None,
            nombre=lead.nombre or ctx.nombre or "",
            fecha_preferida=dia[:60],
            franja=franja[:80],
            motivo=f"[{tipo_cita} · {sede}] {detalle}"[:500],
            asignado_a=lead.asignado_a,
            estado="pendiente",
        )
        db.session.add(cita_creada)
        db.session.commit()
        ep_cita = ep_cli.obtener_endpoint("agendar_cita", solo_activo=True)
        if ep_cita:
            ep_cli.ejecutar(ep_cita, {
                "lead_id": lead.id,
                "empresa_id": lead.empresa_id or 0,
                "franja": franja_completa,
                "motivo": detalle[:300],
            })

    lead.asesor_avisado = notificar_callcenter(lead)
    if not fuera_horario and not franja_completa:
        ctx.usu.bot_pausado = True
        ctx.usu.estado_chat = "en_cola"
        ctx.usu.esperando_asesor_desde = utcnow()
    db.session.commit()
    estado.terminar(ctx.tel)

    dni_linea = f"\n🪪 *DNI:* ****{dni[-4:]}" if dni else ""
    if franja_completa:
        cod = f"#C-{cita_creada.id:04d}" if cita_creada else f"#{lead.id}"
        enviar_botones(
            ctx.tel,
            f"📅 *¡Cita confirmada ({cod})!*\n\n"
            f"🏫 *Institución:* {sede}\n"
            f"📌 *Modalidad:* {tipo_cita}\n"
            f"🕒 *Horario:* {franja_completa}\n"
            f"👤 *Titular:* {lead.nombre}{dni_linea}\n\n"
            "Agendamos tu llamada/cita y un asesor se comunicará contigo en ese horario. ¡Gracias!",
            [("CMD_MAIN_MENU", "🏠 Menú Principal")],
        )
    else:
        enviar_texto(
            ctx.tel,
            f"✅ ¡Listo, *{lead.nombre}*! Registramos tu solicitud (*#{lead.id}*).{dni_linea}\n\n"
            "Un asesor de nuestro equipo se comunicará contigo muy pronto por llamada o por este mismo WhatsApp. ¡Gracias!",
        )


def iniciar(ctx):
    if not en_horario_atencion():
        estado.iniciar(ctx.tel, "asesor", "franja", {"fuera_horario": True})
        enviar_botones(
            ctx.tel,
            f"🕒 *Atención fuera de horario*\n\n"
            f"Nuestro horario de asesores es de *{Config.ATENCION_HORA_INICIO:02d}:00 a {Config.ATENCION_HORA_FIN:02d}:00*.\n"
            f"Agendemos tu llamada o visita en 2 pasos rápidos. ¿En qué franja horaria te conviene?",
            [
                ("ase_franja:Mañana (9am - 12pm)", "🌅 Mañana (9-12)"),
                ("ase_franja:Tarde (2pm - 6pm)", "🌆 Tarde (2-6)"),
                ("ase_franja:Otro horario", "📅 Otro horario"),
            ],
        )
        return

    estado.iniciar(ctx.tel, "asesor", "sede", {"fuera_horario": False})
    enviar_botones(
        ctx.tel,
        "📅 *Atención con Asesor / Agendar Cita*\n\n"
        "Con gusto te ayudamos. Paso 1 de 4: ¿sobre qué institución deseas información?",
        [
            ("ase_sede:Colegio Comas", "🏫 Colegio Comas"),
            ("ase_sede:Colegio Ventanilla", "🏫 Col. Ventanilla"),
            ("ase_sede:Instituto Pedagógico", "🎓 Instituto"),
        ],
    )


def continuar(ctx, st):
    d = estado.datos(st)
    paso = st.paso

    if paso == "sede":
        if ctx.btn_id.startswith("ase_sede:"):
            sede = ctx.btn_id.split(":", 1)[1]
        elif ctx.tipo == "text" and ctx.txt.strip():
            sede = ctx.txt.strip()[:80]
        else:
            enviar_texto(ctx.tel, "Elige *Colegio Comas*, *Colegio Ventanilla* o *Instituto* (o escribe *cancelar*).")
            return True
        estado.avanzar(st, "tipo_cita", sede=sede)
        enviar_botones(
            ctx.tel,
            f"Perfecto (*{sede}*). Paso 2 de 4: ¿cómo prefieres que te atendamos?",
            [
                ("ase_tipo:Llamada telefónica", "📞 Que me llamen"),
                ("ase_tipo:Visita guiada", "🏫 Visita guiada"),
                ("ase_tipo:Chat ahora", "💬 Chat ahora"),
            ],
        )
        return True

    if paso == "tipo_cita":
        if ctx.btn_id.startswith("ase_tipo:"):
            tipo_cita = ctx.btn_id.split(":", 1)[1]
        elif ctx.tipo == "text" and ctx.txt.strip():
            tipo_cita = ctx.txt.strip()[:60]
        else:
            enviar_texto(ctx.tel, "Elige una modalidad: *Que me llamen*, *Visita guiada* o *Chat ahora*.")
            return True

        if tipo_cita == "Chat ahora":
            estado.avanzar(st, "datos", tipo_cita=tipo_cita, franja="")
            _pedir_identificacion(ctx, st, estado.datos(st), "¡Genial! Te conectaremos ahora mismo con un asesor.")
            return True

        estado.avanzar(st, "dia", tipo_cita=tipo_cita)
        enviar_botones(
            ctx.tel,
            f"Excelente (*{tipo_cita}*). Paso 3 de 4: ¿para qué día prefieres agendar?",
            [
                ("ase_dia:Hoy / Mañana", "📅 Hoy / Mañana"),
                ("ase_dia:Esta semana", "📆 Esta semana"),
                ("ase_dia:Sábado", "🗓️ Sábado"),
            ],
        )
        return True

    if paso == "dia":
        if ctx.btn_id.startswith("ase_dia:"):
            dia = ctx.btn_id.split(":", 1)[1]
        elif ctx.tipo == "text" and ctx.txt.strip():
            dia = ctx.txt.strip()[:60]
        else:
            enviar_texto(ctx.tel, "Indícanos qué día prefieres (ej. *Mañana*, *Jueves* o *Sábado*).")
            return True
        estado.avanzar(st, "franja", dia=dia)
        enviar_botones(
            ctx.tel,
            f"Anotado (*{dia}*). Paso 4 de 4: ¿en qué horario prefieres?",
            [
                ("ase_franja:Mañana (9am - 12pm)", "🌅 Mañana (9-12)"),
                ("ase_franja:Tarde (2pm - 6pm)", "🌆 Tarde (2-6)"),
                ("ase_franja:Otro horario", "🕒 Otro horario"),
            ],
        )
        return True

    if paso == "franja":
        if ctx.btn_id.startswith("ase_franja:"):
            franja = ctx.btn_id.split(":", 1)[1]
        elif ctx.tipo == "text" and ctx.txt.strip():
            franja = ctx.txt.strip()[:80]
        else:
            enviar_texto(ctx.tel, "Indícanos en qué horario prefieres que te llamemos (ej. *Mañana 10am* o *Tarde 3pm*).")
            return True
        estado.avanzar(st, "datos", franja=franja, sede=d.get("sede") or "Cita programada")
        _pedir_identificacion(ctx, st, estado.datos(st), f"¡Listo (*{franja}*)!")
        return True

    if paso == "confirmar_dni":
        resp_txt = (ctx.txt or "").strip().lower()
        if ctx.btn_id == "ase_dni_ok:si" or resp_txt in ("si", "sí", "confirmo", "ok"):
            _finalizar_registro(
                ctx, d,
                nombre_lead=d.get("nombre_completo") or ctx.nombre,
                detalle=d.get("detalle") or f"DNI verificado ({d.get('sede') or 'General'})",
                dni=d.get("dni") or "",
                nombres=d.get("nombres") or "",
                paternal=d.get("paternal") or "",
                maternal=d.get("maternal") or "",
            )
            return True
        if ctx.btn_id == "ase_dni_ok:no" or resp_txt in ("no", "cambiar", "corregir"):
            estado.avanzar(st, "datos", dni="", nombre_completo="")
            enviar_texto(ctx.tel, "Entendido. Escribe nuevamente tu número de *DNI* (8 dígitos) o tu *nombre completo*:")
            return True
        enviar_botones(
            ctx.tel,
            f"Por favor confirma si tus datos son correctos:\n*{d.get('nombre_completo', '')}*",
            [("ase_dni_ok:si", "✅ Sí, soy yo"), ("ase_dni_ok:no", "✏️ Corregir DNI")],
        )
        return True

    if paso == "datos":
        if ctx.tipo != "text" or not ctx.txt.strip():
            enviar_texto(ctx.tel, "Por favor escribe tu *DNI* (8 dígitos) o tu *nombre completo* (o escribe *cancelar*).")
            return True
        entrada = ctx.txt.strip()[:400]

        # Si el usuario ingresó un DNI de 8 dígitos -> validar contra API externa (/admin/endpoints)
        if entrada.isdigit() and len(entrada) == 8:
            info = consultar_dni_externo(entrada)
            if info.get("ok"):
                estado.avanzar(
                    st, "confirmar_dni",
                    dni=info["dni"],
                    nombres=info["nombres"],
                    paternal=info["paternal"],
                    maternal=info["maternal"],
                    nombre_completo=info["nombre_completo"],
                )
                enviar_botones(
                    ctx.tel,
                    f"👤 Encontré tus datos:\n"
                    f"*{info['nombre_completo']}*\n"
                    f"🪪 DNI: *****{info['dni'][-4:]}*\n\n"
                    "¿Confirmas que eres tú?",
                    [("ase_dni_ok:si", "✅ Sí, soy yo"), ("ase_dni_ok:no", "✏️ Corregir DNI")],
                    registro="[Confirmación de identidad por DNI enviada]",
                )
                return True
            enviar_texto(
                ctx.tel,
                "😕 No pude validar ese *DNI* en este momento. Verifica los 8 dígitos o escribe tu *nombre completo* para continuar sin demoras.",
            )
            return True

        # Respaldo manual: el usuario escribió su nombre completo (ej. CE o sin DNI)
        nombre_lead = entrada.splitlines()[0][:120]
        if len(nombre_lead) > 60:
            nombre_lead = ctx.nombre or nombre_lead[:60]
        _finalizar_registro(ctx, d, nombre_lead=nombre_lead, detalle=entrada)
        return True

    estado.terminar(ctx.tel)
    return False
