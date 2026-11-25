import logging
import sys
from datetime import timedelta

from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config
from .models import db
from .timeutil import to_local

csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)


def create_app(validate_config=True):
    if validate_config:
        Config.validate()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],  # Docker recoge stdout
        force=True,
    )

    Config.aplicar_ia()

    app = Flask(__name__)
    app.config.from_object(Config)
    app.secret_key = Config.SECRET_KEY
    app.config.update(
        SESSION_COOKIE_NAME='bot_session',
        SESSION_COOKIE_SECURE=Config.SESSION_COOKIE_SECURE,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=Config.SESSION_LIFETIME_MINUTES),
        RATELIMIT_STORAGE_URI=Config.RATELIMIT_STORAGE_URI,
        RATELIMIT_DEFAULT=Config.RATELIMIT_DEFAULT,
    )

    # Detrás de nginx: respeta X-Forwarded-For / -Proto / -Host (IP real y HTTPS correcto)
    if Config.TRUSTED_PROXIES > 0:
        n = Config.TRUSTED_PROXIES
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)

    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    app.add_template_filter(to_local, 'localtime')

    @app.context_processor
    def inject_brand():
        return {'brand': {'bot_name': Config.BOT_NAME, 'company': Config.COMPANY_NAME,
                          'bot_sender': Config.bot_sender(), 'id_label': Config.ID_LABEL,
                          'require_id_validation': Config.REQUIRE_ID_VALIDATION}}

    @app.context_processor
    def inject_pendientes():
        from flask import session
        if session.get('user_role') in ('admin', 'contabilidad'):
            try:
                from .models import Comprobante
                return {'pendientes_comprobantes': Comprobante.query.filter_by(estado='pendiente').count()}
            except Exception:
                db.session.rollback()
        return {'pendientes_comprobantes': 0}

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'DENY')
        resp.headers.setdefault('Referrer-Policy', 'same-origin')
        resp.headers.setdefault('Cache-Control', 'no-store')
        if Config.SESSION_COOKIE_SECURE:
            resp.headers.setdefault('Strict-Transport-Security', 'max-age=31536000')
        if Config.CONTENT_SECURITY_POLICY:
            resp.headers.setdefault('Content-Security-Policy', Config.CONTENT_SECURITY_POLICY)
        return resp

    from .routes import main
    app.register_blueprint(main)
    return app
