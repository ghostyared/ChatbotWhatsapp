"""Tests de Fase 2: Cola de aprendizaje IA (Knowledge Gaps), listas/documentos en nodos y citas fuera de horario."""
import json
from datetime import datetime

from app import asistente, bot_logic
from app.flujos import asesor
from app.models import Empresa, Lead, PreguntaPendiente, RespuestaBot, db
from tests.conftest import csrf_de, login


def test_knowledge_gap_deduplica_y_aprueba_a_empresa(app, client):
    with app.app_context():
        emp = Empresa(
            clave="colegio_norte",
            nombre_comercial="Colegio Norte",
            base_conocimiento="Nivel Primaria: pensión S/ 450.",
            activo=True,
        )
        db.session.add(emp)
        db.session.commit()
        emp_id = emp.id

        # Dos usuarios preguntan lo mismo (con diferente puntuación/mayúsculas) -> deduplica y suma veces=2
        p1 = PreguntaPendiente.registrar("¿Tienen piscina?", empresa_id=emp_id, telefono="51911111111")
        p2 = PreguntaPendiente.registrar("tienen piscina", empresa_id=emp_id, telefono="51922222222")
        assert p1.id == p2.id
        assert p2.veces == 2
        pid = p1.id

    login(client)
    token = csrf_de(client, "/admin/ia")
    r = client.post(
        f"/admin/ia/preguntas/{pid}/aprobar",
        data={
            "csrf_token": token,
            "empresa_id": str(emp_id),
            "respuesta_oficial": "Sí, contamos con piscina temperada para talleres de natación.",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200
    with app.app_context():
        p = db.session.get(PreguntaPendiente, pid)
        emp = db.session.get(Empresa, emp_id)
        assert p.estado == "aprobada"
        assert "piscina temperada" in emp.base_conocimiento


def test_tool_registrar_duda_sin_contexto(app):
    with app.app_context():
        emp = Empresa(clave="carrion", nombre_comercial="Instituto Carrión", base_conocimiento="Enfermería", activo=True)
        db.session.add(emp)
        db.session.commit()

        est = {"empresa_id": emp.id}
        res = asistente._t_registrar_duda("51933333333", {"pregunta": "¿Aceptan traslado desde provincia?"}, est)
        assert res["ok"] is True and res["registrado"] is True
        gap = PreguntaPendiente.query.filter_by(empresa_id=emp.id, estado="pendiente").first()
        assert gap is not None
        assert "traslado" in gap.pregunta.lower()


def test_nodo_lista_y_brochure_pdf(app, sent):
    with app.app_context():
        nodo = RespuestaBot(
            clave="info_carreras_test",
            titulo="Carreras disponibles",
            texto="Elige la carrera de tu interés:",
            tipo_nodo="lista",
            texto_boton_lista="Ver carreras",
            documento_url="https://ejemplo.edu.pe/brochure.pdf",
            documento_nombre="Brochure_2026.pdf",
            botones_json=json.dumps([
                {"id": "c_enf", "title": "Enfermería", "description": "3 años · Salud"},
                {"id": "c_far", "title": "Farmacia", "description": "3 años · Salud"},
                {"id": "c_fis", "title": "Fisioterapia", "description": "3 años · Salud"},
                {"id": "c_lab", "title": "Laboratorio Clínico", "description": "3 años · Salud"},
            ]),
        )
        db.session.add(nodo)
        db.session.commit()

        ok = bot_logic.enviar_nodo("51944444444", "info_carreras_test")
        assert ok is True
        assert any(m.get("type") == "interactive" and m["interactive"]["type"] == "list" for m in sent)
        assert any(m.get("type") == "document" and m["document"]["filename"] == "Brochure_2026.pdf" for m in sent)


def test_asesor_fuera_de_horario_agenda_cita(app, sent, monkeypatch):
    monkeypatch.setattr(asesor, "en_horario_atencion", lambda ahora_local=None: False)
    with app.app_context():
        # Inicia flujo fuera de horario -> pide franja horaria
        bot_logic.brain_gethi("51955555555", "Botón: Asesor", "acc_asesor", "Yared", tipo="interactive")
        assert any("fuera de horario" in str(m).lower() for m in sent)

        sent.clear()
        bot_logic.brain_gethi("51955555555", "Botón: Tarde (2-6)", "ase_franja:Tarde (2pm - 6pm)", "Yared", tipo="interactive")
        assert any("tarde" in str(m).lower() for m in sent)

        sent.clear()
        bot_logic.brain_gethi("51955555555", "Yared Cruz - Informes Enfermería", "", "Yared", tipo="text")
        lead = Lead.query.filter_by(telefono="51955555555").first()
        assert lead is not None
        assert lead.resultado_llamada == "cita_agendada"
        assert lead.estado == "visita"
        assert "Tarde" in (lead.visita_pref or "")
        assert any("agendamos tu llamada" in str(m).lower() for m in sent)


def test_en_horario_atencion_valida_dias_y_horas():
    # Miércoles 10:00 -> dentro de horario (L-V 8-18)
    assert asesor.en_horario_atencion(datetime(2026, 11, 25, 10, 0)) is True
    # Miércoles 21:00 -> fuera de horario
    assert asesor.en_horario_atencion(datetime(2026, 11, 25, 21, 0)) is False
    # Domingo 11:00 -> fuera de horario
    assert asesor.en_horario_atencion(datetime(2026, 11, 29, 11, 0)) is False
