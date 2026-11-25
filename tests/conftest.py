import os
import sys
import tempfile

# Variables de entorno de PRUEBA (antes de importar la app)
_tmp = tempfile.mkdtemp()
os.environ.update({
    "SECRET_KEY": "k" * 40,
    "TOKEN_PERMANENTE": "test-token",
    "PHONE_ID": "111222333",
    "WEBHOOK_TOKEN": "verify-test",
    "APP_SECRET": "app-secret-test",
    "ADMIN_USER": "admin",
    "ADMIN_PASS": "Clave-De-Prueba-123",
    "SESSION_COOKIE_SECURE": "false",
    "BOT_NAME": "Botty",
    "COMPANY_NAME": "Acme",
    "TRUSTED_PROXIES": "0",
})
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/test.db")  # exporta DATABASE_URL=postgresql://... para probar con PostgreSQL
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hashlib
import hmac
import json
import re

import pytest

from app import create_app
from app.models import db, Colaborador, RespuestaBot, UsuarioSistema
import app.routes as routes
import app.services as services


class _SyncExecutor:
    def submit(self, fn, *a, **kw):
        fn(*a, **kw)


class FakeResp:
    status_code = 200
    text = "{}"

    def json(self):
        return {}


@pytest.fixture()
def sent(monkeypatch):
    """Captura lo que el bot enviaría a Meta."""
    out = []

    def fake_post(url, json=None, headers=None, timeout=None):
        out.append(json)
        return FakeResp()

    monkeypatch.setattr(services.requests, "post", fake_post)
    return out


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setattr(routes, "executor", _SyncExecutor())
    a = create_app()
    with a.app_context():
        db.drop_all()
        db.create_all()
        RespuestaBot.seed()
        UsuarioSistema.seed_admin()
        db.session.add(Colaborador(dni="12345678", nombre="Ana Prueba", empresa="Acme", cargo="Analista", estado="Activo"))
        db.session.add(Colaborador(dni="87654321", nombre="Baja Persona", empresa="Acme", cargo="X", estado="Inactivo"))
        db.session.commit()
    yield a


@pytest.fixture()
def client(app):
    return app.test_client()


def firmar(body: bytes, secret="app-secret-test"):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def payload_texto(tel, texto, wa_id="wamid.1", tipo="text", btn=None, media_id=None, caption=""):
    msg = {"from": tel, "id": wa_id, "type": tipo}
    if tipo == "text":
        msg["text"] = {"body": texto}
    elif tipo == "interactive":
        msg["interactive"] = {"type": "button_reply", "button_reply": {"id": btn, "title": texto}}
    elif tipo == "list":
        msg["type"] = "interactive"
        msg["interactive"] = {"type": "list_reply", "list_reply": {"id": btn, "title": texto}}
    elif tipo in ("image", "document"):
        msg[tipo] = {"id": media_id or "media-1", "mime_type": "image/png", "caption": caption}
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": tel, "profile": {"name": "Tester"}}], "messages": [msg]}}]}]}


def texto_de(payload):
    """Texto visible de un mensaje saliente (texto, botones o lista)."""
    if payload["type"] == "text":
        return payload["text"]["body"]
    return payload["interactive"]["body"]["text"]


def ids_de(payload):
    """Ids de botones o filas de lista de un mensaje saliente."""
    a = payload["interactive"]["action"]
    if "buttons" in a:
        return [b["reply"]["id"] for b in a["buttons"]]
    return [r["id"] for sec in a["sections"] for r in sec["rows"]]


@pytest.fixture()
def post_wa(client):
    def _post(data, firma=True):
        body = json.dumps(data).encode()
        headers = {"Content-Type": "application/json"}
        if firma:
            headers["X-Hub-Signature-256"] = firmar(body)
        return client.post("/webhook", data=body, headers=headers)
    return _post


def csrf_de(client, path):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="csrf-token" content="([^"]+)"', html) or re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1)


def login(client, user="admin", pwd="Clave-De-Prueba-123"):
    token = csrf_de(client, "/login")
    return client.post("/login", data={"u": user, "p": pwd, "csrf_token": token})
