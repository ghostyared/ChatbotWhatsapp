"""Asistente de ventas (Gemini simulado) + catálogo de sedes (modo demo) + interesados."""
import itertools

import pytest

import app.asistente as asistente
import app.catalogo_api as cat
import app.ia_gemini as gem
from app.models import IaUso, Lead, Usuario, db
from config import Config
from conftest import csrf_de, login, payload_texto, texto_de

TEL = "51900000077"
_n = itertools.count(1)


@pytest.fixture()
def ia(monkeypatch):
    monkeypatch.setattr(Config, "IA_ENABLED", True)
    monkeypatch.setattr(Config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(Config, "CAT_API_MOCK", True)
    monkeypatch.setattr(Config, "ASESOR_TELEFONO", "51999000111")
    guion = {"respuestas": [], "vistas": []}

    def fake(system, contents, tools=None, temperatura=0.4):
        guion["vistas"].append((system, list(contents)))
        return guion["respuestas"].pop(0), 10

    monkeypatch.setattr(gem, "generar", fake)
    return guion


def modelo_texto(t):
    return {"role": "model", "parts": [{"text": t}]}


def modelo_llama(_fn, **args):
    return {"role": "model", "parts": [{"functionCall": {"name": _fn, "args": args}}]}


def escribir(post_wa, t, tel=TEL):
    post_wa(payload_texto(tel, t, f"w{next(_n)}"))


def test_ia_apagada_usa_mensaje_por_defecto(post_wa, sent):
    escribir(post_wa, "quiero precios")
    assert "No logré entender" in texto_de(sent[-1])


def test_ia_responde_texto_libre(ia, post_wa, sent):
    ia["respuestas"] = [modelo_texto("¡Hola! ¿Para qué nivel buscas vacante?")]
    escribir(post_wa, "buenas, busco colegio para mi hijo")
    assert "¿Para qué nivel" in texto_de(sent[-1])


def test_ia_responde_consulta_aunque_empiece_con_hola_o_info(ia, post_wa, sent):
    ia["respuestas"] = [modelo_texto("Tenemos 2 sedes de colegio y el instituto pedagógico.")]
    escribir(post_wa, "Hola, quiero info de precios del colegio")
    assert "2 sedes" in texto_de(sent[-1])
    # Pero un saludo puro ("hola") sigue abriendo el menú
    escribir(post_wa, "hola")
    assert sent[-1]["type"] == "interactive"


def test_ia_sin_catalogo_usa_conocimiento_oficial(ia, post_wa, sent, monkeypatch, client):
    monkeypatch.setattr(Config, "CAT_API_MOCK", False)
    monkeypatch.setattr(Config, "CAT_API_URL", "")
    login(client)
    token = csrf_de(client, "/admin/ia")
    client.post("/admin/ia/ajustes", data={"conocimiento": "Sede Surco: pensión S/ 550.", "csrf_token": token})
    ia["respuestas"] = [modelo_texto("En Sede Surco la pensión es S/ 550.")]
    escribir(post_wa, "cuánto cuesta la pensión en surco")
    assert "550" in texto_de(sent[-1])
    assert "ÚNICAMENTE la INFORMACIÓN" in ia["vistas"][0][0]


def test_ia_consulta_catalogo_y_responde(ia, post_wa, sent):
    ia["respuestas"] = [modelo_llama("consultar_oferta", sede_id="2", nivel="Inicial"), modelo_texto("Inicial 5 años: pensión S/ 480.")]
    escribir(post_wa, "cuánto cuesta inicial en san miguel")
    assert "480" in texto_de(sent[-1])
    # la 2.ª llamada recibió el resultado real del catálogo
    resp = ia["vistas"][1][1][-1]["parts"][0]["functionResponse"]["response"]["resultado"]
    assert resp["ofertas"][0]["pension"] == 480.0 and resp["ofertas"][0]["vacantes"] == 6


def test_ia_registra_interesado_avisa_asesor_y_pausa_bot(ia, post_wa, sent, app):
    ia["respuestas"] = [
        modelo_llama("registrar_interesado", nombre="María Pérez", sede_id="1", nivel="Inicial", edad_hijo="5",
                     visita_preferida="sábado 10am", quiere_asesor=True, comentario="busca turno mañana"),
        modelo_texto("¡Listo María! Te esperamos el sábado."),
    ]
    escribir(post_wa, "quiero visitar el sábado y hablar con alguien")
    with app.app_context():
        lead = Lead.query.one()
        paus = Usuario.query.filter_by(telefono=TEL).one().bot_pausado
    assert (lead.nombre, lead.sede_nombre, lead.estado, lead.asesor_avisado) == ("María Pérez", "Sede Norte", "visita", True)
    avisos = [m for m in sent if m.get("to") == "51999000111"]
    assert avisos and "María Pérez" in avisos[0]["text"]["body"]
    assert paus is True
    n = len(sent)
    escribir(post_wa, "hola?")           # el bot pausado calla
    assert len(sent) == n


def test_registrar_dos_veces_no_duplica(ia, post_wa, sent, app):
    for _ in range(2):
        ia["respuestas"] = [modelo_llama("registrar_interesado", nombre="Luis", sede_id="2"), modelo_texto("ok")]
        escribir(post_wa, "me interesa")
    with app.app_context():
        assert Lead.query.count() == 1


def test_ia_no_recibe_numeros_largos(ia, post_wa, sent):
    ia["respuestas"] = [modelo_texto("Para eso usa el menú.")]
    escribir(post_wa, "mi dni es 12345678 y quiero mis notas")
    contenido = str(ia["vistas"][0][1])
    assert "12345678" not in contenido and "[dato oculto]" in contenido


def test_ia_menu_alumno(ia, post_wa, sent):
    ia["respuestas"] = [modelo_llama("mostrar_menu_alumno"), modelo_texto("Usa este menú seguro 👇")]
    escribir(post_wa, "quiero ver mis pagos")
    assert sent[-1]["type"] == "interactive"


def test_falla_ia_cae_al_menu(ia, post_wa, sent, monkeypatch):
    def roto(*a, **k):
        raise gem.IaError("caída")
    monkeypatch.setattr(gem, "generar", roto)
    escribir(post_wa, "hola necesito info de precios")
    assert sent  # respondió algo (menú o mensaje por defecto), no se quedó mudo


def test_falla_catalogo_no_inventa(ia, post_wa, sent, monkeypatch):
    def roto(*a, **k):
        raise cat.CatalogoError("caído")
    monkeypatch.setattr(cat, "consultar_oferta", roto)
    ia["respuestas"] = [modelo_llama("consultar_oferta", sede_id="1"), modelo_texto("No puedo ver precios ahora; te paso con un asesor.")]
    escribir(post_wa, "precios sede norte")
    r = ia["vistas"][1][1][-1]["parts"][0]["functionResponse"]["response"]["resultado"]
    assert "error" in r


def test_tope_por_telefono(ia, post_wa, sent, monkeypatch, app):
    monkeypatch.setattr(Config, "IA_MAX_MSG_DIA_TEL", 2)
    for i in range(3):
        ia["respuestas"] = [modelo_texto(f"respuesta {i}")]
        escribir(post_wa, f"pregunta {i}")
    assert "respuesta 1" in str([texto_de(m) for m in sent])
    assert "respuesta 2" not in str([texto_de(m) for m in sent])
    with app.app_context():
        assert IaUso.query.one().mensajes == 2


def test_catalogo_normaliza_campos_alternativos(monkeypatch):
    monkeypatch.setattr(Config, "CAT_API_MOCK", False)
    monkeypatch.setattr(Config, "CAT_API_URL", "http://x")
    def fake(path, params=None):
        return {"sede": {"codigo": 9, "name": "Sur", "whatsapp": "+51 999-111-222"},
                "ofertas": [{"nivel": "Primaria", "grado": "2.º", "mensualidad": "310,50", "cupos": "4"}]}
    monkeypatch.setattr(cat, "_get", fake)
    sede, ofertas = cat.consultar_oferta("9", "prim")
    assert sede["id"] == "9" and sede["telefono_asesor"] == "51999111222"
    assert ofertas[0]["pension"] == 310.5 and ofertas[0]["vacantes"] == 4


def test_ia_sin_clave_se_apaga_pero_el_bot_arranca(monkeypatch):
    monkeypatch.setattr(Config, "IA_ENABLED", True)
    monkeypatch.setattr(Config, "IA_PROVIDER", "gemini")
    monkeypatch.setattr(Config, "GEMINI_API_KEY", "")
    Config.validate()                      # ya no lanza: la IA es opcional
    Config.aplicar_ia()
    assert Config.IA_ENABLED is False and "gemini" in Config.IA_AVISO


def test_panel_interesados(ia, client, post_wa, sent, app):
    ia["respuestas"] = [modelo_llama("registrar_interesado", nombre="Rosa", sede_id="1"), modelo_texto("ok")]
    escribir(post_wa, "me interesa")
    login(client)
    html = client.get("/admin/leads").get_data(as_text=True)
    assert "Rosa" in html and "Sede Norte" in html
    token = csrf_de(client, "/admin/leads")
    with app.app_context():
        lid = Lead.query.one().id
    client.post(f"/admin/leads/{lid}/actualizar", data={"estado": "contactado", "notas": "llamado", "csrf_token": token})
    with app.app_context():
        assert Lead.query.one().estado == "contactado"
    assert client.get("/admin/leads?estado=contactado").status_code == 200


# ---------------- protecciones contra respuestas erradas ----------------
def test_verificar_rechaza_precio_inventado():
    ev = '{"ofertas":[{"pension":480.0,"vacantes":6}]}'
    assert asistente.verificar("La pensión es S/ 480 y hay 6 vacantes.", ev) == []
    assert asistente.verificar("La pensión es S/ 350.", ev)
    assert asistente.verificar("Hay 12 vacantes.", ev)


def test_verificar_promesas_y_competencia(monkeypatch):
    monkeypatch.setattr(Config, "IA_BLOQUEAR_NOMBRES", ["colegio rival"])
    assert asistente.verificar("Tenemos una beca del 50%", "")
    assert asistente.verificar("Mejor mira el Colegio Rival", "")
    monkeypatch.setattr(Config, "IA_INSTRUCCIONES_EXTRA", "Beca por excelencia: S/ 100 menos")
    assert asistente.verificar("Hay una beca por excelencia", "") == []


def test_precio_inventado_se_corrige_o_se_reemplaza(ia, post_wa, sent):
    ia["respuestas"] = [modelo_llama("consultar_oferta", sede_id="2", nivel="Inicial"),
                        modelo_texto("Cuesta S/ 999 al mes."),          # inventado -> rechazado
                        modelo_texto("La pensión de Inicial es S/ 480.")]  # corregido
    escribir(post_wa, "precio inicial san miguel")
    assert "480" in texto_de(sent[-1]) and "999" not in str([texto_de(m) for m in sent])


def test_si_insiste_en_inventar_se_envia_mensaje_seguro(ia, post_wa, sent):
    ia["respuestas"] = [modelo_texto("Cuesta S/ 999."), modelo_texto("Te hago descuento, S/ 888.")]
    escribir(post_wa, "cuánto cuesta")
    assert texto_de(sent[-1]) == Config.IA_MENSAJE_SEGURO


# ---------------- OpenRouter, respaldo y panel de ajustes ----------------
import app.ia_openrouter as orr
import app.ia as ia_router


class _R:
    def __init__(self, code, data):
        self.status_code, self._d, self.text = code, data, str(data)

    def json(self):
        return self._d


def test_openrouter_convierte_mensajes_y_herramientas(monkeypatch):
    visto = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        visto["body"], visto["headers"] = json, headers
        return _R(200, {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "x", "type": "function", "function": {"name": "consultar_oferta", "arguments": '{"sede_id": "2"}'}}]}}],
            "usage": {"total_tokens": 42}})
    monkeypatch.setattr(orr.requests, "post", fake_post)
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "or-key")
    contenido, tokens = orr.generar("sistema", [
        {"role": "user", "parts": [{"text": "hola"}]},
        {"role": "model", "parts": [{"functionCall": {"name": "listar_sedes", "args": {}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "listar_sedes", "response": {"resultado": {"sedes": []}}}}]},
    ], asistente.HERRAMIENTAS)
    msgs = visto["body"]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"]
    assert msgs[3]["tool_call_id"] == msgs[2]["tool_calls"][0]["id"]
    assert visto["body"]["tools"][0]["type"] == "function"
    assert visto["headers"]["Authorization"] == "Bearer or-key"
    assert contenido["parts"][0]["functionCall"] == {"name": "consultar_oferta", "args": {"sede_id": "2"}} and tokens == 42


def test_openrouter_prueba_siguiente_modelo(monkeypatch):
    modelos = []

    def fake_post(url, json=None, headers=None, timeout=None):
        modelos.append(json["model"])
        return _R(429, "limite") if len(modelos) == 1 else _R(200, {"choices": [{"message": {"content": "hola"}}]})
    monkeypatch.setattr(orr.requests, "post", fake_post)
    monkeypatch.setattr(Config, "OPENROUTER_MODELS", ["a:free", "b:free"])
    contenido, _ = orr.generar("s", [{"role": "user", "parts": [{"text": "x"}]}])
    assert modelos == ["a:free", "b:free"] and contenido["parts"][0]["text"] == "hola"


def test_respaldo_si_falla_el_principal(monkeypatch):
    monkeypatch.setattr(Config, "IA_PROVIDER", "gemini")
    monkeypatch.setattr(Config, "IA_PROVIDER_RESPALDO", "openrouter")

    def roto(*a, **k):
        raise gem.IaError("límite")
    monkeypatch.setattr(gem, "generar", roto)
    monkeypatch.setattr(orr, "generar", lambda *a, **k: ({"role": "model", "parts": [{"text": "desde respaldo"}]}, 1))
    contenido, _ = ia_router.generar("s", [])
    assert contenido["parts"][0]["text"] == "desde respaldo"


def test_respaldo_sin_clave_se_ignora(monkeypatch):
    monkeypatch.setattr(Config, "IA_ENABLED", True)
    monkeypatch.setattr(Config, "IA_PROVIDER", "gemini")
    monkeypatch.setattr(Config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(Config, "IA_PROVIDER_RESPALDO", "openrouter")
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(Config, "CAT_API_MOCK", True)
    Config.aplicar_ia()
    assert Config.IA_ENABLED is True and Config.IA_PROVIDER_RESPALDO == "" and "respaldo" in Config.IA_AVISO


def test_conocimiento_del_panel_llega_a_la_ia_y_valida(ia, client, post_wa, sent, app):
    login(client)
    token = csrf_de(client, "/admin/ia")
    client.post("/admin/ia/ajustes", data={"conocimiento": "Beca por excelencia: 15% de descuento en pensión", "csrf_token": token})
    ia["respuestas"] = [modelo_texto("Tenemos beca por excelencia con 15% de descuento.")]
    escribir(post_wa, "hay becas?")
    assert "Beca por excelencia" in ia["vistas"][0][0]         # entra al prompt
    assert "beca" in texto_de(sent[-1]).lower() and texto_de(sent[-1]) != Config.IA_MENSAJE_SEGURO   # y autoriza la palabra


def test_panel_ia_y_probar_conexion(ia, client, monkeypatch):
    login(client)
    assert "Asistente IA" in client.get("/admin/ia").get_data(as_text=True)
    ia["respuestas"] = [modelo_texto("OK")]
    token = csrf_de(client, "/admin/ia")
    r = client.post("/admin/ia/probar", data={"csrf_token": token}, follow_redirects=True)
    assert "Conexión correcta" in r.get_data(as_text=True)


# ---------------- productos, planes de pago y requisitos ----------------
def test_ia_consulta_planes_y_requisitos_y_los_cita(ia, post_wa, sent):
    ia["respuestas"] = [modelo_llama("consultar_planes_pago", tipo="colegio"),
                        modelo_llama("consultar_requisitos", tipo="colegio"),
                        modelo_texto("Puedes pagar en 10 cuotas de S/ 420. Requisitos: partida de nacimiento y DNI.")]
    escribir(post_wa, "cómo se paga y qué piden para postular al colegio")
    assert "10 cuotas de S/ 420" in texto_de(sent[-1])
    r1 = ia["vistas"][1][1][-1]["parts"][0]["functionResponse"]["response"]["resultado"]
    r2 = ia["vistas"][2][1][-1]["parts"][0]["functionResponse"]["response"]["resultado"]
    assert r1["planes_pago"][0]["monto_cuota"] == 420 and "Partida de nacimiento" in r2["requisitos"]


def test_porcentaje_inventado_se_bloquea():
    ev = '{"planes_pago":[{"nombre":"Pago anual","descuento":"10%"}]}'
    assert asistente.verificar("Con el pago anual tienes 10% de descuento", ev) == []
    assert asistente.verificar("Tienes 25% de descuento", ev)


def test_listar_sedes_filtra_por_producto(monkeypatch):
    monkeypatch.setattr(Config, "CAT_API_MOCK", True)
    assert [x["nombre"] for x in cat.listar_sedes("instituto")] == ["Instituto Pedagógico"]
    assert len(cat.listar_sedes("colegio")) == 2


def test_planes_y_requisitos_dentro_de_la_oferta(monkeypatch):
    monkeypatch.setattr(Config, "CAT_API_MOCK", False)
    monkeypatch.setattr(Config, "CAT_API_URL", "http://x")
    monkeypatch.setattr(cat, "_get", lambda path, params=None: {"sede": {"id": 1, "nombre": "N"}, "ofertas": [
        {"nivel": "Inicial", "planes_pago": [{"nombre": "Mensual"}], "requisitos": ["DNI"]}]})
    _, ofertas = cat.consultar_oferta("1")
    assert ofertas[0]["planes_pago"] == [{"nombre": "Mensual"}] and ofertas[0]["requisitos"] == ["DNI"]
