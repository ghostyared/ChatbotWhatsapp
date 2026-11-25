#!/usr/bin/env python3
"""
Utilidades de administración.

  python manage.py init-db        Crea tablas y datos iniciales (admin + flujo de ejemplo). Idempotente.
  python manage.py reset-admin    Restablece la contraseña del admin (ADMIN_USER) con el ADMIN_PASS del .env.
  python manage.py reset-menu     BORRA los textos/botones del bot y vuelve a cargar el menú de ejemplo.
"""
import sys
import time
import logging

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from app import create_app
from app.models import db, ApiEndpoint, RespuestaBot, UsuarioSistema
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
    ("comprobantes", "contenido", "BLOB"),     # se traduce a BYTEA en PostgreSQL
    ("leads", "asignado_a", "VARCHAR(60)"),
    ("leads", "prioridad", "VARCHAR(15) DEFAULT 'media'"),
    ("leads", "telefono_contacto", "VARCHAR(30)"),
    ("leads", "resultado_llamada", "VARCHAR(30) DEFAULT 'pendiente'"),
    ("leads", "resumen_ia", "TEXT"),
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


if __name__ == "__main__":
    comandos = {"init-db": init_db, "reset-admin": reset_admin, "reset-menu": reset_menu}
    if len(sys.argv) != 2 or sys.argv[1] not in comandos:
        print(__doc__)
        raise SystemExit(1)
    app = create_app()
    with app.app_context():
        comandos[sys.argv[1]]()
