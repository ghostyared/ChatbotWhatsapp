#!/usr/bin/env python3
"""
Utilidades de administración.

  python manage.py init-db             Crea tablas y datos iniciales (admin + flujo de ejemplo). Idempotente.
  python manage.py reset-admin         Restablece la contraseña del admin (ADMIN_USER) con el ADMIN_PASS del .env.
  python manage.py reset-menu          BORRA los textos/botones del bot y vuelve a cargar el menú de ejemplo.
  python manage.py seed-demo-empresas  Crea 3 empresas ficticias de prueba en /admin/ia (idempotente).
  python manage.py seed-demo-crm       Simula datos completos en TODO el CRM (empresas, equipo, leads, citas, chats, brechas IA, campañas y materiales).
"""
import sys
import time
import logging

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from app import create_app
from app.models import (db, ApiEndpoint, Cita, Empresa, Lead, LeadEvento, MaterialComercial,
                        Mensaje, PlantillaRapida, PreguntaPendiente, ReglaCampana, RespuestaBot,
                        Usuario, UsuarioSistema, utcnow)
from config import Config

log = logging.getLogger("manage")


def wait_for_db(retries=30, delay=2):
    for intento in range(1, retries + 1):
        try:
            db.session.execute(text("SELECT 1"))
            return
        except OperationalError as e:
            db.session.rollback()
            log.warning("Esperando a la base de datos (%s/%s): %s", intento, retries, str(e).splitlines()[0])
            time.sleep(delay)
    raise SystemExit("No se pudo conectar a la base de datos.")


# Columnas añadidas en versiones posteriores: se agregan solas a bases ya existentes.
COLUMNAS_NUEVAS = [
    ("usuarios", "intentos_acad", "INTEGER NOT NULL DEFAULT 0"),
    ("usuarios", "bloqueado_acad_hasta", "TIMESTAMP"),
    ("usuarios", "empresa_id", "INTEGER"),
    ("usuarios", "saludo_enviado", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("usuarios", "estado_chat", "VARCHAR(20) NOT NULL DEFAULT 'bot'"),
    ("usuarios", "asignado_a", "VARCHAR(60)"),
    ("usuarios", "esperando_asesor_desde", "TIMESTAMP"),
    ("usuarios", "esperando_usuario_desde", "TIMESTAMP"),
    ("usuarios", "aviso_inactividad_enviado", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("usuarios", "opt_out", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("usuarios", "ultimo_msg_entrada", "TIMESTAMP"),
    ("mensajes", "es_interno", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("mensajes", "estado_envio", "VARCHAR(20)"),
    ("mensajes", "error_meta", "VARCHAR(300)"),
    ("mensajes", "media_tipo", "VARCHAR(20)"),
    ("mensajes", "media_url", "VARCHAR(500)"),
    ("comprobantes", "contenido", "BLOB"),     # se traduce a BYTEA en PostgreSQL
    ("leads", "asignado_a", "VARCHAR(60)"),
    ("leads", "prioridad", "VARCHAR(15) DEFAULT 'media'"),
    ("leads", "telefono_contacto", "VARCHAR(30)"),
    ("leads", "resultado_llamada", "VARCHAR(30) DEFAULT 'pendiente'"),
    ("leads", "resumen_ia", "TEXT"),
    ("leads", "empresa_id", "INTEGER"),
    ("leads", "origen", "VARCHAR(30) DEFAULT 'organico'"),
    ("leads", "campana_id", "VARCHAR(100)"),
    ("leads", "campana_nombre", "VARCHAR(150)"),
    ("leads", "ctwa_clid", "VARCHAR(200)"),
    ("leads", "score", "VARCHAR(15) DEFAULT 'frio'"),
    ("leads", "tags", "VARCHAR(255) DEFAULT ''"),
    ("leads", "motivo_descarte", "VARCHAR(60) DEFAULT ''"),
    ("leads", "seguimiento_20h_enviado", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("leads", "csat", "INTEGER"),
    ("leads", "primera_respuesta_en", "TIMESTAMP"),
    ("leads", "resuelto_en", "TIMESTAMP"),
    ("respuestas_bot", "tipo_nodo", "VARCHAR(20) NOT NULL DEFAULT 'botones'"),
    ("respuestas_bot", "empresa_id", "INTEGER"),
    ("respuestas_bot", "nodo_padre", "VARCHAR(50)"),
    ("respuestas_bot", "texto_boton_lista", "VARCHAR(30) DEFAULT 'Ver opciones'"),
    ("respuestas_bot", "documento_url", "VARCHAR(500)"),
    ("respuestas_bot", "documento_nombre", "VARCHAR(120)"),
    ("usuarios_sistema", "en_linea", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("usuarios_sistema", "empresa_id", "INTEGER"),
    ("usuarios_sistema", "max_chats", "INTEGER NOT NULL DEFAULT 10"),
]


def migrar_columnas():
    insp = inspect(db.engine)
    for tabla, columna, ddl in COLUMNAS_NUEVAS:
        if tabla in insp.get_table_names() and columna not in {c["name"] for c in insp.get_columns(tabla)}:
            if ddl == 'BLOB' and db.engine.dialect.name == 'postgresql':
                ddl = 'BYTEA'
            db.session.execute(text(f'ALTER TABLE {tabla} ADD COLUMN {columna} {ddl}'))
            db.session.commit()
            log.info("Columna agregada: %s.%s", tabla, columna)


def migrar_indices():
    """Crea índices compuestos recomendados para escalar /monitor, /leads y /citas (idempotente)."""
    indices = [
        "CREATE INDEX IF NOT EXISTS ix_mensajes_tel_fecha ON mensajes (telefono, fecha)",
        "CREATE INDEX IF NOT EXISTS ix_leads_estado_creado ON leads (estado, creado_en)",
        "CREATE INDEX IF NOT EXISTS ix_citas_estado_creado ON citas (estado, creado_en)",
    ]
    for ddl in indices:
        try:
            db.session.execute(text(ddl))
            db.session.commit()
        except Exception:
            db.session.rollback()


def init_db():
    wait_for_db()
    db.create_all()
    migrar_columnas()
    migrar_indices()
    Empresa.seed_from_ajuste()
    PlantillaRapida.seed()
    RespuestaBot.seed()
    ApiEndpoint.seed()
    creado = UsuarioSistema.seed_admin()
    log.info("Base de datos lista. Admin %s.", "creado" if creado else "ya existía")


def reset_admin():
    wait_for_db()
    user = UsuarioSistema.query.filter_by(username=Config.ADMIN_USER.strip().lower()).first()
    if not user:
        UsuarioSistema.seed_admin()
        log.info("Admin creado.")
        return
    user.set_password(Config.ADMIN_PASS)
    user.activo = True
    user.rol = 'admin'
    user.intentos_fallidos = 0
    user.bloqueado_hasta = None
    db.session.commit()
    log.info("Contraseña del admin '%s' restablecida desde .env.", user.username)


def reset_menu():
    wait_for_db()
    borrados = RespuestaBot.query.delete()
    db.session.commit()
    RespuestaBot.seed()
    log.info("Menú reiniciado (%s nodos anteriores eliminados).", borrados)


def seed_demo_empresas():
    """Carga 3 empresas ficticias de prueba en /admin/ia sin sobrescribir las que ya existan."""
    wait_for_db()
    demos = [
        dict(
            clave="uwiener",
            nombre_comercial="Universidad Norbert Wiener",
            dominio_web="https://www.uwiener.edu.pe",
            horario_atencion="Lunes a viernes 08:00–19:00 · Sábados 09:00–13:00",
            cola_asesores="universidad",
            orden=1,
            prompt_sistema=(
                "Atiendes consultas de admisión universitaria (pregrado y posgrado). "
                "Enfócate en empleabilidad, laboratorios de simulación clínica y convenios internacionales."
            ),
            base_conocimiento=(
                "INSTITUCIÓN: Universidad Norbert Wiener (datos ficticios de QA)\n"
                "SEDE PRINCIPAL: Av. Arequipa 440, Cercado de Lima.\n"
                "CARRERAS Y PENSIONES REFERENCIALES (5 cuotas por ciclo):\n"
                "- Medicina Humana: matrícula S/ 450, pensión mensual S/ 2,400.\n"
                "- Enfermería: matrícula S/ 350, pensión mensual S/ 1,150.\n"
                "- Derecho: matrícula S/ 350, pensión mensual S/ 980.\n"
                "- Ingeniería de Sistemas e Informática: matrícula S/ 350, pensión mensual S/ 1,050.\n"
                "MODALIDADES: Presencial y Semipresencial.\n"
                "INICIO DE CLASES: Marzo y Agosto (2 procesos de admisión al año).\n"
                "REQUISITOS DE ADMISIÓN: DNI vigente, certificado de estudios secundarios completos y ficha de inscripción.\n"
                "DIFERENCIALES: Laboratorios de simulación clínica, biblioteca digital 24/7 y bolsa de trabajo."
            ),
        ),
        dict(
            clave="carrion",
            nombre_comercial="Instituto Carrión",
            dominio_web="https://www.acarrion.edu.pe",
            horario_atencion="Lunes a viernes 08:00–18:30 · Sábados 09:00–13:00",
            cola_asesores="instituto",
            orden=2,
            prompt_sistema=(
                "Atiendes postulantes de carreras técnicas profesionales de salud (3 años). "
                "Destaca el título a nombre de la Nación y las prácticas desde los primeros ciclos."
            ),
            base_conocimiento=(
                "INSTITUCIÓN: Instituto de Educación Superior Daniel Alcides Carrión (datos ficticios de QA)\n"
                "SEDES: Av. Arequipa (Lima Centro), Sede Los Olivos y Sede San Juan de Lurigancho.\n"
                "CARRERAS TÉCNICAS (3 años — 6 ciclos):\n"
                "- Enfermería Técnica: matrícula S/ 220, pensión mensual S/ 490.\n"
                "- Farmacia Técnica: matrícula S/ 220, pensión mensual S/ 470.\n"
                "- Fisioterapia y Rehabilitación: matrícula S/ 220, pensión mensual S/ 510.\n"
                "- Laboratorio Clínico y Anatomía Patológica: matrícula S/ 220, pensión mensual S/ 495.\n"
                "TURNOS: Mañana (08:00–12:30), Tarde (14:00–18:30) y Noche (19:00–22:30).\n"
                "REQUISITOS: DNI vigente y certificado de secundaria completa.\n"
                "BENEFICIOS: Convenios con clínicas y hospitales para prácticas preprofesionales."
            ),
        ),
        dict(
            clave="mariategui",
            nombre_comercial="Colegio Mariátegui",
            dominio_web="https://www.mariategui.edu.pe",
            horario_atencion="Lunes a viernes 08:00–17:00",
            cola_asesores="colegio",
            orden=3,
            prompt_sistema=(
                "Atiendes a padres de familia interesados en educación escolar (Inicial, Primaria y Secundaria). "
                "Sé muy cálido y ofrece siempre agendar una visita guiada."
            ),
            base_conocimiento=(
                "INSTITUCIÓN: Colegio Mariátegui (datos ficticios de QA)\n"
                "SEDES:\n"
                "- Sede Norte (Los Olivos): Inicial, Primaria y Secundaria.\n"
                "- Sede Sur (Surco): Inicial, Primaria y Secundaria.\n"
                "INVERSION REFERENCIAL:\n"
                "- Inicial (3, 4 y 5 años): matrícula S/ 420, pensión mensual S/ 420.\n"
                "- Primaria (1° a 6° grado): matrícula S/ 480, pensión mensual S/ 480.\n"
                "- Secundaria (1° a 5° año): matrícula S/ 520, pensión mensual S/ 520.\n"
                "HORARIO ESCOLAR: 07:45 a 14:30.\n"
                "TALLERES INCLUIDOS: Robótica educativa, inglés intensivo, ajedrez y música.\n"
                "REQUISITOS: DNI del estudiante y apoderado, libreta de notas del año anterior y constancia de matrícula SIAGIE."
            ),
        ),
    ]
    creadas = 0
    for item in demos:
        if not Empresa.query.filter_by(clave=item["clave"]).first():
            db.session.add(Empresa(**item, activo=True, actualizado_por="seed_demo"))
            creadas += 1
    db.session.commit()
    log.info("Empresas demo listas (%s creadas, %s total en BD).", creadas, Empresa.query.count())


def seed_demo_crm():
    """Simula datos realistas en TODOS los módulos del CRM (idempotente):
    empresas (Comas, Ventanilla, Instituto), equipo/roles, leads en todos los estados,
    citas, chats con checks y susurro, brechas IA, campañas Ads y biblioteca multimedia."""
    from datetime import timedelta
    wait_for_db()
    PlantillaRapida.seed()
    RespuestaBot.seed()

    # 1. Empresas principales alineadas con el negocio real (Comas, Ventanilla, Instituto)
    empresas_demo = [
        dict(
            clave="colegio_comas",
            nombre_comercial="Colegio Sede Comas",
            dominio_web="https://comas.mariategui.edu.pe",
            horario_atencion="Lunes a viernes 08:00–17:30 · Sábados 09:00–13:00",
            cola_asesores="comas",
            orden=1,
            prompt_sistema="Atiendes familias interesadas en Colegio Sede Comas (Inicial, Primaria, Secundaria). Destaca talleres y vacantes limitadas.",
            base_conocimiento=(
                "COLEGIO SEDE COMAS (Av. Universitaria Norte 1450, Comas)\n"
                "- Inicial (3, 4 y 5 años): matrícula S/ 420, pensión mensual S/ 420 (10 cuotas).\n"
                "- Primaria (1° a 6° grado): matrícula S/ 480, pensión mensual S/ 480 (10 cuotas).\n"
                "- Secundaria (1° a 5° año): matrícula S/ 530, pensión mensual S/ 530 (10 cuotas).\n"
                "TALLERES: Inglés intensivo, Robótica, Ajedrez y Deportes. Vacantes limitadas 2026."
            ),
        ),
        dict(
            clave="colegio_ventanilla",
            nombre_comercial="Colegio Sede Ventanilla",
            dominio_web="https://ventanilla.mariategui.edu.pe",
            horario_atencion="Lunes a viernes 08:00–17:30 · Sábados 09:00–13:00",
            cola_asesores="ventanilla",
            orden=2,
            prompt_sistema="Atiendes familias interesadas en Colegio Sede Ventanilla (Inicial, Primaria, Secundaria).",
            base_conocimiento=(
                "COLEGIO SEDE VENTANILLA (Av. Néstor Gambetta Km 14.5, Ventanilla)\n"
                "- Inicial (3, 4 y 5 años): matrícula S/ 380, pensión mensual S/ 380 (10 cuotas).\n"
                "- Primaria (1° a 6° grado): matrícula S/ 430, pensión mensual S/ 430 (10 cuotas).\n"
                "- Secundaria (1° a 5° año): matrícula S/ 470, pensión mensual S/ 470 (10 cuotas).\n"
                "TALLERES: Robótica, Inglés, Danza y Pre-U. Vacantes limitadas 2026."
            ),
        ),
        dict(
            clave="instituto_pedagogico",
            nombre_comercial="Instituto Pedagógico",
            dominio_web="https://instituto.mariategui.edu.pe",
            horario_atencion="Lunes a viernes 08:00–19:00 · Sábados 09:00–13:00",
            cola_asesores="instituto",
            orden=3,
            prompt_sistema="Atiendes postulantes del Instituto Pedagógico (3 carreras, inicio cada semestre, vacantes limitadas).",
            base_conocimiento=(
                "INSTITUTO PEDAGÓGICO (Inicio cada semestre: Marzo y Agosto · Vacantes limitadas)\n"
                "- Educación Inicial (5 años / 10 ciclos): matrícula S/ 250, pensión S/ 460 (5 cuotas).\n"
                "- Educación Primaria (5 años / 10 ciclos): matrícula S/ 250, pensión S/ 460 (5 cuotas).\n"
                "- Educación Física (5 años / 10 ciclos): matrícula S/ 250, pensión S/ 490 (5 cuotas).\n"
                "REQUISITOS: DNI vigente y certificado de secundaria completa."
            ),
        ),
    ]
    emp_objs = {}
    for item in empresas_demo:
        e = Empresa.query.filter_by(clave=item["clave"]).first()
        if not e:
            e = Empresa(**item, activo=True, actualizado_por="seed_demo_crm")
            db.session.add(e)
            db.session.commit()
        emp_objs[item["clave"]] = e

    # 2. Equipo y Roles demo (contraseña QA: DemoCRM2026!)
    equipo_demo = [
        ("supervisor_demo", "Laura Gómez (Supervisora)", "supervisor", None),
        ("asesor_comas", "Piero Cruz (Comas)", "callcenter", emp_objs["colegio_comas"].id),
        ("asesor_ventanilla", "Valeria Ruiz (Ventanilla)", "callcenter", emp_objs["colegio_ventanilla"].id),
        ("asesor_instituto", "Diego Torres (Instituto)", "callcenter", emp_objs["instituto_pedagogico"].id),
        ("contabilidad_demo", "Rosa Mendoza (Caja)", "contabilidad", None),
    ]
    for uname, nom, rol, eid in equipo_demo:
        if not UsuarioSistema.query.filter_by(username=uname).first():
            u = UsuarioSistema(username=uname, nombre_completo=nom, rol=rol, empresa_id=eid, en_linea=True, max_chats=10, activo=True)
            u.set_password("DemoCRM2026!")
            db.session.add(u)
    db.session.commit()

    # 3. Campañas Facebook/IG Ads + Materiales Comerciales
    campanas_demo = [
        ("Campaña Educ. Inicial Marzo", "AD_INST_001", "educacion inicial,instituto", emp_objs["instituto_pedagogico"].id,
         "Educación Inicial", "https://instituto.mariategui.edu.pe/brochure-inicial.pdf"),
        ("Campaña Colegio Comas 2026", "AD_COMAS_002", "colegio comas,vacantes", emp_objs["colegio_comas"].id,
         "Primaria / Secundaria", "https://comas.mariategui.edu.pe/brochure-2026.pdf"),
    ]
    for nom, sid, kw, eid, prog, burl in campanas_demo:
        if not ReglaCampana.query.filter_by(source_id=sid).first():
            db.session.add(ReglaCampana(
                nombre=nom, source_id=sid, palabras_clave=kw, empresa_id=eid,
                programa=prog, brochure_url=burl, brochure_nombre="Brochure_2026.pdf", activo=True,
            ))

    materiales_demo = [
        (emp_objs["colegio_comas"].id, "Flyer Vacantes Comas 2026", "image", "https://comas.mariategui.edu.pe/flyer-comas.jpg", "Vacantes limitadas en Sede Comas"),
        (emp_objs["colegio_ventanilla"].id, "Video Recorrido Campus Ventanilla", "video", "https://ventanilla.mariategui.edu.pe/tour-campus.mp4", "Conoce nuestro campus en Ventanilla"),
        (emp_objs["instituto_pedagogico"].id, "Brochure 3 Carreras Pedagógicas", "document", "https://instituto.mariategui.edu.pe/brochure-carreras.pdf", "Malla curricular e inversión semestral"),
    ]
    for eid, tit, tipo, url, cap in materiales_demo:
        if not MaterialComercial.query.filter_by(titulo=tit).first():
            db.session.add(MaterialComercial(empresa_id=eid, titulo=tit, tipo=tipo, url=url, caption=cap, activo=True))
    db.session.commit()

    # 4. Brechas de conocimiento IA (Knowledge Gaps listas para aprobar en /admin/ia)
    gaps_demo = [
        (emp_objs["colegio_comas"].id, "¿Tienen piscina temperada en la sede Comas?", "51990001001", 5),
        (emp_objs["colegio_ventanilla"].id, "¿Cuentan con servicio de movilidad escolar en Ventanilla?", "51990001002", 4),
        (emp_objs["instituto_pedagogico"].id, "¿Aceptan traslado externo desde provincia para Educación Física?", "51990001003", 3),
    ]
    for eid, preg, tel, veces in gaps_demo:
        reg = PreguntaPendiente.registrar(preg, empresa_id=eid, telefono=tel)
        if reg and reg.veces < veces:
            reg.veces = veces
    db.session.commit()

    # 5. Usuarios, Conversaciones (con checks y susurro), Leads en todos los estados y Citas
    ahora = utcnow()
    prospectos = [
        ("51990001001", "Carla Quispe", emp_objs["colegio_comas"], "Primaria 4to", "facebook_ads", "Campaña Colegio Comas 2026", "caliente", "nuevo", "alta", "asesor_comas", "en_cola", True, "Beca,Turno Mañana", None, 45, None),
        ("51990001002", "Jorge Paredes", emp_objs["colegio_ventanilla"], "Inicial 4 años", "instagram_ads", "Ads Ventanilla Inicial", "tibio", "contactado", "media", "asesor_ventanilla", "atendido", True, "Movilidad", None, 90, None),
        ("51990001003", "Milagros Silva", emp_objs["instituto_pedagogico"], "Educación Física", "facebook_ads", "Campaña Educ. Inicial Marzo", "caliente", "visita", "alta", "asesor_instituto", "bot", False, "Semestre Marzo", 5, 60, 420),
        ("51990001004", "Renato Chávez", emp_objs["instituto_pedagogico"], "Educación Inicial", "organico", "", "caliente", "matriculado", "alta", "asesor_instituto", "bot", False, "Matriculado 2026-I", 5, 40, 300),
        ("51990001005", "Elena Morales", emp_objs["colegio_comas"], "Secundaria 2do", "organico", "", "frio", "descartado", "baja", "asesor_comas", "bot", False, "", None, 120, 600),
    ]
    for tel, nom, emp, niv, orig, camp, score, est, prio, asig, est_chat, pausado, tags, csat, frt_s, art_s in prospectos:
        usu = Usuario.query.filter_by(telefono=tel).first()
        if not usu:
            usu = Usuario(
                telefono=tel, nombre_wa=nom, empresa_id=emp.id, saludo_enviado=True,
                bot_pausado=pausado, estado_chat=est_chat, asignado_a=asig,
                ultima_vez=ahora - timedelta(minutes=15),
                ultimo_msg_entrada=ahora - timedelta(minutes=15),
            )
            db.session.add(usu)
            db.session.add(Mensaje(telefono=tel, nombre=nom, contenido=f"Hola EVO, quiero información de {niv} en {emp.nombre_comercial}", direccion="ENTRADA", fecha=ahora - timedelta(minutes=20)))
            db.session.add(Mensaje(telefono=tel, nombre=Config.bot_sender(), contenido=f"¡Hola {nom}! Soy {Config.BOT_NAME}, con gusto te brindo la información oficial de {emp.nombre_comercial}.", direccion="SALIDA", estado_envio="read", fecha=ahora - timedelta(minutes=19)))
            if est_chat == "atendido":
                db.session.add(Mensaje(telefono=tel, nombre="[Susurro] supervisor_demo", contenido="Ofrécele visita guiada este sábado con taller demostrativo.", direccion="INTERNO", es_interno=True, fecha=ahora - timedelta(minutes=10)))

        lead = Lead.query.filter_by(telefono=tel).first()
        if not lead:
            creado = ahora - timedelta(hours=3)
            lead = Lead(
                telefono=tel, nombre=nom, empresa_id=emp.id, sede_nombre=emp.nombre_comercial, nivel=niv,
                origen=orig, campana_nombre=camp, score=score, estado=est, prioridad=prio, asignado_a=asig,
                quiere_asesor=(score == "caliente"), tags=tags, csat=csat,
                motivo_descarte="distancia" if est == "descartado" else "",
                resumen_ia=f"Interesado en {niv} ({emp.nombre_comercial}) vía {orig}.",
                creado_en=creado,
                primera_respuesta_en=(creado + timedelta(seconds=frt_s)) if frt_s else None,
                resuelto_en=(creado + timedelta(seconds=art_s)) if art_s else None,
            )
            db.session.add(lead)
            db.session.commit()
            db.session.add(LeadEvento(lead_id=lead.id, tipo="asignacion", detalle=f"Auto-asignado (Round-Robin) a {asig}", usuario="sistema"))
            if est in ("visita", "contactado"):
                db.session.add(Cita(
                    lead_id=lead.id, empresa_id=emp.id, telefono=tel, nombre=nom,
                    fecha_preferida="Mañana", franja="Tarde (2pm - 6pm)",
                    motivo=f"[Visita guiada · {emp.nombre_comercial}] Informes {niv}",
                    asignado_a=asig, estado="confirmada" if est == "visita" else "pendiente",
                ))
    db.session.commit()
    log.info("Simulación CRM completa lista (empresas=%s, leads=%s, citas=%s, brechas=%s).",
             Empresa.query.count(), Lead.query.count(), Cita.query.count(), PreguntaPendiente.query.count())


if __name__ == "__main__":
    comandos = {
        "init-db": init_db,
        "reset-admin": reset_admin,
        "reset-menu": reset_menu,
        "seed-demo-empresas": seed_demo_empresas,
        "seed-demo-crm": seed_demo_crm,
    }
    if len(sys.argv) != 2 or sys.argv[1] not in comandos:
        print(__doc__)
        raise SystemExit(1)
    app = create_app()
    with app.app_context():
        comandos[sys.argv[1]]()
