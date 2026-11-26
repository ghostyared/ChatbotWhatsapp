"""Tests de Bloques 3 y 4: Exportación CSV de Leads, motivo_descarte, seguimiento Drip 20h y Webhooks salientes firmados (HMAC)."""
import hashlib
import hmac
from datetime import timedelta

from app import timers, webhooks_out
from app.models import Empresa, Lead, Usuario, WebhookSaliente, db, utcnow
from tests.conftest import csrf_de, login


def test_drip_20h_respeta_ventana_24h_y_opt_out(app, sent):
    from config import Config
    old_drip = Config.DRIP_20H_ENABLED
    Config.DRIP_20H_ENABLED = True
    try:
        with app.app_context():
            ahora = utcnow()
            u_ok = Usuario(telefono="51910000111", nombre_wa="Lucía", ultimo_msg_entrada=ahora - timedelta(hours=21), opt_out=False)
            u_baja = Usuario(telefono="51910000222", nombre_wa="Pedro", ultimo_msg_entrada=ahora - timedelta(hours=21), opt_out=True)
            l_ok = Lead(telefono="51910000111", nombre="Lucía", sede_nombre="Colegio Comas", estado="nuevo", score="tibio")
            l_baja = Lead(telefono="51910000222", nombre="Pedro", sede_nombre="Colegio Comas", estado="nuevo", score="caliente")
            db.session.add_all([u_ok, u_baja, l_ok, l_baja])
            db.session.commit()

            stats = timers.ejecutar_ciclo_timers(ahora=ahora)
            assert stats["drip_20h"] == 1
            assert len(sent) == 1 and sent[0]["to"] == "51910000111"
            assert Lead.query.filter_by(telefono="51910000111").first().seguimiento_20h_enviado is True
    finally:
        Config.DRIP_20H_ENABLED = old_drip


def test_leads_exportar_csv_y_webhook_saliente_hmac(app, client, monkeypatch):
    capturados = []

    class _FakeResp:
        status_code = 200

    def fake_post(url, data=None, headers=None, timeout=None):
        capturados.append((url, data, headers))
        return _FakeResp()

    monkeypatch.setattr(webhooks_out.requests, "post", fake_post)

    with app.app_context():
        l = Lead(telefono="51920000333", nombre="Andrea", sede_nombre="Instituto Pedagógico", estado="contactado", score="caliente")
        h = WebhookSaliente(nombre="n8n Matriculas", evento="lead.won", url="https://n8n.ejemplo.com/hook", secret="mi-secreto", activo=True)
        db.session.add_all([l, h])
        db.session.commit()
        lid = l.id

    login(client)
    # 1. Exportar CSV con BOM UTF-8 y ';'
    r_csv = client.get("/admin/leads/exportar.csv")
    assert r_csv.status_code == 200
    txt = r_csv.get_data(as_text=True)
    assert txt.startswith("\ufeff") and "Andrea" in txt and ";" in txt

    # 2. Pasar lead a matriculado -> dispara webhook lead.won firmado con HMAC-SHA256
    tok = csrf_de(client, "/admin/leads")
    r_upd = client.post(
        f"/admin/leads/{lid}/actualizar",
        data={"csrf_token": tok, "estado": "matriculado", "prioridad": "alta", "resultado_llamada": "matriculado"},
        follow_redirects=True,
    )
    assert r_upd.status_code == 200
    assert len(capturados) == 1
    url, raw_body, headers = capturados[0]
    assert url == "https://n8n.ejemplo.com/hook"
    assert headers["X-EVO-Event"] == "lead.won"
    esperado = "sha256=" + hmac.new(b"mi-secreto", raw_body, hashlib.sha256).hexdigest()
    assert headers["X-EVO-Signature"] == esperado
