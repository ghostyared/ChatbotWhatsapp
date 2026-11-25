import io
import pytest
import openpyxl

from config import Config
from app.models import db, Usuario, Mensaje, UsuarioSistema, Colaborador
from conftest import payload_texto, login, csrf_de, firmar


# ---------- configuración ----------
def test_validate_falla_sin_variables(monkeypatch):
    monkeypatch.setattr(Config, "TOKEN_PERMANENTE", "")
    monkeypatch.setattr(Config, "APP_SECRET", "CAMBIAR_ESTO")
    with pytest.raises(RuntimeError) as e:
        Config.validate()
    assert "TOKEN_PERMANENTE" in str(e.value) and "APP_SECRET" in str(e.value)


def test_validate_ok():
    Config.validate()


# ---------- webhook ----------
def test_verify_ok_y_mal_token(client):
    r = client.get("/webhook?hub.mode=subscribe&hub.verify_token=verify-test&hub.challenge=abc123")
    assert r.status_code == 200 and r.get_data(as_text=True) == "abc123"
    assert client.get("/webhook?hub.mode=subscribe&hub.verify_token=mal&hub.challenge=x").status_code == 403
    assert client.get("/webhook?hub.verify_token=verify-test&hub.challenge=x").status_code == 403  # sin mode


def test_post_sin_firma_o_firma_falsa(client, post_wa, sent):
    assert post_wa(payload_texto("51900000001", "hola"), firma=False).status_code == 403
    r = client.post("/webhook", data=b"{}", headers={"X-Hub-Signature-256": "sha256=00", "Content-Type": "application/json"})
    assert r.status_code == 403
    assert sent == []


def test_flujo_completo_con_validacion_de_dni(app, monkeypatch, post_wa, sent):
    monkeypatch.setattr(Config, "REQUIRE_ID_VALIDATION", True)
    tel = "51900000002"
    assert post_wa(payload_texto(tel, "hola", "w1")).status_code == 200
    assert "ingresa tu número de DNI" in sent[-1]["text"]["body"]

    post_wa(payload_texto(tel, "99999999", "w2"))                        # DNI inexistente
    assert "No logré validar" in sent[-1]["text"]["body"]

    post_wa(payload_texto(tel, "87654321", "w3"))                        # DNI inactivo
    assert "No logré validar" in sent[-1]["text"]["body"]

    post_wa(payload_texto(tel, "12345678", "w4"))                        # válido
    ult = sent[-1]
    assert ult["type"] == "interactive" and "Ana Prueba" in ult["interactive"]["body"]["text"]
    assert [b["reply"]["id"] for b in ult["interactive"]["action"]["buttons"]] == ["m_gest", "m_segu", "m_bene"]
    with app.app_context():   # el DNI escrito se guarda enmascarado en el historial
        contenidos = [m.contenido for m in Mensaje.query.filter_by(telefono=tel, direccion="ENTRADA").all()]
    assert "DNI ****5678" in contenidos and "12345678" not in contenidos

    post_wa(payload_texto(tel, "1️⃣ Servicios", "w5", tipo="interactive", btn="m_gest"))
    assert [b["reply"]["id"] for b in sent[-1]["interactive"]["action"]["buttons"]] == ["v_colegio", "v_instituto", "f_matricula"]

    post_wa(payload_texto(tel, "Matrícula", "w6", tipo="interactive", btn="f_matricula"))
    assert "Proceso de matrícula" in sent[-1]["interactive"]["body"]["text"]

    post_wa(payload_texto(tel, "🏠 Inicio", "w7", tipo="interactive", btn="ir_inicio"))
    assert "Botty" in sent[-1]["interactive"]["body"]["text"]

    post_wa(payload_texto(tel, "x", "w8", tipo="interactive", btn="no_existe"))
    assert "no disponible" in sent[-1]["text"]["body"]

    with app.app_context():
        assert Usuario.query.filter_by(telefono=tel).first().dni == "12345678"
        assert Mensaje.query.filter_by(telefono=tel, direccion="SALIDA").count() >= 6


def test_duplicado_de_meta_no_se_procesa_dos_veces(post_wa, sent):
    p = payload_texto("51900000003", "hola", "dup-1")
    post_wa(p)
    n = len(sent)
    assert post_wa(p).status_code == 200
    assert len(sent) == n


def test_tipo_no_soportado(post_wa, sent):
    data = payload_texto("51900000004", "", "aud-1", tipo="audio")
    assert post_wa(data).status_code == 200
    assert "solo puedo leer" in sent[-1]["text"]["body"]


def test_imagen_fuera_de_flujo_ofrece_enviar_comprobante(post_wa, sent):
    post_wa(payload_texto("51900000009", "", "img-9", tipo="image"))
    assert "comp_enviar" in [b["reply"]["id"] for b in sent[-1]["interactive"]["action"]["buttons"]]


def test_bloqueo_por_intentos_de_id(app, monkeypatch, post_wa, sent):
    monkeypatch.setattr(Config, "REQUIRE_ID_VALIDATION", True)
    tel = "51900000005"
    for i in range(Config.ID_MAX_ATTEMPTS):
        post_wa(payload_texto(tel, f"1000000{i}", f"b{i}"))
    post_wa(payload_texto(tel, "12345678", "b-final"))      # incluso el DNI correcto queda bloqueado
    assert "máximo de intentos" in sent[-1]["text"]["body"]


def test_bot_pausado_no_responde(app, post_wa, sent):
    tel = "51900000006"
    post_wa(payload_texto(tel, "hola", "p1"))
    with app.app_context():
        Usuario.query.filter_by(telefono=tel).first().bot_pausado = True
        db.session.commit()
    n = len(sent)
    post_wa(payload_texto(tel, "hola", "p2"))
    assert len(sent) == n


def test_webhook_no_tiene_rate_limit_global(post_wa, sent):
    for i in range(320):  # el límite por defecto del panel es 300/hora
        assert post_wa(payload_texto("51900000007", "hola", f"rl-{i}")).status_code == 200


def test_sin_validacion_de_id_por_defecto(post_wa, sent):
    post_wa(payload_texto("51900000008", "hola", "nv1"))
    assert sent[-1]["type"] == "interactive"
    assert [b["reply"]["id"] for b in sent[-1]["interactive"]["action"]["buttons"]] == ["m_gest", "m_segu", "m_bene"]


# ---------- panel ----------
def test_rutas_protegidas_redirigen(client):
    for ruta in ("/dashboard", "/monitor", "/admin/editor", "/admin/colaboradores", "/admin/usuarios", "/admin/comprobantes"):
        assert client.get(ruta).status_code == 302
    assert client.get("/api/chat_updates").status_code == 401


def test_login_malo_y_bueno(client):
    assert b"Credenciales incorrectas" in login(client, pwd="mala").data
    r = login(client)
    assert r.status_code == 302 and "/dashboard" in r.headers["Location"]


def test_no_existen_cuentas_qa(app):
    with app.app_context():
        nombres = {u.username for u in UsuarioSistema.query.all()}
    assert nombres == {"admin"}


def test_paginas_admin_renderizan(client):
    login(client)
    for ruta in ("/dashboard", "/monitor", "/admin/editor", "/admin/colaboradores", "/admin/usuarios", "/admin/comprobantes", "/health"):
        r = client.get(ruta)
        assert r.status_code == 200, ruta
    assert b"Botty" in client.get("/monitor").data
    assert b"Wiener" not in client.get("/dashboard").data


def test_csrf_obligatorio_en_api(client, sent):
    login(client)
    assert client.post("/api/toggle_bot/51911111111").status_code == 400        # sin token
    token = csrf_de(client, "/monitor")
    r = client.post("/api/send_message/51911111111", json={"mensaje": "hola"}, headers={"X-CSRFToken": token})
    assert r.status_code == 200 and sent[-1]["text"]["body"] == "hola"


def test_lector_no_puede_enviar_ni_pausar(app, client, sent):
    with app.app_context():
        u = UsuarioSistema(username="lector1", rol="lector", activo=True)
        u.set_password("Otra-Clave-Larga-1")
        db.session.add(u)
        db.session.commit()
    assert login(client, "lector1", "Otra-Clave-Larga-1").status_code == 302
    token = csrf_de(client, "/monitor")
    assert client.get("/api/chat_updates").status_code == 200
    assert client.post("/api/send_message/5191", json={"mensaje": "x"}, headers={"X-CSRFToken": token}).status_code == 403
    assert client.post("/api/toggle_bot/5191", headers={"X-CSRFToken": token}).status_code == 403
    assert client.get("/admin/usuarios").status_code == 302
    assert sent == []


def test_login_no_refleja_texto_arbitrario(client):
    assert b"alert(1)" not in client.get("/login?error=<script>alert(1)</script>").data
    assert b"cuenta ha sido desactivada" in client.get("/login?error=desactivada").data


def test_contrasena_minima_al_crear_usuario(client):
    login(client)
    token = csrf_de(client, "/admin/usuarios")
    r = client.post("/admin/usuarios", data={"action": "create", "username": "nuevo", "password": "corta",
                                            "nombre_completo": "N", "rol": "lector", "csrf_token": token})
    assert b"al menos 10 caracteres" in r.data


def test_editor_valida_y_guarda(app, client):
    login(client)
    token = csrf_de(client, "/admin/editor")
    base = {"csrf_token": token, "action": "save", "clave": "f_nuevo", "titulo": "Nuevo", "texto": "Hola {nombre}"}
    assert client.post("/admin/editor", data={**base, "clave": "MAL CLAVE!"}).headers["Location"].endswith("error=clave")
    assert "error=link" in client.post("/admin/editor", data={**base, "link": "javascript:alert(1)"}).headers["Location"]
    assert "error=botones" in client.post("/admin/editor", data={**base, "botones_json": '[{"id":"a","title":"%s"}]' % ("x" * 21)}).headers["Location"]
    r = client.post("/admin/editor", data={**base, "link": "https://ejemplo.com", "botones_json": '[{"id":"ir_inicio","title":"Inicio"}]'})
    assert "saved=1" in r.headers["Location"]


def test_importar_excel(app, client):
    login(client)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["DNI", "Nombre", "Empresa", "Cargo"])
    ws.append([7845123, "Pedro", "Acme", "Jefe"])         # número de 7 dígitos -> se completa con 0
    ws.append(["11112222", "Lucia", "Beta", None])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    token = csrf_de(client, "/admin/colaboradores")
    r = client.post("/admin/colaboradores", data={"csrf_token": token, "archivo_excel": (buf, "x.xlsx")},
                    content_type="multipart/form-data")
    assert "success_msg" in r.headers["Location"]
    with app.app_context():
        assert Colaborador.query.filter_by(dni="07845123").first().nombre == "Pedro"
        assert Colaborador.query.filter_by(dni="11112222").first().cargo == "-"
    # tipo de archivo incorrecto
    r = client.post("/admin/colaboradores", data={"csrf_token": token, "archivo_excel": (io.BytesIO(b"x"), "x.csv")},
                    content_type="multipart/form-data")
    assert "error" in r.headers["Location"]


def test_cabeceras_de_seguridad(client):
    r = client.get("/login")
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["X-Content-Type-Options"] == "nosniff"
