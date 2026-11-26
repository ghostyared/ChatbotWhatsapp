"""Tests de Bloques 1 y 2: Audio fallback Groq Whisper, retornos CMD_BACK / CMD_SWITCH_COMPANY / cancelar, rol supervisor y aislamiento callcenter por empresa."""
from app import bot_logic, ia_gemini
from app.flujos import estado as estado_flujo
from app.models import Cita, Empresa, Lead, Usuario, UsuarioSistema, db
from tests.conftest import csrf_de, login


def test_retornos_cmd_back_cancelar_y_cambiar_empresa(app, sent):
    with app.app_context():
        e1 = Empresa(clave="comas", nombre_comercial="Colegio Comas", activo=True, orden=1)
        e2 = Empresa(clave="ventanilla", nombre_comercial="Colegio Ventanilla", activo=True, orden=2)
        db.session.add_all([e1, e2])
        db.session.commit()

        # 1. Cancelar un flujo en curso muestra confirmación explícita antes del menú
        estado_flujo.iniciar("51911000111", "asesor", "sede", {})
        bot_logic.brain_gethi("51911000111", "cancelar", "", "Yared")
        assert estado_flujo.obtener("51911000111") is None
        assert any("operación cancelada" in str(m).lower() for m in sent)

        # 2. CMD_BACK:m_gest navega al nodo padre m_gest
        sent.clear()
        bot_logic.brain_gethi("51911000111", "Botón: Volver", "CMD_BACK:m_gest", "Yared", tipo="interactive")
        assert any("informes" in str(m).lower() or "vacantes" in str(m).lower() for m in sent)

        # 3. "cambiar empresa" limpia empresa_id y vuelve a pedir selección de institución
        u = Usuario.query.filter_by(telefono="51911000111").first()
        u.empresa_id = e1.id
        db.session.commit()
        sent.clear()
        bot_logic.brain_gethi("51911000111", "cambiar empresa", "", "Yared")
        u = Usuario.query.filter_by(telefono="51911000111").first()
        assert u.empresa_id is None
        assert any("Colegio Comas" in str(m) for m in sent)


def test_rol_supervisor_y_aislamiento_callcenter_por_empresa(app, client):
    with app.app_context():
        e1 = Empresa(clave="comas", nombre_comercial="Colegio Comas", activo=True)
        e2 = Empresa(clave="ventanilla", nombre_comercial="Colegio Ventanilla", activo=True)
        db.session.add_all([e1, e2])
        db.session.commit()

        sup = UsuarioSistema(username="sup1", rol="supervisor", activo=True)
        sup.set_password("Clave-Sup-1234")
        cc = UsuarioSistema(username="cc_comas", rol="callcenter", activo=True, empresa_id=e1.id)
        cc.set_password("Clave-CC-1234")
        l1 = Lead(telefono="51922000111", nombre="Lead Comas", empresa_id=e1.id, estado="nuevo")
        l2 = Lead(telefono="51922000222", nombre="Lead Ventanilla", empresa_id=e2.id, estado="nuevo")
        c1 = Cita(telefono="51922000111", nombre="Cita Comas", empresa_id=e1.id, estado="pendiente")
        c2 = Cita(telefono="51922000222", nombre="Cita Ventanilla", empresa_id=e2.id, estado="pendiente")
        db.session.add_all([sup, cc, l1, l2, c1, c2])
        db.session.commit()

    # 1. Supervisor puede ver KPIs y usar Whisper, pero NO /admin/usuarios
    login(client, "sup1", "Clave-Sup-1234")
    assert client.get("/api/reportes/kpis").status_code == 200
    assert client.get("/admin/usuarios").status_code == 302
    tok = csrf_de(client, "/monitor")
    assert client.post("/api/whisper/51922000111", json={"mensaje": "Buen trabajo"}, headers={"X-CSRFToken": tok}).status_code == 201
    client.get("/logout")

    # 2. Callcenter asignado a Comas solo ve Lead Comas y Cita Comas (no Ventanilla)
    login(client, "cc_comas", "Clave-CC-1234")
    html_leads = client.get("/admin/leads").get_data(as_text=True)
    assert "Lead Comas" in html_leads and "Lead Ventanilla" not in html_leads
    html_citas = client.get("/admin/citas").get_data(as_text=True)
    assert "Cita Comas" in html_citas and "Cita Ventanilla" not in html_citas


def test_transcripcion_audio_fallback_groq(monkeypatch):
    from config import Config
    old_gem, old_groq = Config.GEMINI_API_KEY, Config.GROQ_API_KEY
    Config.GEMINI_API_KEY = ""
    Config.GROQ_API_KEY = "gsk_test_key"

    class _FakeResp:
        status_code = 200
        def json(self):
            return {"text": "Quiero informes de Colegio Comas"}

    monkeypatch.setattr(ia_gemini.requests, "post", lambda *a, **kw: _FakeResp())
    try:
        res = ia_gemini.transcribir_audio(b"OGGDATA", "audio/ogg; codecs=opus")
        assert res == "Quiero informes de Colegio Comas"
    finally:
        Config.GEMINI_API_KEY, Config.GROQ_API_KEY = old_gem, old_groq
