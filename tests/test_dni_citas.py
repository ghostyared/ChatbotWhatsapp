"""Tests del flujo de validación de DNI externo (dsgperutech / /admin/endpoints) al agendar citas."""
from app import bot_logic, endpoints_client as ep_cli
from app.flujos import asesor
from app.models import ApiEndpoint, Cita, Lead, Usuario, db


class _FakeResp:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data
        self.text = str(data)

    def json(self):
        return self._data


def test_cita_consulta_dni_externo_confirma_y_guarda_en_bd(app, sent, monkeypatch):
    monkeypatch.setattr(asesor, "en_horario_atencion", lambda ahora_local=None: True)

    def fake_request(method, url, **kw):
        assert method == "GET"
        assert url.endswith("/api-customer/v1/dni/41819975")
        return _FakeResp(200, {
            "success": True,
            "result": {
                "dni": "41819975",
                "name": "DEYVI FRANCO",
                "paternal": "REYES",
                "maternal": "PUICON",
                "status": "OK",
            },
        })

    monkeypatch.setattr(ep_cli.requests, "request", fake_request)

    with app.app_context():
        ApiEndpoint.seed()
        ep = ApiEndpoint.query.filter_by(clave="consultar_dni_externo").first()
        assert ep is not None and ep.activo is True

        # Flujo completo de cita: sede -> modalidad -> día -> horario -> DNI -> confirmación
        tel = "51988112233"
        bot_logic.brain_gethi(tel, "Botón: Cita", "acc_asesor", "Deyvi", tipo="interactive")
        bot_logic.brain_gethi(tel, "Botón: Comas", "ase_sede:Colegio Comas", "Deyvi", tipo="interactive")
        bot_logic.brain_gethi(tel, "Botón: Visita", "ase_tipo:Visita guiada", "Deyvi", tipo="interactive")
        bot_logic.brain_gethi(tel, "Botón: Mañana", "ase_dia:Hoy / Mañana", "Deyvi", tipo="interactive")
        bot_logic.brain_gethi(tel, "Botón: Tarde", "ase_franja:Tarde (2pm - 6pm)", "Deyvi", tipo="interactive")

        # Ingresa DNI de 8 dígitos -> consulta API externa y pide confirmación con botones
        sent.clear()
        bot_logic.brain_gethi(tel, "41819975", "", "Deyvi", tipo="text")
        assert any("DEYVI FRANCO REYES PUICON" in str(m) and "****9975" in str(m) for m in sent)

        # Confirma identidad -> guarda en Usuario, Lead y Cita
        sent.clear()
        bot_logic.brain_gethi(tel, "Botón: Sí, soy yo", "ase_dni_ok:si", "Deyvi", tipo="interactive")
        assert any("Cita confirmada" in str(m) and "DEYVI FRANCO REYES PUICON" in str(m) for m in sent)

        u = Usuario.query.filter_by(telefono=tel).first()
        assert u.dni == "41819975"
        assert u.nombres == "DEYVI FRANCO"
        assert u.apellido_paterno == "REYES"
        assert u.apellido_materno == "PUICON"
        assert u.nombre_completo == "DEYVI FRANCO REYES PUICON"
        assert u.dni_verificado is True

        lead = Lead.query.filter_by(telefono=tel).first()
        assert lead.dni == "41819975" and lead.nombre == "DEYVI FRANCO REYES PUICON"

        cita = Cita.query.filter_by(telefono=tel).first()
        assert cita is not None and cita.dni == "41819975" and cita.nombre == "DEYVI FRANCO REYES PUICON"

        # Segunda vez que agenda cita: ya recuerda el DNI y solo pide confirmación en 1 clic
        sent.clear()
        bot_logic.brain_gethi(tel, "Botón: Cita", "acc_asesor", "Deyvi", tipo="interactive")
        bot_logic.brain_gethi(tel, "Botón: Comas", "ase_sede:Colegio Comas", "Deyvi", tipo="interactive")
        bot_logic.brain_gethi(tel, "Botón: Chat", "ase_tipo:Chat ahora", "Deyvi", tipo="interactive")
        assert any("Tengo registrado tu DNI" in str(m) and "DEYVI FRANCO REYES PUICON" in str(m) for m in sent)


def test_cita_dni_no_encontrado_permite_respaldo_manual(app, sent, monkeypatch):
    monkeypatch.setattr(asesor, "en_horario_atencion", lambda ahora_local=None: False)
    monkeypatch.setattr(ep_cli.requests, "request", lambda *a, **kw: _FakeResp(200, {"success": False}))

    with app.app_context():
        ApiEndpoint.seed()
        tel = "51988445566"
        bot_logic.brain_gethi(tel, "Botón: Cita", "acc_asesor", "Visitante", tipo="interactive")
        bot_logic.brain_gethi(tel, "Botón: Mañana", "ase_franja:Mañana (9am - 12pm)", "Visitante", tipo="interactive")

        sent.clear()
        bot_logic.brain_gethi(tel, "00000000", "", "Visitante", tipo="text")
        assert any("No pude validar ese *DNI*" in str(m) for m in sent)

        # Respaldo manual: escribe nombre y sí agenda la cita sin perder el lead
        sent.clear()
        bot_logic.brain_gethi(tel, "Carlos Extranjero", "", "Visitante", tipo="text")
        assert any("Cita confirmada" in str(m) for m in sent)
        assert Cita.query.filter_by(telefono=tel).first().nombre == "Carlos Extranjero"
