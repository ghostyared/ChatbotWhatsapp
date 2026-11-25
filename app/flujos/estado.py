"""Estado de conversaciones guiadas (qué pregunta sigue y qué datos llevamos)."""
import json
from datetime import timedelta
from config import Config
from ..models import Conversacion, db, utcnow


def obtener(tel):
    """Conversación activa o None (si expiró, se elimina)."""
    c = Conversacion.query.filter_by(telefono=tel).first()
    if c and c.expira_en < utcnow():
        db.session.delete(c)
        db.session.commit()
        return None
    return c


def datos(c):
    try:
        return json.loads(c.datos or '{}')
    except ValueError:
        return {}


def iniciar(tel, flujo, paso, datos_iniciales=None):
    c = Conversacion.query.filter_by(telefono=tel).first() or Conversacion(telefono=tel)
    c.flujo, c.paso = flujo, paso
    c.datos = json.dumps(datos_iniciales or {})
    c.expira_en = utcnow() + timedelta(minutes=Config.FLOW_TIMEOUT_MINUTES)
    db.session.add(c)
    db.session.commit()
    return c


def avanzar(c, paso, **nuevos):
    d = datos(c)
    d.update(nuevos)
    c.paso = paso
    c.datos = json.dumps(d)
    c.expira_en = utcnow() + timedelta(minutes=Config.FLOW_TIMEOUT_MINUTES)
    db.session.commit()


def terminar(tel):
    Conversacion.query.filter_by(telefono=tel).delete()
    db.session.commit()
