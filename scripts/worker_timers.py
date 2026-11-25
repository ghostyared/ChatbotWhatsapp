#!/usr/bin/env python3
"""
Worker dedicado para temporizadores de SLA (asesores) e inactividad de usuarios.
Corre como proceso único (servicio `worker` en docker-compose.yml) cada 30 segundos
para evitar mensajes duplicados entre múltiples workers de Gunicorn.
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app import create_app
from app.models import db
from app.timers import ejecutar_ciclo_timers

INTERVALO_SEG = int(os.environ.get("WORKER_INTERVAL_SECONDS", "30") or "30")
LOCK_KEY = 774202601  # advisory lock fijo para garantizar instancia única

log = logging.getLogger("worker_timers")


def main():
    app = create_app()
    log.info("Worker de timers iniciado (intervalo=%ss).", INTERVALO_SEG)
    while True:
        with app.app_context():
            got_lock = True
            if db.engine.dialect.name == "postgresql":
                got_lock = bool(db.session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar())
            if got_lock:
                try:
                    stats = ejecutar_ciclo_timers()
                    if any(stats.values()):
                        log.info("Ciclo timers: %s", stats)
                except Exception:
                    log.exception("Error ejecutando ciclo de timers")
                    db.session.rollback()
                finally:
                    if db.engine.dialect.name == "postgresql":
                        try:
                            db.session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
                        except Exception:
                            pass
            db.session.remove()
        time.sleep(max(5, INTERVALO_SEG))


if __name__ == "__main__":
    main()
