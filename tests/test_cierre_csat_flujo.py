"""Tests del flujo de cierre por inactividad en 3 etapas con encuesta Sí/No (estilo banca)."""
from datetime import timedelta

from app import bot_logic, timers
from app.models import Empresa, Lead, Usuario, db, utcnow


def test_cierre_inactividad_3_etapas_sin_respuesta_envia_despedida(app, sent):
    with app.app_context():
        emp = Empresa(clave="comas", nombre_comercial="Colegio Sede Comas", activo=True)
        db.session.add(emp)
        db.session.commit()

        ahora = utcnow()
        u = Usuario(
            telefono="51911220001",
            nombre_wa="Yared",
            empresa_id=emp.id,
            bot_pausado=True,
            estado_chat="atendido",
            esperando_usuario_desde=ahora - timedelta(minutes=4),
            aviso_inactividad_enviado=False,
        )
        db.session.add(u)
        db.session.commit()

        # Etapa 1: Aviso "¿Sigues ahí?"
        s1 = timers.ejecutar_ciclo_timers(ahora=ahora)
        assert s1["avisos_inactividad"] == 1
        assert any("¿Sigues ahí?" in str(m) for m in sent)

        # Etapa 2 (+3 min): Cierre con botones "¿Logramos resolver tu consulta? [Sí / No]"
        sent.clear()
        s2 = timers.ejecutar_ciclo_timers(ahora=ahora + timedelta(minutes=3))
        assert s2["cierres_inactividad"] == 1
        assert any("Logramos resolver tu consulta" in str(m) and "resuelto:si" in str(m) and "resuelto:no" in str(m) for m in sent)

        # Etapa 3 (+2 min más sin tocar botón): Despedida final con nombre de empresa
        sent.clear()
        s3 = timers.ejecutar_ciclo_timers(ahora=ahora + timedelta(minutes=5))
        assert s3["despedidas_finales"] == 1
        assert any("¡Gracias por contactarte con Colegio Sede Comas!" in str(m) for m in sent)


def test_encuesta_cierre_boton_si_y_boton_no(app, sent):
    with app.app_context():
        emp = Empresa(clave="comas", nombre_comercial="Colegio Sede Comas", activo=True)
        db.session.add(emp)
        db.session.commit()

        u = Usuario(telefono="51911220002", nombre_wa="Ana", empresa_id=emp.id, aviso_inactividad_enviado=True, esperando_usuario_desde=utcnow())
        l = Lead(telefono="51911220002", nombre="Ana", empresa_id=emp.id, estado="contactado")
        db.session.add_all([u, l])
        db.session.commit()

        # Botón SÍ -> mensaje positivo + botón Menú Principal + CSAT=5 + cancela despedida final
        bot_logic.brain_gethi("51911220002", "Botón: ✅ Sí", "resuelto:si", "Ana", tipo="interactive")
        assert any("respuesta positiva" in str(m) and "CMD_MAIN_MENU" in str(m) for m in sent)
        assert Lead.query.filter_by(telefono="51911220002").first().csat == 5
        assert Usuario.query.filter_by(telefono="51911220002").first().aviso_inactividad_enviado is False

        # Botón NO -> CSAT=2 y deriva automáticamente al flujo de asesor/cita
        sent.clear()
        bot_logic.brain_gethi("51911220002", "Botón: ❌ No", "resuelto:no", "Ana", tipo="interactive")
        assert Lead.query.filter_by(telefono="51911220002").first().csat == 2
        assert any("asesor" in str(m).lower() for m in sent)
        assert len(sent) >= 2  # mensaje empático + inicio de flujo acc_asesor
