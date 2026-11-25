"""Tests de Fase 5: Agenda de Citas (/admin/citas + /api/v1/crm/citas), Handoff Brief IA, CRUD /atajos y tope diario académico."""
from app import asistente
from app.flujos import alumno
from app.models import AuditoriaConsulta, Cita, Empresa, Lead, Mensaje, PlantillaRapida, db
from tests.conftest import csrf_de, login


def test_agenda_citas_api_panel_y_recordatorio(app, client, sent):
    with app.app_context():
        emp = Empresa(clave="wiener", nombre_comercial="U. Norbert Wiener", activo=True)
        db.session.add(emp)
        db.session.commit()
        lead = Lead(telefono="51911223344", nombre="María López", empresa_id=emp.id)
        db.session.add(lead)
        db.session.commit()
        lead_id, emp_id = lead.id, emp.id

    login(client)
    token = csrf_de(client, "/admin/citas")

    # 1. Crear cita vía POST /api/v1/crm/citas
    r_api = client.post(
        "/api/v1/crm/citas",
        json={
            "lead_id": lead_id,
            "empresa_id": emp_id,
            "nombre": "María López",
            "fecha_preferida": "2026-11-26",
            "franja": "Tarde (2pm - 6pm)",
            "motivo": "Informes Medicina Humana",
        },
        headers={"X-CSRFToken": token},
    )
    assert r_api.status_code == 201
    cid = r_api.get_json()["id"]

    # 2. Ver en /admin/citas y enviar recordatorio por WhatsApp
    r_get = client.get("/admin/citas")
    assert r_get.status_code == 200 and "María López" in r_get.get_data(as_text=True)

    r_rec = client.post(f"/admin/citas/{cid}/recordar", data={"csrf_token": token}, follow_redirects=True)
    assert r_rec.status_code == 200
    assert any("recordamos tu llamada/cita" in str(m).lower() for m in sent)

    with app.app_context():
        c = db.session.get(Cita, cid)
        assert c.recordatorio_enviado is True
        assert c.estado == "confirmada"


def test_crud_plantillas_rapidas_en_admin_ia(app, client):
    login(client)
    token = csrf_de(client, "/admin/ia")
    r = client.post(
        "/admin/ia/plantillas/guardar",
        data={
            "csrf_token": token,
            "atajo": "/brochure",
            "titulo": "Envío de Brochure",
            "contenido": "Hola {{usuario}}, aquí tienes la web oficial: {{url_empresa}}",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200
    with app.app_context():
        p = PlantillaRapida.query.filter_by(atajo="/brochure").first()
        assert p is not None and "{{url_empresa}}" in p.contenido
        pid = p.id

    token = csrf_de(client, "/admin/ia")
    client.post(f"/admin/ia/plantillas/{pid}/eliminar", data={"csrf_token": token}, follow_redirects=True)
    with app.app_context():
        assert PlantillaRapida.query.filter_by(atajo="/brochure").first() is None


def test_handoff_brief_y_creacion_cita_desde_ia(app):
    with app.app_context():
        emp = Empresa(clave="carrion", nombre_comercial="Instituto Carrión", activo=True)
        db.session.add(emp)
        db.session.commit()
        db.session.add(Mensaje(telefono="51922334455", nombre="Luis", contenido="Quiero info de Enfermería turno noche", direccion="ENTRADA"))
        db.session.commit()

        est = {"empresa_id": emp.id}
        res = asistente._t_registrar("51922334455", {
            "nombre": "Luis Pérez",
            "sede_id": "carrion",
            "nivel": "Enfermería",
            "visita_preferida": "Mañana 10am",
            "quiere_asesor": True,
            "comentario": "Info",
        }, est)
        assert res["ok"] is True
        lead = Lead.query.filter_by(telefono="51922334455").first()
        assert lead.score == "caliente"
        assert "Enfermería turno noche" in (lead.resumen_ia or "")
        cita = Cita.query.filter_by(lead_id=lead.id).first()
        assert cita is not None and "Mañana 10am" in cita.franja


def test_tope_diario_consultas_academicas(app):
    with app.app_context():
        from config import Config
        old_max = Config.ACAD_MAX_CONSULTAS_DIA
        Config.ACAD_MAX_CONSULTAS_DIA = 2
        try:
            assert alumno._supero_tope_diario("51933445566") is False
            db.session.add(AuditoriaConsulta(telefono="51933445566", tipo="alumno", dni_mask="****1111", resultado="ok"))
            db.session.add(AuditoriaConsulta(telefono="51933445566", tipo="alumno", dni_mask="****2222", resultado="ok"))
            db.session.commit()
            assert alumno._supero_tope_diario("51933445566") is True
        finally:
            Config.ACAD_MAX_CONSULTAS_DIA = old_max
