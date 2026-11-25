#!/usr/bin/env python3
"""
Utilidades de administración.

  python manage.py init-db             Crea tablas y datos iniciales (admin + flujo de ejemplo). Idempotente.
  python manage.py reset-admin         Restablece la contraseña del admin (ADMIN_USER) con el ADMIN_PASS del .env.
  python manage.py reset-menu          BORRA los textos/botones del bot y vuelve a cargar el menú de ejemplo.
  python manage.py seed-demo-empresas  Crea 3 empresas ficticias de prueba en /admin/ia (idempotente).
"""
import sys
import time
import logging

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from app import create_app
from app.models import db, ApiEndpoint, Empresa, PlantillaRapida, RespuestaBot, UsuarioSistema
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


def init_db():
    wait_for_db()
    db.create_all()
    migrar_columnas()
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


if __name__ == "__main__":
    comandos = {
        "init-db": init_db,
        "reset-admin": reset_admin,
        "reset-menu": reset_menu,
        "seed-demo-empresas": seed_demo_empresas,
    }
    if len(sys.argv) != 2 or sys.argv[1] not in comandos:
        print(__doc__)
        raise SystemExit(1)
    app = create_app()
    with app.app_context():
        comandos[sys.argv[1]]()
