"""Tests de Fase 1: Multi-Empresa (/admin/ia), identidad EVO, navegación global y aislamiento de contexto IA."""
from app import asistente, bot_logic
from app.models import Ajuste, Empresa, Usuario, db
from tests.conftest import csrf_de, login


def test_crud_empresas_en_admin_ia(app, client):
    login(client)
    token = csrf_de(client, "/admin/ia")
    r = client.post(
        "/admin/ia/empresas/guardar",
        data={
            "csrf_token": token,
            "nombre_comercial": "Instituto Carrión",
            "clave": "carrion",
            "dominio_web": "https://www.acarrion.edu.pe",
            "horario_atencion": "L-V 08:00 a 19:00",
            "telefono_asesor": "51999111222",
            "cola_asesores": "salud",
            "prompt_sistema": "Enfócate en carreras técnicas de salud.",
            "base_conocimiento": "Carrera de Enfermería Técnica: pensión S/ 480.",
            "activo": "1",
            "orden": "1",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200
    with app.app_context():
        emp = Empresa.query.filter_by(clave="carrion").first()
        assert emp is not None
        assert emp.nombre_comercial == "Instituto Carrión"
        assert emp.dominio_web == "https://www.acarrion.edu.pe"
        assert "480" in emp.base_conocimiento
        emp_id = emp.id

    # Eliminar empresa
    token = csrf_de(client, "/admin/ia")
    r2 = client.post(f"/admin/ia/empresas/{emp_id}/eliminar", data={"csrf_token": token}, follow_redirects=True)
    assert r2.status_code == 200
    with app.app_context():
        assert Empresa.query.filter_by(clave="carrion").first() is None


def test_contexto_aislado_por_empresa(app):
    with app.app_context():
        e1 = Empresa(
            clave="wiener",
            nombre_comercial="Universidad Norbert Wiener",
            base_conocimiento="Medicina Humana: pensión S/ 1900.",
            activo=True,
            orden=1,
        )
        e2 = Empresa(
            clave="carrion",
            nombre_comercial="Instituto Carrión",
            base_conocimiento="Enfermería Técnica: pensión S/ 480.",
            activo=True,
            orden=2,
        )
        db.session.add_all([e1, e2])
        db.session.commit()

        prompt_e1 = asistente._prompt(empresa=e1, es_primer_saludo=True)
        prompt_e2 = asistente._prompt(empresa=e2, es_primer_saludo=True)
        assert "1900" in prompt_e1 and "480" not in prompt_e1
        assert "480" in prompt_e2 and "1900" not in prompt_e2
        assert "PRESENTACIÓN OBLIGATORIA" in prompt_e1


def test_enrutamiento_multi_empresa_pide_seleccion_y_selecciona_boton(app, sent):
    with app.app_context():
        e1 = Empresa(clave="wiener", nombre_comercial="U. Norbert Wiener", base_conocimiento="Info Wiener", activo=True, orden=1)
        e2 = Empresa(clave="carrion", nombre_comercial="Instituto Carrión", base_conocimiento="Info Carrión", activo=True, orden=2)
        db.session.add_all([e1, e2])
        db.session.commit()

        from config import Config
        old_ia = Config.IA_ENABLED
        Config.IA_ENABLED = True
        try:
            # 1. Pregunta ambigua sin mencionar empresa -> pide selección con botones
            bot_logic.brain_gethi("51900111222", "Cuánto cuesta la pensión?", "", "Yared")
            assert any(p.get("type") == "interactive" for p in sent)
            u = Usuario.query.filter_by(telefono="51900111222").first()
            assert u.empresa_id is None

            # 2. Usuario pulsa botón sel_empresa:carrion -> queda vinculado a Carrión
            sent.clear()
            bot_logic.brain_gethi("51900111222", "Botón: Instituto Carrión", "sel_empresa:carrion", "Yared", tipo="interactive")
            u = Usuario.query.filter_by(telefono="51900111222").first()
            assert u.empresa_id == e2.id
            assert any("Instituto Carrión" in str(p) for p in sent)
        finally:
            Config.IA_ENABLED = old_ia


def test_navegacion_global_home_y_cmd_main_menu(app, sent):
    with app.app_context():
        from app.flujos import estado as estado_flujo
        estado_flujo.iniciar("51900222333", "asesor", "datos", {"sede": "Colegio"})

        # Palabra clave global "home" limpia flujo y regresa al inicio
        bot_logic.brain_gethi("51900222333", "home", "", "Yared")
        assert estado_flujo.obtener("51900222333") is None
        assert len(sent) >= 1

        # Botón global CMD_MAIN_MENU también limpia flujo y regresa al inicio
        sent.clear()
        estado_flujo.iniciar("51900222333", "asesor", "datos", {"sede": "Colegio"})
        bot_logic.brain_gethi("51900222333", "Botón: Menú Principal", "CMD_MAIN_MENU", "Yared", tipo="interactive")
        assert estado_flujo.obtener("51900222333") is None
        assert len(sent) >= 1


def test_seed_from_ajuste_migra_conocimiento(app):
    with app.app_context():
        Ajuste.guardar("ia_conocimiento", "Texto oficial heredado S/ 500.", "admin")
        assert Empresa.query.count() == 0
        creado = Empresa.seed_from_ajuste()
        assert creado is True
        emp = Empresa.query.first()
        assert emp is not None
        assert "Texto oficial heredado" in emp.base_conocimiento
