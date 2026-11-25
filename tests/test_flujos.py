"""Flujos guiados: menú de vacantes, consulta de pagos/notas (API simulada) y comprobantes."""
import itertools
import os
from datetime import timedelta

import pytest
import requests

import app.api_academica as api
import app.flujos.comprobante as comp_mod
from app.models import AuditoriaConsulta, Comprobante, Conversacion, Mensaje, Usuario, UsuarioSistema, db, utcnow
from app.services import MediaError
from config import Config
from conftest import csrf_de, ids_de, login, payload_texto, texto_de

TEL = "51900000001"
_n = itertools.count(1)
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture()
def chat(post_wa, sent):
    """Devuelve funciones para 'escribir' como un usuario y leer la última respuesta del bot."""
    class Chat:
        tel = TEL

        def texto(self, t, tel=None):
            post_wa(payload_texto(tel or self.tel, t, f"c{next(_n)}"))
            return sent[-1]

        def boton(self, id_, titulo="x", tel=None):
            post_wa(payload_texto(tel or self.tel, titulo, f"c{next(_n)}", tipo="interactive", btn=id_))
            return sent[-1]

        def lista(self, id_, titulo="x", tel=None):
            post_wa(payload_texto(tel or self.tel, titulo, f"c{next(_n)}", tipo="list", btn=id_))
            return sent[-1]

        def archivo(self, tipo="image", caption="", tel=None):
            post_wa(payload_texto(tel or self.tel, "", f"c{next(_n)}", tipo=tipo, caption=caption))
            return sent[-1]
    return Chat()


@pytest.fixture()
def mock_api(monkeypatch):
    monkeypatch.setattr(Config, "ACAD_API_MOCK", True)


def todo_el_texto(sent):
    return "\n".join(texto_de(p) for p in sent)


# ----------------------------------------------------------------- menú de vacantes
def test_menu_vacantes_colegio_e_instituto(chat):
    assert ids_de(chat.texto("hola")) == ["m_gest", "m_segu", "m_bene"]
    assert ids_de(chat.boton("m_gest")) == ["v_colegio", "v_instituto", "f_matricula"]
    assert ids_de(chat.boton("v_colegio")) == ["v_col_sede1", "v_col_sede2", "m_gest"]
    r = chat.boton("v_col_sede1")
    assert "Sede 1" in texto_de(r) and "v_colegio" in ids_de(r)
    assert ids_de(chat.boton("v_instituto")) == ["v_inst_inicial", "v_inst_fisica", "v_inst_primaria"]
    assert "Educación Física" in texto_de(chat.boton("v_inst_fisica"))
    assert ids_de(chat.boton("m_segu")) == ["acc_alumno", "comp_enviar", "ir_inicio"]


# ----------------------------------------------------------------- consulta de pagos
def test_consulta_deudas_flujo_completo(app, chat, sent, mock_api):
    chat.boton("acc_deudas")
    lista_anios = sent[-1]
    assert lista_anios["interactive"]["type"] == "list" and ids_de(lista_anios) == ["anio:2026", "anio:2025"]

    r = chat.lista("anio:2026", "2026")
    assert ids_de(r) == ["periodo:2026-I", "periodo:2026-II"]
    assert "DNI del estudiante" in texto_de(chat.lista("periodo:2026-I", "2026-I"))
    assert "fecha de nacimiento" in texto_de(chat.texto("12345678"))

    chat.texto("20/05/2010")
    reporte = [texto_de(p) for p in sent if p["type"] == "text" and "Estado de pagos" in texto_de(p)][-1]
    assert "Ana Pérez Quispe" in reporte and "Pensión marzo" in reporte and "350.00" in reporte
    assert "comp_enviar" in ids_de(sent[-1])          # con deuda ofrece enviar comprobante

    with app.app_context():
        # el historial no guarda montos ni notas, ni el DNI completo
        salidas = [m.contenido for m in Mensaje.query.filter_by(telefono=TEL, direccion="SALIDA").all()]
        entradas = [m.contenido for m in Mensaje.query.filter_by(telefono=TEL, direccion="ENTRADA").all()]
        assert "[Reporte de pagos enviado]" in salidas and not any("350" in x for x in salidas)
        assert "DNI ****5678" in entradas and "12345678" not in entradas
        a = AuditoriaConsulta.query.one()
        assert (a.tipo, a.dni_mask, a.anio, a.periodo, a.resultado) == ("deudas", "****5678", "2026", "2026-I", "ok")
        assert Conversacion.query.count() == 0       # el estado se limpia al terminar


def test_deudas_al_dia(chat, sent, mock_api):
    chat.boton("acc_deudas"); chat.lista("anio:2026"); chat.lista("periodo:2026-II")
    chat.texto("11111111"); chat.texto("02/03/2011")
    assert "al día en tus pagos" in todo_el_texto(sent)


def test_fecha_incorrecta_y_dni_inexistente_dan_el_mismo_mensaje(app, chat, sent, mock_api):
    chat.boton("acc_deudas"); chat.lista("anio:2026"); chat.lista("periodo:2026-I")
    chat.texto("12345678")
    r1 = texto_de(chat.texto("01/01/2000"))                   # fecha equivocada
    chat.boton("acc_deudas"); chat.lista("anio:2026"); chat.lista("periodo:2026-I")
    chat.texto("99999999")
    r2 = texto_de(chat.texto("01/01/2000"))                   # DNI inexistente
    assert r1 == r2 and "No encontré un estudiante" in r1
    assert "Ana" not in todo_el_texto(sent)                   # nunca se reveló el nombre
    with app.app_context():
        assert AuditoriaConsulta.query.filter_by(resultado="no_coincide").count() == 2


def test_bloqueo_tras_intentos_fallidos(chat, sent, mock_api, monkeypatch):
    monkeypatch.setattr(Config, "ACAD_MAX_ATTEMPTS", 3)
    for _ in range(3):
        chat.boton("acc_deudas"); chat.lista("anio:2026"); chat.lista("periodo:2026-I")
        chat.texto("99999999"); chat.texto("01/01/2000")
    r = chat.boton("acc_deudas")
    assert "máximo de intentos" in texto_de(r)


def test_formato_invalido_no_cuenta_como_intento(app, chat, sent, mock_api):
    chat.boton("acc_deudas"); chat.lista("anio:2026"); chat.lista("periodo:2026-I")
    assert "8 o 9 dígitos" in texto_de(chat.texto("123"))
    chat.texto("12345678")
    assert "DD/MM/AAAA" in texto_de(chat.texto("ayer"))
    with app.app_context():
        assert Usuario.query.filter_by(telefono=TEL).first().intentos_acad == 0


def test_cancelar_y_menu_cortan_el_flujo(app, chat, sent, mock_api):
    chat.boton("acc_deudas"); chat.lista("anio:2026")
    assert ids_de(chat.texto("cancelar")) == ["m_gest", "m_segu", "m_bene"]
    with app.app_context():
        assert Conversacion.query.count() == 0
    chat.boton("acc_notas")
    assert ids_de(chat.boton("ir_inicio")) == ["m_gest", "m_segu", "m_bene"]


def test_flujo_expirado_no_interpreta_el_dni(app, chat, sent, mock_api):
    chat.boton("acc_deudas"); chat.lista("anio:2026"); chat.lista("periodo:2026-I")
    with app.app_context():
        c = Conversacion.query.one()
        c.expira_en = utcnow() - timedelta(minutes=1)
        db.session.commit()
    assert "No logré entender" in texto_de(chat.texto("12345678"))


# ----------------------------------------------------------------- notas
def test_notas_bloqueadas_si_hay_deuda(app, chat, sent, mock_api):
    chat.boton("acc_notas"); chat.lista("anio:2026"); chat.lista("periodo:2026-I")
    chat.texto("12345678")
    r = chat.texto("20/05/2010")
    assert "al día en tus pagos" in texto_de(r) and "S/ 350.00" in texto_de(r)
    assert "comp_enviar" in ids_de(r)
    assert "Matemática" not in todo_el_texto(sent)
    with app.app_context():
        assert AuditoriaConsulta.query.one().resultado == "pagos_pendientes"


def test_notas_al_dia(chat, sent, mock_api):
    chat.boton("acc_notas"); chat.lista("anio:2026"); chat.lista("periodo:2026-I")
    chat.texto("11111111"); chat.texto("02/03/2011")
    t = todo_el_texto(sent)
    assert "Matemática: *16*" in t and "Promedio: 15.5" in t


def test_notas_sin_exigir_pagos(chat, sent, mock_api, monkeypatch):
    monkeypatch.setattr(Config, "ACAD_NOTAS_REQUIERE_PAGOS_AL_DIA", False)
    chat.boton("acc_notas"); chat.lista("anio:2026"); chat.lista("periodo:2026-I")
    chat.texto("12345678"); chat.texto("20/05/2010")
    assert "Matemática: *16*" in todo_el_texto(sent)


# ----------------------------------------------------------------- verificación
def test_verificacion_por_telefono(chat, sent, mock_api, monkeypatch):
    monkeypatch.setattr(Config, "ACAD_VERIFY_MODE", "telefono")
    chat.boton("acc_deudas"); chat.lista("anio:2026"); chat.lista("periodo:2026-I")
    chat.texto("12345678")                                    # el teléfono 51900000001 es el registrado
    assert "Ana Pérez Quispe" in todo_el_texto(sent)
    sent.clear()
    otro = "51977777777"
    chat.boton("acc_deudas", tel=otro); chat.lista("anio:2026", tel=otro); chat.lista("periodo:2026-I", tel=otro)
    r = chat.texto("12345678", tel=otro)                      # otro teléfono: denegado
    assert "No encontré un estudiante" in texto_de(r) and "Ana" not in todo_el_texto(sent)


def test_sin_segundo_control_oculta_el_nombre(chat, sent, mock_api, monkeypatch):
    monkeypatch.setattr(Config, "ACAD_VERIFY_MODE", "none")
    chat.boton("acc_deudas"); chat.lista("anio:2026"); chat.lista("periodo:2026-I")
    chat.texto("12345678")
    t = todo_el_texto(sent)
    assert "A*** P*** Q***" in t and "Ana" not in t


def test_si_la_api_no_trae_el_dato_de_verificacion_se_deniega(chat, sent, monkeypatch):
    monkeypatch.setattr(api, "consultar_deudas", lambda *a: {
        "estudiante": {"nombre": "X", "fecha_nacimiento": None, "telefonos": []}, "deudas": [], "total_pendiente": 0.0, "moneda": "S/"})
    monkeypatch.setattr(api, "listar_anios", lambda: ["2026"])
    monkeypatch.setattr(api, "listar_periodos", lambda a: ["2026-I"])
    chat.boton("acc_deudas"); chat.lista("anio:2026"); chat.lista("periodo:2026-I")
    chat.texto("12345678")
    assert "No encontré un estudiante" in texto_de(chat.texto("01/01/2000"))


# ----------------------------------------------------------------- cliente HTTP real (simulado)
class _R:
    def __init__(self, code=200, data=None):
        self.status_code, self._d = code, data

    def json(self):
        if self._d is None:
            raise ValueError
        return self._d


def test_cliente_envia_credenciales_y_parametros(monkeypatch):
    visto = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        visto.update(url=url, params=params, headers=headers, timeout=timeout)
        return _R(200, {"estudiante": {"nombre": "Z", "fecha_nacimiento": "2010-01-01"}, "deudas": [
            {"concepto": "Pensión", "monto": "100.5", "vencimiento": "2026-03-01", "estado": "Pendiente"}]})

    monkeypatch.setattr(api.requests, "get", fake_get)
    monkeypatch.setattr(Config, "ACAD_API_URL", "https://api.colegio.test/v1")
    monkeypatch.setattr(Config, "ACAD_API_TOKEN", "secreto")
    d = api.consultar_deudas("2026", "2026-I", "12345678")
    assert visto["url"] == "https://api.colegio.test/v1/deudas"
    assert visto["params"] == {"anio": "2026", "periodo": "2026-I", "dni": "12345678"}
    assert visto["headers"]["Authorization"] == "Bearer secreto"
    assert d["total_pendiente"] == 100.5      # calculado cuando la API no envía el total


@pytest.mark.parametrize("respuesta,esperado", [
    (_R(404), api.NoEncontrado), (_R(401), api.ApiNoDisponible), (_R(500), api.ApiNoDisponible),
    (_R(200, None), api.ApiNoDisponible)])
def test_cliente_errores_http(monkeypatch, respuesta, esperado):
    monkeypatch.setattr(api.requests, "get", lambda *a, **k: respuesta)
    monkeypatch.setattr(Config, "ACAD_API_URL", "https://api.colegio.test")
    with pytest.raises(esperado):
        api.consultar_notas("2026", "2026-I", "12345678")


def test_api_caida_muestra_mensaje_amable(app, chat, sent, monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectTimeout("timeout")
    monkeypatch.setattr(api.requests, "get", boom)
    monkeypatch.setattr(Config, "ACAD_API_URL", "https://api.colegio.test")
    monkeypatch.setattr(Config, "ACAD_ANIOS_FIJOS", ["2026"])
    monkeypatch.setattr(Config, "ACAD_PERIODOS_FIJOS", ["I"])
    chat.boton("acc_deudas"); chat.lista("anio:2026")
    assert ids_de(sent[-1]) == ["periodo:2026-I"]              # años/periodos fijos no necesitan la API
    chat.lista("periodo:2026-I"); chat.texto("12345678")
    assert "no está disponible" in texto_de(chat.texto("20/05/2010"))
    with app.app_context():
        assert AuditoriaConsulta.query.one().resultado == "error"


# ----------------------------------------------------------------- comprobantes
@pytest.fixture()
def carpeta(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "COMPROBANTES_DIR", str(tmp_path / "comp"))
    monkeypatch.setattr(Config, "COMPROBANTE_ALMACEN", "disco")
    return tmp_path / "comp"


def test_comprobante_flujo_completo(app, chat, sent, carpeta, monkeypatch):
    monkeypatch.setattr(comp_mod, "descargar_media", lambda mid, mx: (PNG, "image/png"))
    assert "DNI del estudiante" in texto_de(chat.boton("comp_enviar"))
    assert "foto o el PDF" in texto_de(chat.texto("12345678"))
    r = chat.archivo("image", caption="Pensión marzo S/ 350")
    assert "CP-000001" in texto_de(r)
    with app.app_context():
        c = Comprobante.query.one()
        assert (c.dni_estudiante, c.telefono, c.estado, c.nota, c.mime) == ("12345678", TEL, "pendiente", "Pensión marzo S/ 350", "image/png")
        assert not c.duplicado
        assert (carpeta / c.archivo).read_bytes() == PNG
        assert oct((carpeta / c.archivo).stat().st_mode & 0o777) == "0o600"
        assert Conversacion.query.count() == 0
    # mismo archivo otra vez -> marcado como duplicado
    chat.boton("comp_enviar"); chat.texto("12345678"); chat.archivo("image")
    with app.app_context():
        assert Comprobante.query.filter_by(duplicado=True).count() == 1


def test_comprobante_rechaza_formato_y_tamano(app, chat, sent, carpeta, monkeypatch):
    chat.boton("comp_enviar"); chat.texto("12345678")
    monkeypatch.setattr(comp_mod, "descargar_media", lambda mid, mx: (b"GIF89a" + b"0" * 20, "image/gif"))
    assert "Formato no admitido" in texto_de(chat.archivo("image"))
    monkeypatch.setattr(comp_mod, "descargar_media", lambda mid, mx: (b"MZ\x90\x00" + b"0" * 20, "application/pdf"))
    assert "Formato no admitido" in texto_de(chat.archivo("document"))   # ejecutable disfrazado de PDF

    def grande(mid, mx):
        raise MediaError("Archivo demasiado grande")
    monkeypatch.setattr(comp_mod, "descargar_media", grande)
    assert "No pude recibir el archivo" in texto_de(chat.archivo("image"))
    with app.app_context():
        assert Comprobante.query.count() == 0
        assert Conversacion.query.count() == 1                       # sigue esperando un archivo válido


def test_comprobante_pide_archivo_si_escriben_texto(chat, sent):
    chat.boton("comp_enviar"); chat.texto("12345678")
    assert "foto o el PDF" in texto_de(chat.texto("ya pagué"))


# ----------------------------------------------------------------- panel de contabilidad
def _crear_usuario(app, nombre, rol, clave="Clave-Larga-12345"):
    with app.app_context():
        u = UsuarioSistema(username=nombre, rol=rol, activo=True)
        u.set_password(clave)
        db.session.add(u)
        db.session.commit()


def _sembrar_comprobante(app, carpeta, tel="51955555555"):
    os.makedirs(carpeta, exist_ok=True)
    (carpeta / "abc.png").write_bytes(PNG)
    with app.app_context():
        c = Comprobante(telefono=tel, nombre_wa="Padre", dni_estudiante="12345678", archivo="abc.png",
                        mime="image/png", tamano=len(PNG), sha256="x", nota="n")
        db.session.add(c)
        db.session.commit()
        return c.id


def test_contabilidad_ve_solo_comprobantes(app, client, carpeta):
    _crear_usuario(app, "conta", "contabilidad")
    r = login(client, "conta", "Clave-Larga-12345")
    assert r.headers["Location"].endswith("/admin/comprobantes")
    assert client.get("/admin/comprobantes").status_code == 200
    for ruta in ("/monitor", "/dashboard", "/admin/editor", "/admin/usuarios", "/admin/colaboradores"):
        assert client.get(ruta).status_code == 302, ruta            # redirige: sin acceso
    assert client.get("/api/chat_updates").status_code == 403


def test_lector_no_accede_a_comprobantes(app, client, carpeta):
    _crear_usuario(app, "lec", "lector")
    login(client, "lec", "Clave-Larga-12345")
    cid = _sembrar_comprobante(app, carpeta)
    assert client.get("/admin/comprobantes").status_code == 302
    assert client.get(f"/admin/comprobantes/{cid}/archivo").status_code == 302


def test_revisar_aprueba_notifica_y_rechaza_con_motivo(app, client, carpeta, sent):
    cid = _sembrar_comprobante(app, carpeta)
    login(client)
    token = csrf_de(client, "/admin/comprobantes")
    ruta = f"/admin/comprobantes/{cid}/revisar"

    # el archivo se sirve con su tipo real
    r = client.get(f"/admin/comprobantes/{cid}/archivo")
    assert r.status_code == 200 and r.mimetype == "image/png" and r.data == PNG

    # rechazo sin motivo: no cambia nada ni escribe al usuario
    client.post(ruta, data={"csrf_token": token, "accion": "rechazar", "motivo": ""})
    with app.app_context():
        assert db.session.get(Comprobante, cid).estado == "pendiente"
    assert sent == []

    # rechazo con motivo -> notifica
    client.post(ruta, data={"csrf_token": token, "accion": "rechazar", "motivo": "Monto ilegible"})
    assert "Monto ilegible" in texto_de(sent[-1]) and sent[-1]["to"] == "51955555555"
    with app.app_context():
        c = db.session.get(Comprobante, cid)
        assert (c.estado, c.motivo, c.revisado_por) == ("rechazado", "Monto ilegible", "admin")

    # aprobación posterior
    client.post(ruta, data={"csrf_token": token, "accion": "aprobar"})
    assert "fue validado" in texto_de(sent[-1])
    with app.app_context():
        assert db.session.get(Comprobante, cid).estado == "aprobado"


def test_revisar_exige_csrf_y_no_permite_redireccion_externa(app, client, carpeta, sent):
    cid = _sembrar_comprobante(app, carpeta)
    login(client)
    assert client.post(f"/admin/comprobantes/{cid}/revisar", data={"accion": "aprobar"}).status_code == 400
    token = csrf_de(client, "/admin/comprobantes")
    r = client.post(f"/admin/comprobantes/{cid}/revisar",
                    data={"csrf_token": token, "accion": "aprobar", "next": "https://evil.example/x"})
    assert r.headers["Location"].endswith("/admin/comprobantes")


def test_archivo_inexistente_devuelve_404(app, client, carpeta):
    cid = _sembrar_comprobante(app, carpeta)
    (carpeta / "abc.png").unlink()
    login(client)
    assert client.get(f"/admin/comprobantes/{cid}/archivo").status_code == 404


def test_comprobante_se_guarda_en_la_base_de_datos(app, client, chat, sent, tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "COMPROBANTES_DIR", str(tmp_path / "comp"))
    monkeypatch.setattr(Config, "COMPROBANTE_ALMACEN", "bd")
    monkeypatch.setattr(comp_mod, "descargar_media", lambda mid, mx: (PNG, "image/png"))
    chat.boton("comp_enviar"); chat.texto("12345678")
    assert "CP-000001" in texto_de(chat.archivo("image", caption="Pensión abril"))
    assert not (tmp_path / "comp").exists()                     # nada en disco
    with app.app_context():
        c = Comprobante.query.one()
        assert c.contenido == PNG and c.sha256 and c.estado == "pendiente"
        cid = c.id
    login(client)
    r = client.get(f"/admin/comprobantes/{cid}/archivo")       # contabilidad lo ve desde la BD
    assert r.status_code == 200 and r.data == PNG and r.mimetype == "image/png"
    assert client.get("/admin/comprobantes").status_code == 200   # el listado no carga los bytes
