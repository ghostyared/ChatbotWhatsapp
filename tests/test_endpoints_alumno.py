"""Pruebas para Endpoints API (/admin/endpoints), motor de variables dinámicas y flujo Soy Alumno."""
import itertools

import pytest

import app.endpoints_client as ep_cli
from app.models import ApiEndpoint, AuditoriaConsulta, Conversacion, Mensaje, db
from conftest import csrf_de, ids_de, login, payload_texto, texto_de

TEL = "51900000088"
_n = itertools.count(1)


class _FakeResp:
    def __init__(self, code=200, data=None, text=None):
        self.status_code = code
        self._data = data
        self.text = text if text is not None else (str(data) if data is not None else "")

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


@pytest.fixture()
def chat(post_wa, sent):
    class Chat:
        tel = TEL

        def texto(self, t, tel=None):
            post_wa(payload_texto(tel or self.tel, t, f"a{next(_n)}"))
            return sent[-1]

        def boton(self, id_, titulo="x", tel=None):
            post_wa(payload_texto(tel or self.tel, titulo, f"a{next(_n)}", tipo="interactive", btn=id_))
            return sent[-1]

        def lista(self, id_, titulo="x", tel=None):
            post_wa(payload_texto(tel or self.tel, titulo, f"a{next(_n)}", tipo="list", btn=id_))
            return sent[-1]
    return Chat()


# ------------------------------------------------------------------ motor de variables dinámicas
def test_renderizar_body_tipos_y_seguridad():
    tpl = '{"dni": "{dni}", "IdEstudiante": {IdEstudiante}, "IdPeriodo": {IdPeriodo}}'
    out = ep_cli.renderizar_body(tpl, {"dni": "78744420", "IdEstudiante": 8844, "IdPeriodo": "165"})
    assert out == {"dni": "78744420", "IdEstudiante": 8844, "IdPeriodo": 165}
    assert ep_cli.extraer_variables(tpl) == ["dni", "IdEstudiante", "IdPeriodo"]

    # Escapa caracteres que podrían romper el JSON
    out_esc = ep_cli.renderizar_body('{"dni": "{dni}"}', {"dni": '123", "hack": true'})
    assert out_esc == {"dni": '123", "hack": true'}

    ok, err = ep_cli.validar_body_template(tpl)
    assert ok is True and err is None

    ok_bad, err_bad = ep_cli.validar_body_template('{"dni": {invalido')
    assert ok_bad is False and err_bad


# ------------------------------------------------------------------ panel /admin/endpoints
def test_panel_endpoints_crud_y_probar(app, client, monkeypatch):
    assert client.get("/admin/endpoints").status_code == 302  # requiere login
    login(client)
    r = client.get("/admin/endpoints")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "PeriodosEstudiante.php" in html and "ConsultarPagos.php" in html

    token = csrf_de(client, "/admin/endpoints")
    # Crear un nuevo endpoint
    r = client.post("/admin/endpoints/guardar", data={
        "csrf_token": token,
        "clave": "mi_endpoint",
        "nombre": "Mi Endpoint",
        "metodo": "POST",
        "url": "https://api.mariategui.edu.pe/Test.php",
        "auth_header": "Authorization",
        "auth_scheme": "Bearer",
        "token": "tok-secreto-1234",
        "headers_json": '{"Content-Type": "application/json"}',
        "body_template": '{"IdEstudiante": {IdEstudiante}}',
        "timeout": "10",
        "activo": "1",
    }, follow_redirects=True)
    assert r.status_code == 200 and "guardado correctamente" in r.get_data(as_text=True)
    assert "tok-secreto-1234" not in r.get_data(as_text=True)  # el token completo no se expone

    # Probar endpoint vía API
    visto = {}

    def fake_req(method, url, **kw):
        visto.update(method=method, url=url, **kw)
        return _FakeResp(200, {"ok": True, "recibido": kw.get("json")})

    monkeypatch.setattr(ep_cli.requests, "request", fake_req)
    with app.app_context():
        eid = ApiEndpoint.query.filter_by(clave="mi_endpoint").first().id

    res = client.post(
        f"/api/endpoints/{eid}/probar",
        json={"variables": {"IdEstudiante": "8830"}},
        headers={"X-CSRFToken": token},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True and body["sent_body"] == {"IdEstudiante": 8830}
    assert visto["headers"]["Authorization"] == "Bearer tok-secreto-1234"


# ------------------------------------------------------------------ flujo SOY ALUMNO completo
def test_flujo_soy_alumno_un_periodo_y_cuotas(app, chat, sent, monkeypatch):
    with app.app_context():
        ApiEndpoint.seed()

    llamadas = []

    def fake_req(method, url, **kw):
        llamadas.append((url, kw.get("json")))
        if "PeriodosEstudiante.php" in url:
            return _FakeResp(200, {
                "success": True,
                "estudiante": {
                    "IdEstudiante": 8844,
                    "Dni": "91463623",
                    "Nombres": "Isaac De Jesús",
                    "ApellidoP": "Rodriguez",
                    "ApellidoM": "Onton",
                    "NombreCompleto": "Rodriguez Onton Isaac De Jesús",
                },
                "total_periodos": 1,
                "periodos": [{
                    "IdInscripcion": 59011,
                    "IdEmpresa": 45695,
                    "NombreEmpresa": "Colegio Mariategui Secundaria",
                    "IdPeriodo": 165,
                    "NombrePeriodo": "2027-1",
                    "IdCarrera": 10,
                    "IdClase": 3,
                    "Carrera": "NIVEL_SECUNDARIA",
                    "IdCiclo": 23,
                    "Ciclo": "7mo_Grado",
                    "IdEstudiante": 8844,
                }],
            })
        if "ConsultarPagos.php" in url:
            return _FakeResp(200, {
                "success": True,
                "IdEstudiante": 8844,
                "IdPeriodo": 165,
                "resumen": {
                    "total_cuotas": 2,
                    "cuotas_canceladas": 1,
                    "cuotas_vencidas": 1,
                    "cuotas_pendientes": 0,
                    "monto_total_periodo": 1300,
                    "total_pagado": 650,
                    "total_deuda_pendiente": 650,
                    "total_deuda_vencida": 650,
                },
                "cuotas": [
                    {
                        "IdCuota": 472, "NombreCuota": "MATRICULA",
                        "FechaHoraPago": "2026-10-27 08:00:12", "FechaFinalPago": "2027-02-26 13:34:00",
                        "MontoCancelar": 650, "MontoPagado": 650, "DeudaPendiente": 0, "Estado": "CANCELADO",
                    },
                    {
                        "IdCuota": 478, "NombreCuota": "OCTUBRE",
                        "FechaHoraPago": None, "FechaFinalPago": "2026-09-30 13:36:00",
                        "MontoCancelar": 650, "MontoPagado": 0, "DeudaPendiente": 650, "Estado": "DEUDA",
                    },
                ],
            })
        if "ConsultarNotas.php" in url:
            return _FakeResp(200, {
                "success": True,
                "estudiante": {"IdEstudiante": 8844, "NombreCompleto": "Rodriguez Onton Isaac De Jesús"},
                "matricula": {
                    "NombreEmpresa": "Colegio Mariategui Secundaria",
                    "TipoEmpresa": 1,
                    "NombrePeriodo": "2027-1",
                    "Carrera": "NIVEL_SECUNDARIA",
                    "Ciclo": "7mo_Grado",
                },
                "columnas_subperiodos": ["IB", "IIB", "IIIB", "IVB"],
                "formato_principal": "cualitativo",
                "total_cursos": 2,
                "cursos": [
                    {
                        "Curso": "ÁLGEBRA", "IB": "A", "IIB": "B", "IIIB": "A", "IVB": "—", "PromFinal": "A",
                        "notas_numericas": {"IB": 16.5, "IIB": 12.3, "IIIB": 14.4, "IVB": None, "PromFinal": 14.4},
                        "notas_cualitativas": {"IB": "A", "IIB": "B", "IIIB": "A", "IVB": "—", "PromFinal": "A"},
                    },
                ],
                "total_pts": {"IB": 283.98, "IIB": 270.3, "IIIB": 266.65, "IVB": 0, "Total": 820.93},
            })
        return _FakeResp(404, {})

    monkeypatch.setattr(ep_cli.requests, "request", fake_req)

    # 1. Al pulsar Soy Alumno (m_segu) con endpoints activos, pide DNI directamente
    r0 = chat.boton("m_segu")
    assert "DNI" in texto_de(r0)

    # 2. Ingresa DNI -> consulta PeriodosEstudiante.php y pide confirmación
    r1 = chat.texto("91463623")
    assert "Rodriguez Onton Isaac De Jesús" in texto_de(r1)
    assert ids_de(r1) == ["alu_conf:si", "alu_conf:no"]
    assert llamadas[0][1] == {"dni": "91463623"}

    # 3. Confirma identidad -> como hay 1 periodo, muestra Notas / Cuotas de pago / Comunicados
    r2 = chat.boton("alu_conf:si")
    assert ids_de(r2) == ["alu_op:notas", "alu_op:pagos", "alu_op:comunicados"]

    # 4. Consulta Cuotas de pago -> llama ConsultarPagos.php con IdEstudiante=8844 e IdPeriodo=165
    chat.boton("alu_op:pagos")
    assert llamadas[1][1] == {"IdEstudiante": 8844, "IdPeriodo": 165}
    reportes = [texto_de(p) for p in sent if p["type"] == "text" and "Cuotas de pago" in texto_de(p)]
    assert reportes
    rep = reportes[-1]
    assert "MATRICULA" in rep and "26 feb 2027" in rep and "CANCELADO" in rep
    assert "OCTUBRE" in rep and "30 sep 2026" in rep and "650.00" in rep and "DEUDA" in rep
    assert "Total pendiente: S/ 650.00" in rep

    # 5. Notas (activo para Colegio) -> llama ConsultarNotas.php y formatea boleta
    chat.boton("alu_op:notas")
    assert llamadas[2][1] == {"IdEstudiante": 8844, "IdPeriodo": 165}
    boletas = [texto_de(p) for p in sent if p["type"] == "text" and "Boleta de Notas" in texto_de(p)]
    assert boletas
    bol = boletas[-1]
    assert "```" in bol and "CURSO" in bol and "IB" in bol
    assert "Álgebra" in bol and "16.5" in bol and "12.3" in bol and "14.4" in bol
    assert "Total puntos: 820.93" in bol

    # 6. Comunicados (inactivo por ahora) informa amablemente sin romper la sesión
    r_com = chat.boton("alu_op:comunicados")
    assert "disponible muy pronto" in texto_de(r_com)

    with app.app_context():
        assert AuditoriaConsulta.query.filter_by(tipo="pagos", resultado="ok").count() == 1
        assert AuditoriaConsulta.query.filter_by(tipo="notas", resultado="ok").count() == 1
        salidas = [m.contenido for m in Mensaje.query.filter_by(telefono=TEL, direccion="SALIDA").all()]
        assert "[Reporte de cuotas de pago enviado]" in salidas
        assert "[Reporte de notas enviado]" in salidas
        assert not any("650.00" in s for s in salidas)
        assert not any("ÁLGEBRA" in s for s in salidas)


def test_flujo_soy_alumno_varios_periodos(app, chat, sent, monkeypatch):
    with app.app_context():
        ApiEndpoint.seed()

    def fake_req(method, url, **kw):
        if "PeriodosEstudiante.php" in url:
            return _FakeResp(200, {
                "success": True,
                "estudiante": {"IdEstudiante": 8830, "NombreCompleto": "María López"},
                "total_periodos": 2,
                "periodos": [
                    {"IdInscripcion": 1, "IdEmpresa": 10, "NombreEmpresa": "Colegio Sede 1", "IdPeriodo": 160,
                     "NombrePeriodo": "2026-2", "IdCarrera": 5, "Carrera": "PRIMARIA", "IdCiclo": 12, "Ciclo": "6to", "IdEstudiante": 8830},
                    {"IdInscripcion": 2, "IdEmpresa": 20, "NombreEmpresa": "Instituto Pedagógico", "IdPeriodo": 165,
                     "NombrePeriodo": "2027-1", "IdCarrera": 8, "Carrera": "EDUC_INICIAL", "IdCiclo": 1, "Ciclo": "I_Ciclo", "IdEstudiante": 8830},
                ],
            })
        return _FakeResp(200, {"success": True, "pagos": []})

    monkeypatch.setattr(ep_cli.requests, "request", fake_req)

    chat.boton("acc_alumno")
    chat.texto("78744420")
    r_list = chat.boton("alu_conf:si")
    assert r_list["interactive"]["type"] == "list"
    assert ids_de(r_list) == ["alu_per:0", "alu_per:1"]

    r_menu = chat.lista("alu_per:1")
    assert "Instituto Pedagógico" in texto_de(r_menu)
    assert ids_de(r_menu) == ["alu_op:notas", "alu_op:pagos", "alu_op:comunicados"]

    # Pedagógico NO consulta ConsultarNotas.php: avisa que notas es solo para Colegio
    r_notas_inst = chat.boton("alu_op:notas")
    assert "únicamente para estudiantes de *Colegio*" in texto_de(r_notas_inst)

    with app.app_context():
        c = Conversacion.query.filter_by(telefono=TEL).one()
        import json
        sel = json.loads(c.datos)["seleccion"]
        assert (sel["IdEstudiante"], sel["IdEmpresa"], sel["IdCarrera"], sel["IdCiclo"], sel["IdPeriodo"]) == (8830, 20, 8, 1, 165)


def test_texto_libre_consulta_notas_y_comprobante_directo(app, chat, sent, monkeypatch):
    with app.app_context():
        ApiEndpoint.seed()

    def fake_req(method, url, **kw):
        if "PeriodosEstudiante.php" in url:
            return _FakeResp(200, {
                "success": True,
                "estudiante": {"IdEstudiante": 8011, "NombreCompleto": "Antonela Riofrio"},
                "periodos": [{
                    "IdInscripcion": 58329, "IdEmpresa": 45694, "NombreEmpresa": "Colegio Mariategui Primaria",
                    "IdPeriodo": 153, "NombrePeriodo": "2026-0", "IdCarrera": 9, "Carrera": "NIVEL_PRIMARIA",
                    "IdCiclo": 22, "Ciclo": "6to_Grado", "IdEstudiante": 8011,
                }],
            })
        if "ConsultarNotas.php" in url:
            return _FakeResp(200, {
                "success": True,
                "columnas_subperiodos": ["IB", "IIB"],
                "cursos": [{"Curso": "ÁLGEBRA", "notas_numericas": {"IB": 16.5, "IIB": 15.0}}],
            })
        return _FakeResp(200, {})

    monkeypatch.setattr(ep_cli.requests, "request", fake_req)

    # Escribir en lenguaje natural "quiero consultar mis notas" lanza directo el flujo de alumno con intención notas
    r0 = chat.texto("quiero consultar mis notas")
    assert "DNI" in texto_de(r0)
    chat.texto("78894860")
    chat.boton("alu_conf:si")  # como tenía 1 periodo y pidió notas, va directo a la boleta
    assert any("Boleta de Notas" in texto_de(m) for m in sent if m["type"] == "text")

    # Escribir "quiero subir mi comprobante" lanza directo el flujo de comprobantes
    r_comp = chat.texto("quiero subir mi comprobante de pago")
    assert "DNI del estudiante" in texto_de(r_comp)


def test_rol_callcenter_crm_y_tomar_lead(app, client, sent):
    from app.models import Lead, UsuarioSistema
    with app.app_context():
        u = UsuarioSistema(username="agente1", rol="callcenter", activo=True)
        u.set_password("Clave-Agente-123")
        l = Lead(telefono="51988776655", nombre="Carlos", sede_nombre="Colegio Sede 1", quiere_asesor=True, prioridad="alta")
        db.session.add_all([u, l])
        db.session.commit()
        lid = l.id

    r = login(client, "agente1", "Clave-Agente-123")
    assert r.status_code == 302 and "/admin/leads" in r.headers["Location"]
    assert client.get("/admin/leads").status_code == 200
    assert client.get("/admin/endpoints").status_code == 302  # sin acceso a configuración técnica

    token = csrf_de(client, "/admin/leads")
    client.post(f"/admin/leads/{lid}/tomar", data={"csrf_token": token})
    with app.app_context():
        l2 = db.session.get(Lead, lid)
        assert l2.asignado_a == "agente1" and l2.estado == "contactado"

    # Call center sí puede responder por WhatsApp en /monitor
    r_msg = client.post("/api/send_message/51988776655", json={"mensaje": "Hola Carlos, te llamamos ahora"}, headers={"X-CSRFToken": token})
    assert r_msg.status_code == 200

