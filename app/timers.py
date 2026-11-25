"""
Motor de temporizadores automáticos para el CRM:
  1. SLA de derivación a asesor (HANDOFF_SLA_SECONDS): si ningún asesor responde a tiempo,
     EVO retoma el control amablemente y solicita horario preferido de llamada (cita).
  2. Inactividad del usuario (USER_INACTIVITY_WARNING_MIN + USER_INACTIVITY_CLOSE_MIN):
     - Tras N minutos sin respuesta al asesor: envía aviso de espera de 2 minutos.
     - Tras +M minutos adicionales: envía cierre formal con {{usuario}} y {{url_empresa}},
       reactiva el bot y registra la resolución del Lead.
"""
import logging
from datetime import timedelta

from config import Config
from .flujos import estado as estado_flujo
from .models import Empresa, Lead, PlantillaRapida, Usuario, db, utcnow
from .services import enviar_botones, enviar_texto

logger = logging.getLogger(__name__)

DEFAULT_AVISO_ESPERA = (
    "Tal vez en estos momentos no puedas responder, pero no te preocupes, "
    "estaré esperándote 2 minutos más para poderte ayudar."
)
DEFAULT_CIERRE = (
    "Al no haber alguna respuesta, concluiremos con la conversación.\n"
    "¡Gracias por escribirnos, {{usuario}}!\n"
    "Puedes platicar con nosotros cuando lo necesites.\n"
    "También te invitamos a {{url_empresa}} para acceder a tus beneficios."
)


def interpolar_plantilla(texto, usuario_nombre="", asesor_nombre="", empresa=None):
    """Reemplaza {{usuario}}, {{asesor}}, {{url_empresa}}, {{empresa}} y {{BOT_NAME}}."""
    url_emp = (empresa.dominio_web if empresa and empresa.dominio_web else "") or "nuestro portal web"
    nom_emp = (empresa.nombre_comercial if empresa and empresa.nombre_comercial else "") or Config.COMPANY_NAME
    reemplazos = {
        "{{usuario}}": usuario_nombre or "estimado(a)",
        "{{asesor}}": asesor_nombre or "un asesor",
        "{{url_empresa}}": url_emp,
        "{{empresa}}": nom_emp,
        "{{BOT_NAME}}": Config.BOT_NAME,
    }
    out = texto or ""
    for k, v in reemplazos.items():
        out = out.replace(k, str(v))
    return out


def _texto_plantilla(atajo, defecto):
    p = PlantillaRapida.query.filter_by(atajo=atajo, activo=True).first()
    return p.contenido if p and p.contenido else defecto


def ejecutar_ciclo_timers(ahora=None):
    """Ejecuta una pasada de SLA de asesores + inactividad de usuarios. Devuelve conteos."""
    ahora = ahora or utcnow()
    stats = {"sla_retomados": 0, "avisos_inactividad": 0, "cierres_inactividad": 0}

    # 1. SLA de asesor: chats en cola que superaron HANDOFF_SLA_SECONDS sin respuesta humana
    limite_sla = ahora - timedelta(seconds=Config.HANDOFF_SLA_SECONDS)
    en_cola = Usuario.query.filter(
        Usuario.bot_pausado.is_(True),
        Usuario.estado_chat == 'en_cola',
        Usuario.esperando_asesor_desde.isnot(None),
        Usuario.esperando_asesor_desde <= limite_sla,
    ).all()
    for usu in en_cola:
        usu.bot_pausado = False
        usu.estado_chat = 'bot'
        usu.esperando_asesor_desde = None
        db.session.commit()
        estado_flujo.iniciar(usu.telefono, "asesor", "franja", {"fuera_horario": True})
        enviar_botones(
            usu.telefono,
            "⏳ Nuestros asesores están atendiendo otras consultas en este momento.\n\n"
            "Para no hacerte esperar, indícanos en qué *horario podemos llamarte* o déjanos tu consulta aquí:",
            [
                ("ase_franja:Mañana (9am - 12pm)", "🌅 Mañana (9-12)"),
                ("ase_franja:Tarde (2pm - 6pm)", "🌆 Tarde (2-6)"),
                ("ase_franja:Otro horario", "📅 Otro horario"),
            ],
        )
        stats["sla_retomados"] += 1

    # 2. Aviso de inactividad del usuario (cuando el asesor espera respuesta)
    limite_aviso = ahora - timedelta(minutes=Config.USER_INACTIVITY_WARNING_MIN)
    para_avisar = Usuario.query.filter(
        Usuario.bot_pausado.is_(True),
        Usuario.estado_chat == 'atendido',
        Usuario.aviso_inactividad_enviado.is_(False),
        Usuario.esperando_usuario_desde.isnot(None),
        Usuario.esperando_usuario_desde <= limite_aviso,
    ).all()
    tpl_espera = _texto_plantilla('/espera', DEFAULT_AVISO_ESPERA)
    for usu in para_avisar:
        emp = db.session.get(Empresa, usu.empresa_id) if usu.empresa_id else None
        msg = interpolar_plantilla(tpl_espera, usuario_nombre=usu.nombre_wa, empresa=emp)
        enviar_texto(usu.telefono, msg)
        usu.aviso_inactividad_enviado = True
        usu.esperando_usuario_desde = ahora
        db.session.commit()
        stats["avisos_inactividad"] += 1

    # 3. Cierre formal por inactividad (+USER_INACTIVITY_CLOSE_MIN después del aviso)
    limite_cierre = ahora - timedelta(minutes=Config.USER_INACTIVITY_CLOSE_MIN)
    para_cerrar = Usuario.query.filter(
        Usuario.bot_pausado.is_(True),
        Usuario.estado_chat == 'atendido',
        Usuario.aviso_inactividad_enviado.is_(True),
        Usuario.esperando_usuario_desde.isnot(None),
        Usuario.esperando_usuario_desde <= limite_cierre,
    ).all()
    tpl_cierre = _texto_plantilla('/cierre', DEFAULT_CIERRE)
    for usu in para_cerrar:
        emp = db.session.get(Empresa, usu.empresa_id) if usu.empresa_id else None
        msg = interpolar_plantilla(tpl_cierre, usuario_nombre=usu.nombre_wa, empresa=emp)
        enviar_texto(usu.telefono, msg)
        usu.bot_pausado = False
        usu.estado_chat = 'bot'
        usu.esperando_usuario_desde = None
        usu.aviso_inactividad_enviado = False
        lead = Lead.query.filter_by(telefono=usu.telefono).order_by(Lead.id.desc()).first()
        if lead and not lead.resuelto_en:
            lead.resuelto_en = ahora
        db.session.commit()
        stats["cierres_inactividad"] += 1

    return stats


def run_worker():
    """Loop dedicado del servicio `worker` (docker-compose.yml) cada 30s con advisory lock en PostgreSQL."""
    import os
    import time
    from sqlalchemy import text
    from . import create_app

    intervalo = max(5, int(os.environ.get("WORKER_INTERVAL_SECONDS", "30") or "30"))
    lock_key = 774202601
    app = create_app()
    logger.info("Worker de timers EVO iniciado (intervalo=%ss).", intervalo)
    while True:
        with app.app_context():
            got_lock = True
            if db.engine.dialect.name == "postgresql":
                got_lock = bool(db.session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key}).scalar())
            if got_lock:
                try:
                    stats = ejecutar_ciclo_timers()
                    if any(stats.values()):
                        logger.info("Ciclo timers: %s", stats)
                except Exception:
                    logger.exception("Error ejecutando ciclo de timers")
                    db.session.rollback()
                finally:
                    if db.engine.dialect.name == "postgresql":
                        try:
                            db.session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key})
                        except Exception:
                            pass
            db.session.remove()
        time.sleep(intervalo)


if __name__ == "__main__":
    run_worker()

