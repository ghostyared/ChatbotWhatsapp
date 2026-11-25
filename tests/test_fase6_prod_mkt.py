"""Tests de Fase 6 (Producción & Marketing): Checks lectura Meta (statuses), Opt-Out (BAJA), CSAT, Material Multimedia (Imagen/Video/PDF) y Broadcast HSM."""
from app import bot_logic, services
from app.models import Empresa, Lead, MaterialComercial, Mensaje, Usuario, db
from tests.conftest import csrf_de, login


def test_checks_lectura_meta_statuses(app, post_wa):
    with app.app_context():
        db.session.add(Mensaje(
            telefono="51944000111", nombre="EVO 🤖", contenido="Hola", direccion="SALIDA",
            wa_id="wamid.out.123", estado_envio="sent",
        ))
        db.session.commit()

    # Simular webhook de Meta con status = read
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "statuses": [{"id": "wamid.out.123", "status": "read"}]
                }
            }]
        }]
    }
    r = post_wa(payload)
    assert r.status_code == 200
    with app.app_context():
        m = Mensaje.query.filter_by(wa_id="wamid.out.123").first()
        assert m.estado_envio == "read"


def test_opt_out_baja_bloquea_hsm_y_broadcast(app, client, sent):
    with app.app_context():
        l1 = Lead(telefono="51955000111", nombre="Cliente Baja", estado="nuevo", score="tibio")
        l2 = Lead(telefono="51955000222", nombre="Cliente Activo", estado="nuevo", score="tibio")
        db.session.add_all([l1, l2])
        db.session.commit()

        # Cliente 1 escribe "BAJA" -> opt_out=True
        bot_logic.brain_gethi("51955000111", "BAJA", "", "Cliente Baja")
        u1 = Usuario.query.filter_by(telefono="51955000111").first()
        assert u1.opt_out is True

        # Envío directo HSM a opt_out queda bloqueado
        sent.clear()
        assert services.enviar_plantilla("51955000111", "promo_marzo") is None
        assert len(sent) == 0

    # Broadcast segmentado solo envía al cliente activo (omite opt_out)
    login(client)
    token = csrf_de(client, "/admin/leads")
    r = client.post(
        "/admin/leads/broadcast",
        data={"csrf_token": token, "template_name": "promo_marzo", "estado": "nuevo", "score": "tibio"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert len(sent) == 1
    assert sent[0]["to"] == "51955000222"


def test_envio_material_imagen_video_y_csat(app, client, sent):
    with app.app_context():
        emp = Empresa(clave="wiener", nombre_comercial="U. Norbert Wiener", activo=True)
        db.session.add(emp)
        db.session.commit()
        mat = MaterialComercial(
            empresa_id=emp.id,
            titulo="Video Campus Wiener",
            tipo="video",
            url="https://www.uwiener.edu.pe/campus.mp4",
            caption="Conoce nuestro campus principal",
            activo=True,
        )
        lead = Lead(telefono="51966000333", nombre="Ana", estado="contactado")
        db.session.add_all([mat, lead])
        db.session.commit()
        mid = mat.id

    login(client)
    token = csrf_de(client, "/monitor")
    r = client.post(
        "/api/send_media/51966000333",
        json={"material_id": mid},
        headers={"X-CSRFToken": token},
    )
    assert r.status_code == 200
    assert any(m.get("type") == "video" and m["video"]["link"] == "https://www.uwiener.edu.pe/campus.mp4" for m in sent)

    # Cliente responde encuesta CSAT (5 estrellas)
    with app.app_context():
        bot_logic.brain_gethi("51966000333", "Botón: ⭐ Excelente", "csat:5", "Ana", tipo="interactive")
        l = Lead.query.filter_by(telefono="51966000333").first()
        assert l.csat == 5


def test_transcripcion_nota_de_voz_y_palabra_continuar(app, sent, monkeypatch):
    from app import ia_gemini
    monkeypatch.setattr(services, "descargar_media", lambda mid, max_bytes: (b"FAKE_OGG_AUDIO", "audio/ogg"))
    monkeypatch.setattr(ia_gemini, "transcribir_audio", lambda data, mime="audio/ogg": "Continuar")

    with app.app_context():
        # Cliente envía nota de voz diciendo "Continuar" -> se transcribe, se guarda y lanza flujo de asesor
        bot_logic.brain_gethi(
            "51977000444", "[Nota de voz]", "", "Carlos",
            tipo="audio", media={"id": "media_aud_1", "mime": "audio/ogg"},
        )
        m = Mensaje.query.filter_by(telefono="51977000444", media_tipo="audio").first()
        assert m is not None and "Continuar" in m.contenido
        assert len(sent) >= 1

