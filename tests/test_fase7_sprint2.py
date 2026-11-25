"""Tests de Fase 7 (Sprint 2): Asignación Round-Robin por empresa/disponibilidad, /api/toggle_online, tags + timeline de leads y cifrado de tokens en BD."""
from app import crypto
from app.flujos.asesor import asignar_asesor_round_robin
from app.models import Empresa, Lead, LeadEvento, UsuarioSistema, db
from tests.conftest import csrf_de, login


def test_round_robin_asigna_por_empresa_y_menor_carga(app):
    with app.app_context():
        emp = Empresa(clave="wiener", nombre_comercial="U. Norbert Wiener", activo=True)
        db.session.add(emp)
        db.session.commit()

        a1 = UsuarioSistema(username="asesor1", rol="callcenter", activo=True, en_linea=True, empresa_id=emp.id, max_chats=5)
        a1.set_password("Clave-Test-1234")
        a2 = UsuarioSistema(username="asesor2", rol="callcenter", activo=True, en_linea=True, empresa_id=emp.id, max_chats=5)
        a2.set_password("Clave-Test-1234")
        a3 = UsuarioSistema(username="asesor_off", rol="callcenter", activo=True, en_linea=False, empresa_id=emp.id, max_chats=5)
        a3.set_password("Clave-Test-1234")
        db.session.add_all([a1, a2, a3])
        db.session.commit()

        # asesor1 ya tiene 2 leads abiertos, asesor2 tiene 0 -> elige asesor2
        db.session.add(Lead(telefono="51900001", asignado_a="asesor1", estado="nuevo"))
        db.session.add(Lead(telefono="51900002", asignado_a="asesor1", estado="contactado"))
        db.session.commit()

        elegido = asignar_asesor_round_robin(emp.id)
        assert elegido == "asesor2"


def test_toggle_online_y_timeline_tags_lead(app, client):
    with app.app_context():
        l = Lead(telefono="519000099", nombre="Sofía", estado="nuevo")
        db.session.add(l)
        db.session.commit()
        lid = l.id

    login(client)
    token = csrf_de(client, "/admin/leads")

    # Toggle en línea -> False -> True
    r1 = client.post("/api/toggle_online", headers={"X-CSRFToken": token})
    assert r1.status_code == 200 and r1.get_json()["en_linea"] is False
    r2 = client.post("/api/toggle_online", headers={"X-CSRFToken": token})
    assert r2.status_code == 200 and r2.get_json()["en_linea"] is True

    # Actualizar lead con tags y cambio de estado -> crea evento en timeline
    r_upd = client.post(
        f"/admin/leads/{lid}/actualizar",
        data={
            "csrf_token": token,
            "estado": "contactado",
            "prioridad": "alta",
            "resultado_llamada": "contactado",
            "asignado_a": "admin",
            "tags": "Beca, Turno Noche",
            "notas": "Interesada en marzo",
        },
        follow_redirects=True,
    )
    assert r_upd.status_code == 200
    with app.app_context():
        l2 = db.session.get(Lead, lid)
        assert l2.tags == "Beca,Turno Noche"
        ev = LeadEvento.query.filter_by(lead_id=lid).first()
        assert ev is not None and "estado: nuevo→contactado" in ev.detalle


def test_cifrado_y_descifrado_tokens_bd():
    # Compatible hacia atrás con tokens en texto plano
    assert crypto.descifrar_token("mi-token-plano") == "mi-token-plano"
    # Ida y vuelta cifrado -> descifrado
    cifrado = crypto.cifrar_token("secreto-api-2026")
    assert crypto.descifrar_token(cifrado) == "secreto-api-2026"
