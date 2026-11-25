"""Tests de Fases 3 y 4: Campañas Facebook/IG Ads (referral), Timers SLA/Inactividad, Plantillas /atajos, Whisper, Takeover, HSM y KPIs CSV."""
from datetime import timedelta

from app import bot_logic, timers
from app.models import Empresa, Lead, Mensaje, PlantillaRapida, PreguntaPendiente, ReglaCampana, Usuario, db, utcnow
from tests.conftest import csrf_de, login, payload_texto


def test_atribucion_facebook_ads_zero_friction(app, post_wa, sent):
    with app.app_context():
        emp = Empresa(
            clave="carrion",
            nombre_comercial="Instituto Carrión",
            dominio_web="https://www.acarrion.edu.pe",
            base_conocimiento="Enfermería Técnica: 3 años.",
            activo=True,
        )
        db.session.add(emp)
        db.session.commit()
        regla = ReglaCampana(
            nombre="Campaña Enfermería Marzo",
            source_id="AD_998877",
            palabras_clave="enfermeria,carrion",
            empresa_id=emp.id,
            programa="Enfermería Técnica",
            brochure_url="https://www.acarrion.edu.pe/brochure-enfermeria.pdf",
            brochure_nombre="Brochure_Enfermeria.pdf",
            activo=True,
        )
        db.session.add(regla)
        db.session.commit()
        emp_id = emp.id

    # Simular webhook de Meta con objeto referral de Click-to-WhatsApp Ad
    payload = payload_texto("51966000111", "Hola EVO, vengo de Facebook y quiero info de Enfermería en Carrión", wa_id="wamid.ad1")
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["referral"] = {
        "source_type": "ad",
        "source_id": "AD_998877",
        "headline": "Estudia Enfermería en Carrión",
        "ctwa_clid": "clid_abc123",
    }
    r = post_wa(payload)
    assert r.status_code == 200

    # Verifica respuesta directa (bienvenida + brochure) sin menú de selección de empresa
    assert any(m.get("type") == "interactive" and "Enfermería Técnica" in str(m) for m in sent)
    assert any(m.get("type") == "document" and m["document"]["filename"] == "Brochure_Enfermeria.pdf" for m in sent)

    with app.app_context():
        u = Usuario.query.filter_by(telefono="51966000111").first()
        lead = Lead.query.filter_by(telefono="51966000111").first()
        assert u.empresa_id == emp_id
        assert lead is not None
        assert lead.origen == "facebook_ads"
        assert lead.campana_id == "AD_998877"
        assert lead.ctwa_clid == "clid_abc123"
        assert lead.score == "tibio"


def test_timers_sla_y_cierre_inactividad(app, sent):
    with app.app_context():
        PlantillaRapida.seed()
        emp = Empresa(clave="wiener", nombre_comercial="U. Norbert Wiener", dominio_web="https://www.uwiener.edu.pe", activo=True)
        db.session.add(emp)
        db.session.commit()

        ahora = utcnow()
        # Usuario 1: en cola de asesor con SLA vencido (> 180s)
        u_sla = Usuario(
            telefono="51977000111",
            nombre_wa="Carlos",
            bot_pausado=True,
            estado_chat="en_cola",
            esperando_asesor_desde=ahora - timedelta(seconds=250),
        )
        # Usuario 2: atendido por asesor, sin responder hace 4 min (> 3 min aviso)
        u_inact = Usuario(
            telefono="51977000222",
            nombre_wa="Yared",
            empresa_id=emp.id,
            bot_pausado=True,
            estado_chat="atendido",
            esperando_usuario_desde=ahora - timedelta(minutes=4),
            aviso_inactividad_enviado=False,
        )
        db.session.add_all([u_sla, u_inact])
        db.session.commit()

        stats1 = timers.ejecutar_ciclo_timers(ahora=ahora)
        assert stats1["sla_retomados"] == 1
        assert stats1["avisos_inactividad"] == 1
        assert stats1["cierres_inactividad"] == 0
        assert any("horario podemos llamarte" in str(m).lower() for m in sent)
        assert any("2 minutos más" in str(m) for m in sent)

        # +3 minutos después: cierra conversación formalmente con {{usuario}} y {{url_empresa}}
        sent.clear()
        stats2 = timers.ejecutar_ciclo_timers(ahora=ahora + timedelta(minutes=3))
        assert stats2["cierres_inactividad"] == 1
        assert any("¡Gracias por escribirnos, Yared!" in str(m) and "https://www.uwiener.edu.pe" in str(m) for m in sent)

        u_inact_ref = Usuario.query.filter_by(telefono="51977000222").first()
        assert u_inact_ref.bot_pausado is False
        assert u_inact_ref.estado_chat == "bot"


def test_plantillas_atajos_whisper_takeover_y_kpis_csv(app, client, sent):
    with app.app_context():
        emp = Empresa(clave="wiener", nombre_comercial="U. Norbert Wiener", dominio_web="https://www.uwiener.edu.pe", activo=True)
        db.session.add(emp)
        db.session.commit()
        u = Usuario(telefono="51988000333", nombre_wa="Yared", empresa_id=emp.id, bot_pausado=True, estado_chat="en_cola")
        lead = Lead(telefono="51988000333", nombre="Yared", origen="facebook_ads", quiere_asesor=True, estado="contactado")
        db.session.add_all([u, lead])
        db.session.commit()
        lid = lead.id
        PreguntaPendiente.registrar("¿Tienen piscina?", empresa_id=emp.id, telefono="51988000333")

    login(client)
    token = csrf_de(client, "/monitor")

    # 1. Plantillas rápidas interpoladas
    r_tpl = client.get("/api/plantillas_rapidas/51988000333")
    assert r_tpl.status_code == 200
    plantillas = {p["atajo"]: p["texto"] for p in r_tpl.get_json()["plantillas"]}
    assert "Yared" in plantillas["/saludo"]
    assert "https://www.uwiener.edu.pe" in plantillas["/cierre"]

    # 2. Modo Susurro (Whisper): se guarda interno y NO se envía a WhatsApp
    sent.clear()
    r_wh = client.post("/api/whisper/51988000333", json={"mensaje": "Ofrécele beca convenio"}, headers={"X-CSRFToken": token})
    assert r_wh.status_code == 201
    assert len(sent) == 0
    with app.app_context():
        m = Mensaje.query.filter_by(telefono="51988000333", es_interno=True).first()
        assert m is not None and "convenio" in m.contenido

    # 3. Takeover (reasignación en 1 clic)
    r_tk = client.post("/api/takeover/51988000333", json={"asignado_a": "piero"}, headers={"X-CSRFToken": token})
    assert r_tk.status_code == 200
    assert r_tk.get_json()["asignado_a"] == "piero"

    # 4. Envío de plantilla HSM a lead
    token_l = csrf_de(client, "/admin/leads")
    r_hsm = client.post(f"/admin/leads/{lid}/enviar_hsm", data={"csrf_token": token_l, "template_name": "promo_matricula_2026"}, follow_redirects=True)
    assert r_hsm.status_code == 200
    assert any(m.get("type") == "template" and m["template"]["name"] == "promo_matricula_2026" for m in sent)

    # 5. Reporte KPIs JSON y CSV delimitado por ';' con BOM UTF-8
    r_kpi = client.get("/api/reportes/kpis")
    assert r_kpi.status_code == 200
    kdata = r_kpi.get_json()
    assert "tasa_contencion_bot_pct" in kdata
    assert "facebook_ads" in kdata["embudo_por_origen"]

    r_csv = client.get("/admin/reportes/kpis.csv")
    assert r_csv.status_code == 200
    csv_txt = r_csv.get_data(as_text=True)
    assert csv_txt.startswith("\ufeff")
    assert "EMBUDO_ORIGEN;facebook_ads;" in csv_txt
    assert "BRECHAS_IA;¿Tienen piscina?;" in csv_txt
