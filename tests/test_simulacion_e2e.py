"""Prueba integral End-to-End con simulación completa de datos CRM (`seed_demo_crm`)."""
import manage
from app.models import Cita, Empresa, Lead, MaterialComercial, PreguntaPendiente, ReglaCampana, db
from tests.conftest import csrf_de, login


def test_simulacion_crm_completa_end_to_end(app, client, sent):
    with app.app_context():
        manage.seed_demo_crm()
        assert Empresa.query.count() >= 3
        assert Lead.query.count() >= 5
        assert Cita.query.count() >= 2
        assert PreguntaPendiente.query.filter_by(estado="pendiente").count() >= 3
        assert ReglaCampana.query.count() >= 2
        assert MaterialComercial.query.count() >= 3

    # 1. Admin verifica todas las pantallas y exportaciones con datos simulados
    login(client)
    for ruta in ("/dashboard", "/monitor", "/monitor/51990001001", "/admin/leads", "/admin/citas", "/admin/ia", "/admin/editor", "/admin/usuarios"):
        r = client.get(ruta)
        assert r.status_code == 200, f"Falló {ruta} con {r.status_code}"

    # 2. KPIs JSON y CSV con métricas reales (FRT, ART, Contención, Embudo Ads, Brechas IA)
    kpis = client.get("/api/reportes/kpis").get_json()
    assert kpis["frt_promedio_seg"] is not None
    assert kpis["art_promedio_seg"] is not None
    assert "facebook_ads" in kpis["embudo_por_origen"]
    assert len(kpis["top_brechas_ia"]) >= 3

    r_kpi_csv = client.get("/admin/reportes/kpis.csv")
    assert r_kpi_csv.status_code == 200 and "EMBUDO_ORIGEN;facebook_ads;" in r_kpi_csv.get_data(as_text=True)

    r_leads_csv = client.get("/admin/leads/exportar.csv")
    assert r_leads_csv.status_code == 200 and "Carla Quispe" in r_leads_csv.get_data(as_text=True)

    # 3. Monitor API devuelve checks, ventana 24h, ficha IA y susurro interno
    upd = client.get("/api/chat_updates/51990001002").get_json()
    assert upd["ventana_24h_activa"] is True
    assert upd["handoff_brief"] is not None
    assert any(m["es_interno"] for m in upd["mensajes"])
    assert any(m["estado_envio"] == "read" for m in upd["mensajes"])

    # 4. Aislamiento de asesor de Comas: solo ve leads y citas de Comas (no Ventanilla ni Instituto)
    client.get("/logout")
    login(client, "asesor_comas", "DemoCRM2026!")
    html_leads_comas = client.get("/admin/leads").get_data(as_text=True)
    assert "Carla Quispe" in html_leads_comas
    assert "Jorge Paredes" not in html_leads_comas
    assert "Milagros Silva" not in html_leads_comas

    # 5. Supervisor ve KPIs y todos los leads, pero no /admin/usuarios
    client.get("/logout")
    login(client, "supervisor_demo", "DemoCRM2026!")
    assert client.get("/api/reportes/kpis").status_code == 200
    assert "Jorge Paredes" in client.get("/admin/leads").get_data(as_text=True)
    assert client.get("/admin/usuarios").status_code == 302
