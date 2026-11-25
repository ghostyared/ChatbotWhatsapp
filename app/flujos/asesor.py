"""
Flujo guiado de derivación a Call Center / Asesor humano:
  1. Pregunta qué institución/servicio busca y su nombre (o si prefiere otro número/horario para llamarlo)
  2. Crea/actualiza un Lead con prioridad ALTA en el CRM (/admin/leads)
  3. Notifica al asesor (WhatsApp y/o webhook 'notificar_callcenter' si está activo)
  4. Pausa el bot para que el agente de Call Center atienda el chat o lo llame.
"""
import logging

from config import Config
from .. import endpoints_client as ep_cli
from ..models import Lead, db
from ..services import enviar_botones, enviar_texto
from . import estado

logger = logging.getLogger(__name__)


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
    estado.iniciar(ctx.tel, "asesor", "sede", {})
    enviar_botones(
        ctx.tel,
        "📞 *Atención con un asesor*\n\nCon gusto te comunicamos con nuestro equipo para llamarte o atenderte por este chat.\n"
        "¿Sobre cuál institución deseas información?",
        [("ase_sede:Colegio", "🏫 Colegio"), ("ase_sede:Instituto Pedagógico", "🎓 Instituto"), ("ase_sede:Alumno / Pagos", "💳 Soy alumno")],
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

        lead = Lead.query.filter_by(telefono=ctx.tel).filter(
            Lead.estado.in_(("nuevo", "contactado", "visita"))
        ).first() or Lead(telefono=ctx.tel)
        lead.nombre = nombre_lead if len(nombre_lead) <= 60 else (ctx.nombre or nombre_lead[:60])
        lead.sede_nombre = sede[:120]
        lead.comentario = detalle
        lead.resumen_ia = f"Solicitó asesor/llamada para {sede}: {detalle}"
        lead.quiere_asesor = True
        lead.prioridad = "alta"
        lead.resultado_llamada = "pendiente"
        if not lead.estado:
            lead.estado = "nuevo"
        db.session.add(lead)
        db.session.commit()

        lead.asesor_avisado = notificar_callcenter(lead)
        ctx.usu.bot_pausado = True
        db.session.commit()
        estado.terminar(ctx.tel)

        enviar_texto(
            ctx.tel,
            f"✅ ¡Listo! Registramos tu solicitud (*#{lead.id}*).\n\n"
            "Un asesor de nuestro equipo se comunicará contigo muy pronto por llamada o por este mismo WhatsApp. ¡Gracias!",
        )
        return True

    estado.terminar(ctx.tel)
    return False
