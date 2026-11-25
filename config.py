"""
Configuración centralizada. TODO se controla por variables de entorno (.env).
No hay credenciales ni valores de ejemplo en el código: si falta una variable
obligatoria, la aplicación se niega a arrancar (ver Config.validate()).
"""
import os
from urllib.parse import quote_plus, urlparse
from dotenv import load_dotenv

try:
    load_dotenv()
except PermissionError:
    # En contenedores montados (p. ej. pytest con -v $PWD:/src), .env puede tener chmod 600 de root;
    # en ese caso las variables llegan por entorno o conftest.py.
    pass


def _host(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _str(name, default=""):
    return (os.environ.get(name, default) or "").strip()


def _bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "si", "sí", "on")


def _int(name, default):
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return default


def _int_list(name, default):
    out = []
    for part in _str(name, default).split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out or [int(x) for x in default.split(",")]


class Config:
    # --- IDENTIDAD DEL BOT / EMPRESA (solo presentación) ---
    BOT_NAME = _str("BOT_NAME", "EVO")
    COMPANY_NAME = _str("COMPANY_NAME", "Mi Empresa")
    APP_TIMEZONE = _str("APP_TIMEZONE", "America/Lima")
    BOT_DEFAULT_GREETING = _str(
        "BOT_DEFAULT_GREETING",
        "¡Hola! Soy {{BOT_NAME}}, tu asistente virtual. ¿Con qué institución deseas comunicarte hoy?",
    )
    HANDOFF_SLA_SECONDS = max(30, _int("HANDOFF_SLA_SECONDS", 180))
    USER_INACTIVITY_WARNING_MIN = max(1, _int("USER_INACTIVITY_WARNING_MIN", 3))
    USER_INACTIVITY_CLOSE_MIN = max(1, _int("USER_INACTIVITY_CLOSE_MIN", 2))
    ATENCION_HORA_INICIO = _int("ATENCION_HORA_INICIO", 8)
    ATENCION_HORA_FIN = _int("ATENCION_HORA_FIN", 18)
    ATENCION_DIAS = _int_list("ATENCION_DIAS", "0,1,2,3,4")   # 0=Lun .. 6=Dom

    # --- WHATSAPP CLOUD API (META) ---
    TOKEN_PERMANENTE = _str("TOKEN_PERMANENTE")
    PHONE_ID = _str("PHONE_ID")
    WEBHOOK_TOKEN = _str("WEBHOOK_TOKEN")      # token de verificación (GET /webhook)
    APP_SECRET = _str("APP_SECRET")            # firma X-Hub-Signature-256 (POST /webhook)
    GRAPH_API_VERSION = _str("GRAPH_API_VERSION", "v22.0")
    URL_API = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_ID}/messages"
    WA_API_TIMEOUT = _int("WA_API_TIMEOUT", 10)          # envío de mensajes a Meta (s)
    WA_MEDIA_TIMEOUT = _int("WA_MEDIA_TIMEOUT", 30)      # descarga de archivos recibidos (s)
    WEBHOOK_WORKERS = max(1, _int("WEBHOOK_WORKERS", 8))  # hilos que procesan mensajes (por worker de Gunicorn)
    WEBHOOK_LOCK_POR_TELEFONO = _bool("WEBHOOK_LOCK_POR_TELEFONO", True)  # procesa en serie los mensajes de un mismo número

    # --- PANEL ADMIN ---
    ADMIN_USER = _str("ADMIN_USER")
    ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
    ADMIN_NAME = _str("ADMIN_NAME", "Administrador Principal")
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    PASSWORD_MIN_LENGTH = _int("PASSWORD_MIN_LENGTH", 10)
    LOGIN_MAX_ATTEMPTS = _int("LOGIN_MAX_ATTEMPTS", 5)
    LOGIN_LOCK_MINUTES = _int("LOGIN_LOCK_MINUTES", 15)

    # --- SESIÓN / HTTP ---
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", True)   # True si hay HTTPS (recomendado)
    SESSION_LIFETIME_MINUTES = _int("SESSION_LIFETIME_MINUTES", 480)
    TRUSTED_PROXIES = _int("TRUSTED_PROXIES", 1)  # nº de proxies inversos delante (nginx = 1)
    RATELIMIT_STORAGE_URI = _str("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_DEFAULT = _str("RATELIMIT_DEFAULT", "300 per hour")
    LOGIN_RATE_LIMIT = _str("LOGIN_RATE_LIMIT", "5 per minute; 15 per hour")
    CONTENT_SECURITY_POLICY = _str("CONTENT_SECURITY_POLICY")       # vacío = sin cabecera CSP

    # --- COMPORTAMIENTO DEL BOT ---
    REQUIRE_ID_VALIDATION = _bool("REQUIRE_ID_VALIDATION", False)  # pedir DNI/ID de la tabla de colaboradores antes del menú
    ID_LABEL = _str("ID_LABEL", "DNI")
    ID_LENGTHS = _int_list("ID_LENGTHS", "8,9")
    ID_MAX_ATTEMPTS = _int("ID_MAX_ATTEMPTS", 5)
    ID_LOCK_MINUTES = _int("ID_LOCK_MINUTES", 30)
    MENU_KEYWORDS = [k.strip().lower() for k in
                     _str("MENU_KEYWORDS", "hola,inicio,menu,empezar,comenzar,ayuda,info").split(",") if k.strip()]
    GLOBAL_MENU_KEYWORDS = [k.strip().lower() for k in
                            _str("GLOBAL_MENU_KEYWORDS", "inicio,menu,home,volver,0,empezar").split(",") if k.strip()]
    OPT_OUT_KEYWORDS = [k.strip().lower() for k in
                        _str("OPT_OUT_KEYWORDS", "baja,stop,no deseo mensajes,no enviar,cancelar suscripcion").split(",") if k.strip()]
    INACTIVE_STATES = [s.strip().lower() for s in
                       _str("INACTIVE_STATES", "inactivo,cesado,baja").split(",") if s.strip()]

    FLOW_TIMEOUT_MINUTES = _int("FLOW_TIMEOUT_MINUTES", 15)   # una conversación guiada expira tras N min sin respuesta
    MASK_ID_IN_LOGS = _bool("MASK_ID_IN_LOGS", True)         # guarda DNI como ****1234 en el historial de mensajes

    # --- CONSULTA ACADÉMICA (API propia: deudas y notas) ---
    ACAD_API_URL = _str("ACAD_API_URL").rstrip("/")
    ACAD_API_TOKEN = os.environ.get("ACAD_API_TOKEN", "").strip()
    # Hosts que pueden recibir ACAD_API_TOKEN cuando un endpoint del panel no tiene token propio
    # (evita filtrar el token a otros dominios). Por defecto: el host de ACAD_API_URL.
    ACAD_TOKEN_HOSTS = [h.strip().lower() for h in _str("ACAD_TOKEN_HOSTS", _host(ACAD_API_URL)).split(",") if h.strip()]
    CALLCENTER_WEBHOOK_URL = _str("CALLCENTER_WEBHOOK_URL")      # URL inicial del endpoint notificar_callcenter
    ACAD_AUTH_HEADER = _str("ACAD_AUTH_HEADER", "Authorization")
    ACAD_AUTH_SCHEME = _str("ACAD_AUTH_SCHEME", "Bearer")      # vacío = token tal cual (API key)
    ACAD_API_TIMEOUT = _int("ACAD_API_TIMEOUT", 10)
    ACAD_API_MOCK = _bool("ACAD_API_MOCK", False)               # datos de DEMO sin API real (nunca en producción)
    ACAD_PATH_ANIOS = _str("ACAD_PATH_ANIOS", "/anios")
    ACAD_PATH_PERIODOS = _str("ACAD_PATH_PERIODOS", "/periodos")
    ACAD_PATH_DEUDAS = _str("ACAD_PATH_DEUDAS", "/deudas")
    ACAD_PATH_NOTAS = _str("ACAD_PATH_NOTAS", "/notas")
    ACAD_ANIOS_FIJOS = [a.strip() for a in _str("ACAD_ANIOS_FIJOS").split(",") if a.strip()]
    ACAD_PERIODOS_FIJOS = [p.strip() for p in _str("ACAD_PERIODOS_FIJOS").split(",") if p.strip()]
    # fecha_nacimiento | telefono | none  (segundo control además del DNI)
    ACAD_VERIFY_MODE = _str("ACAD_VERIFY_MODE", "fecha_nacimiento").lower()
    ACAD_NOTAS_REQUIERE_PAGOS_AL_DIA = _bool("ACAD_NOTAS_REQUIERE_PAGOS_AL_DIA", True)
    ACAD_MAX_ATTEMPTS = _int("ACAD_MAX_ATTEMPTS", 5)
    ACAD_LOCK_MINUTES = _int("ACAD_LOCK_MINUTES", 30)
    ACAD_MAX_CONSULTAS_DIA = _int("ACAD_MAX_CONSULTAS_DIA", 25)  # tope diario de consultas académicas por teléfono

    # --- COMPROBANTES DE PAGO (archivos recibidos por WhatsApp) ---
    COMPROBANTES_DIR = _str("COMPROBANTES_DIR", "/app/data/comprobantes")
    COMPROBANTE_MAX_MB = _int("COMPROBANTE_MAX_MB", 5)
    COMPROBANTE_ALMACEN = _str("COMPROBANTE_ALMACEN", "bd").lower()      # bd = en PostgreSQL (con los respaldos de la BD) | disco = carpeta/volumen

    # --- CATÁLOGO DE SEDES (API propia: sedes, precios, vacantes, aulas) ---
    CAT_API_URL = _str("CAT_API_URL").rstrip("/")
    CAT_API_TOKEN = os.environ.get("CAT_API_TOKEN", "").strip()
    CAT_AUTH_HEADER = _str("CAT_AUTH_HEADER", "Authorization")
    CAT_AUTH_SCHEME = _str("CAT_AUTH_SCHEME", "Bearer")
    CAT_API_TIMEOUT = _int("CAT_API_TIMEOUT", 8)
    CAT_API_MOCK = _bool("CAT_API_MOCK", False)                 # datos de DEMO (nunca en producción)
    CAT_PATH_SEDES = _str("CAT_PATH_SEDES", "/sedes")
    CAT_PATH_OFERTA = _str("CAT_PATH_OFERTA", "/oferta")
    CAT_PATH_REQUISITOS = _str("CAT_PATH_REQUISITOS", "/requisitos")       # opcional (también pueden venir dentro de /oferta)
    CAT_PATH_PLANES = _str("CAT_PATH_PLANES", "/planes-pago")             # opcional (también pueden venir dentro de /oferta)
    CAT_CACHE_SEGUNDOS = _int("CAT_CACHE_SEGUNDOS", 60)          # 0 = sin caché (vacantes siempre en vivo)

    # --- ASISTENTE DE VENTAS CON IA (Gemini) ---
    IA_ENABLED = _bool("IA_ENABLED", False)
    IA_PROVIDER = _str("IA_PROVIDER", "gemini").lower()                  # gemini | openrouter
    IA_PROVIDER_RESPALDO = _str("IA_PROVIDER_RESPALDO").lower()          # se usa si el principal falla (límite gratuito, caída)
    OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
    # Uno o varios modelos separados por coma; si uno falla o está saturado se prueba el siguiente
    OPENROUTER_MODELS = [m.strip() for m in _str("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free").split(",") if m.strip()]
    OPENROUTER_BASE_URL = _str("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    OPENROUTER_REFERER = _str("OPENROUTER_REFERER")                      # opcional: tu dominio (estadísticas en OpenRouter)
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL = _str("GEMINI_MODEL", "gemini-3.8-flash")
    GEMINI_BASE_URL = _str("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    IA_TIMEOUT = _int("IA_TIMEOUT", 20)
    IA_MAX_MSG_DIA_TEL = _int("IA_MAX_MSG_DIA_TEL", 30)          # tope de mensajes con IA por teléfono y día
    IA_MAX_LLAMADAS_DIA = _int("IA_MAX_LLAMADAS_DIA", 2000)      # tope global de llamadas a Gemini por día
    IA_HISTORIAL = _int("IA_HISTORIAL", 8)                       # mensajes previos que se le dan como memoria
    IA_MAX_RESPUESTA = _int("IA_MAX_RESPUESTA", 900)             # caracteres máx. por respuesta
    IA_PRODUCTOS = _str("IA_PRODUCTOS", "Colegio, Instituto")             # productos que ofrece el asistente (texto para la IA)
    IA_PERSONALIDAD = _str("IA_PERSONALIDAD", "cálida, breve y profesional")
    IA_INSTRUCCIONES_EXTRA = os.environ.get("IA_INSTRUCCIONES_EXTRA", "").strip()  # ofertas, tono, políticas
    IA_VERIFICAR = _bool("IA_VERIFICAR", True)                   # revisa cada respuesta antes de enviarla (recomendado)
    IA_PALABRAS_RIESGO = [x.strip().lower() for x in _str(
        "IA_PALABRAS_RIESGO", "descuento,beca,gratis,gratuito,promoción,promocion,rebaja,garantizamos,garantizado,exonera").split(",") if x.strip()]
    IA_BLOQUEAR_NOMBRES = [x.strip().lower() for x in _str("IA_BLOQUEAR_NOMBRES").split(",") if x.strip()]  # competidores a no mencionar
    IA_MENSAJE_SEGURO = _str("IA_MENSAJE_SEGURO", "Para darte este dato con exactitud prefiero que te lo confirme un asesor. ¿Te comunico con uno?")
    ASESOR_TELEFONO = _str("ASESOR_TELEFONO")                    # respaldo si la sede no trae teléfono de asesor (con código de país)

    # --- BASE DE DATOS (PostgreSQL) ---
    DB_USER = _str("DB_USER", "chatbot")
    DB_PASS = os.environ.get("DB_PASS", "")
    DB_HOST = _str("DB_HOST")            # nombre del contenedor de PostgreSQL en proxy-net
    DB_NAME = _str("DB_NAME", "chatbot_db")
    DB_PORT = _str("DB_PORT", "5432")
    _DATABASE_URL = _str("DATABASE_URL")
    SQLALCHEMY_DATABASE_URI = _DATABASE_URL or (
        f"postgresql://{quote_plus(DB_USER)}:{quote_plus(DB_PASS)}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    DB_POOL_SIZE = _int("DB_POOL_SIZE", 10)
    DB_MAX_OVERFLOW = _int("DB_MAX_OVERFLOW", 10)
    DB_POOL_RECYCLE = _int("DB_POOL_RECYCLE", 300)
    SQLALCHEMY_ENGINE_OPTIONS = (
        {} if SQLALCHEMY_DATABASE_URI.startswith("sqlite")
        else {"pool_pre_ping": True, "pool_recycle": DB_POOL_RECYCLE,
              "pool_size": DB_POOL_SIZE, "max_overflow": DB_MAX_OVERFLOW}
    )

    UPLOAD_MAX_MB = _int("UPLOAD_MAX_MB", 5)
    MAX_CONTENT_LENGTH = UPLOAD_MAX_MB * 1024 * 1024  # subida de Excel

    @classmethod
    def bot_sender(cls):
        return f"{cls.BOT_NAME} 🤖"

    IA_AVISO = ""      # motivo por el que la IA quedó apagada o limitada (se muestra en el panel)

    @classmethod
    def aplicar_ia(cls):
        """La IA es opcional: si su configuración es incorrecta NO se tumba el bot; se apaga la IA y se avisa."""
        import logging
        log = logging.getLogger("config")
        avisos = []
        if cls.IA_ENABLED:
            claves = {"gemini": cls.GEMINI_API_KEY, "openrouter": cls.OPENROUTER_API_KEY}
            if cls.IA_PROVIDER not in claves:
                avisos.append(f"IA_PROVIDER desconocido ('{cls.IA_PROVIDER}'; usa gemini u openrouter)")
                cls.IA_ENABLED = False
            elif not claves[cls.IA_PROVIDER]:
                avisos.append(f"falta la clave del proveedor principal ({cls.IA_PROVIDER})")
                cls.IA_ENABLED = False
            if cls.IA_PROVIDER_RESPALDO:
                if cls.IA_PROVIDER_RESPALDO not in claves or not claves.get(cls.IA_PROVIDER_RESPALDO):
                    avisos.append(f"respaldo '{cls.IA_PROVIDER_RESPALDO}' ignorado (proveedor desconocido o sin clave)")
                    cls.IA_PROVIDER_RESPALDO = ""
        cls.IA_AVISO = "; ".join(avisos)
        for a in avisos:
            log.warning("IA: %s", a)
        if avisos and not cls.IA_ENABLED:
            log.error("IA DESACTIVADA por configuración. El bot sigue funcionando con menús.")

    @classmethod
    def validate(cls):
        """Falla al arrancar si falta algo obligatorio o quedó un valor de plantilla."""
        errores = []
        obligatorias = {
            "TOKEN_PERMANENTE": cls.TOKEN_PERMANENTE,
            "PHONE_ID": cls.PHONE_ID,
            "WEBHOOK_TOKEN": cls.WEBHOOK_TOKEN,
            "APP_SECRET": cls.APP_SECRET,
            "ADMIN_USER": cls.ADMIN_USER,
            "ADMIN_PASS": cls.ADMIN_PASS,
            "SECRET_KEY": cls.SECRET_KEY,
        }
        if not cls._DATABASE_URL:
            obligatorias["DB_PASS"] = cls.DB_PASS
            obligatorias["DB_HOST"] = cls.DB_HOST
        for nombre, valor in obligatorias.items():
            if not valor:
                errores.append(f"Falta la variable {nombre}")
            elif str(valor).upper().startswith("CAMBIAR"):
                errores.append(f"{nombre} todavía tiene el valor de plantilla (CAMBIAR...)")
        if cls.SECRET_KEY and len(cls.SECRET_KEY) < 32:
            errores.append("SECRET_KEY debe tener al menos 32 caracteres")
        if cls.ADMIN_PASS and len(cls.ADMIN_PASS) < cls.PASSWORD_MIN_LENGTH:
            errores.append(f"ADMIN_PASS debe tener al menos {cls.PASSWORD_MIN_LENGTH} caracteres")
        if cls.COMPROBANTE_ALMACEN not in ("bd", "disco"):
            errores.append("COMPROBANTE_ALMACEN debe ser bd o disco")
        if cls.ACAD_VERIFY_MODE not in ("fecha_nacimiento", "telefono", "none"):
            errores.append("ACAD_VERIFY_MODE debe ser fecha_nacimiento, telefono o none")
        if errores:
            raise RuntimeError(
                "Configuración inválida (revisa tu archivo .env):\n  - " + "\n  - ".join(errores)
            )
