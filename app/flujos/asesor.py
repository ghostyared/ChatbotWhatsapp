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


def iniciar(ctx):
    if not en_horario_atencion():
        estado.iniciar(ctx.tel, "asesor", "franja", {"fuera_horario": True})
        enviar_botones(
            ctx.tel,
            f"🕒 *Atención fuera de horario*\n\n"
            f"Nuestro horario de asesores es de *{Config.ATENCION_HORA_INICIO:02d}:00 a {Config.ATENCION_HORA_FIN:02d}:00*.\n"
            f"Déjanos agendada tu llamada y te contactamos en el horario que prefieras. ¿En qué franja te llamamos?",
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
        "📞 *Atención con un asesor*\n\nCon gusto te comunicamos con nuestro equipo para llamarte o atenderte por este chat.\n"
        "¿Sobre cuál institución deseas información?",
        [("ase_sede:Colegio", "🏫 Colegio"), ("ase_sede:Instituto Pedagógico", "🎓 Instituto"), ("ase_sede:Alumno / Pagos", "💳 Soy alumno")],
    )


def continuar(ctx, st):
    d = estado.datos(st)
    paso = st.paso

    if paso == "franja":
        if ctx.btn_id.startswith("ase_franja:"):
            franja = ctx.btn_id.split(":", 1)[1]
        elif ctx.tipo == "text" and ctx.txt.strip():
            franja = ctx.txt.strip()[:80]
        else:
            enviar_texto(ctx.tel, "Indícanos en qué horario prefieres que te llamemos (ej. *Mañana 10am* o *Tarde 3pm*).")
            return True
        estado.avanzar(st, "datos", franja=franja, sede=d.get("sede") or "Cita programada")
        enviar_texto(
            ctx.tel,
            f"Anotado (*{franja}*). Escribe tu *nombre completo*, la *institución/carrera de interés* y el motivo de tu consulta para agendar tu llamada.",
        )
        return True

    if paso == "sede":
        if ctx.btn_id.startswith("ase_sede:"):
            sede = ctx.btn_id.split(":", 1)[1]
        elif ctx.tipo == "text" and ctx.txt.strip():
            sede = ctx.txt.strip()[:80]
        else:
            enviar_texto(ctx.tel, "Indícame si tu consulta es sobre *Colegio*, *Instituto* o *Soy alumno* (o escribe *cancelar*).")
            return True
        estado.avanzar(st, "datos", sede=sede)
        enviar_texto(
            ctx.tel,
            f"Perfecto (*{sede}*). Escribe tu *nombre completo* y, si deseas que te llamemos, indícanos el *horario preferido* (o si prefieres otro número de celular).",
        )
        return True

    if paso == "datos":
        if ctx.tipo != "text" or not ctx.txt.strip():
            enviar_texto(ctx.tel, "Por favor escribe tu nombre y un breve detalle para pasarte con el asesor (o escribe *cancelar*).")
            return True
        detalle = ctx.txt.strip()[:400]
        nombre_lead = detalle.splitlines()[0][:120]
        sede = d.get("sede") or "General"
        franja = d.get("franja") or ""
        fuera_horario = bool(d.get("fuera_horario"))

        lead = Lead.query.filter_by(telefono=ctx.tel).filter(
            Lead.estado.in_(("nuevo", "contactado", "visita"))
        ).first() or Lead(telefono=ctx.tel)
        lead.nombre = nombre_lead if len(nombre_lead) <= 60 else (ctx.nombre or nombre_lead[:60])
        lead.sede_nombre = sede[:120]
        lead.comentario = detalle
        if franja:
            lead.visita_pref = franja[:120]
        lead.resumen_ia = (
            f"Cita/llamada programada ({franja}) para {sede}: {detalle}"
            if franja else f"Solicitó asesor/llamada para {sede}: {detalle}"
        )
        lead.quiere_asesor = True
        lead.prioridad = "alta"
        lead.score = "caliente"
        lead.empresa_id = lead.empresa_id or ctx.usu.empresa_id
        if not lead.asignado_a:
            auto_asesor = asignar_asesor_round_robin(lead.empresa_id)
            if auto_asesor:
                lead.asignado_a = auto_asesor
        lead.resultado_llamada = "cita_agendada" if franja else "pendiente"
        lead.estado = "visita" if franja else (lead.estado or "nuevo")
        db.session.add(lead)
        db.session.commit()
        if lead.asignado_a:
            ctx.usu.asignado_a = lead.asignado_a
            db.session.add(LeadEvento(lead_id=lead.id, tipo='asignacion', detalle=f'Auto-asignado (Round-Robin) a {lead.asignado_a}', usuario='sistema'))
            db.session.commit()

        if franja:
            cita = Cita(
                lead_id=lead.id,
                empresa_id=lead.empresa_id,
                telefono=ctx.tel,
                nombre=lead.nombre or ctx.nombre or "",
                fecha_preferida="Próximo día hábil",
                franja=franja[:80],
                motivo=detalle[:500],
                asignado_a=lead.asignado_a,
                estado="pendiente",
            )
            db.session.add(cita)
            db.session.commit()
            ep_cita = ep_cli.obtener_endpoint("agendar_cita", solo_activo=True)
            if ep_cita:
                ep_cli.ejecutar(ep_cita, {
                    "lead_id": lead.id,
                    "empresa_id": lead.empresa_id or 0,
                    "franja": franja,
                    "motivo": detalle[:300],
                })

        lead.asesor_avisado = notificar_callcenter(lead)
        if not fuera_horario:
            ctx.usu.bot_pausado = True
            ctx.usu.estado_chat = "en_cola"
            ctx.usu.esperando_asesor_desde = utcnow()
        db.session.commit()
        estado.terminar(ctx.tel)

        if franja:
            enviar_botones(
                ctx.tel,
                f"📅 ¡Listo! Agendamos tu llamada (*#{lead.id}*) para *{franja}*.\n\n"
                f"Un asesor revisará tu consulta y te contactará en ese horario. ¡Gracias!",
                [("CMD_MAIN_MENU", "🏠 Menú Principal")],
            )
        else:
            enviar_texto(
                ctx.tel,
                f"✅ ¡Listo! Registramos tu solicitud (*#{lead.id}*).\n\n"
                "Un asesor de nuestro equipo se comunicará contigo muy pronto por llamada o por este mismo WhatsApp. ¡Gracias!",
            )
        return True

    estado.terminar(ctx.tel)
    return False
