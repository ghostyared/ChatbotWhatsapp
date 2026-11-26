from datetime import datetime, timedelta, timezone
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from config import Config

db = SQLAlchemy()


def utcnow():
    """UTC 'naive' (sin tzinfo) para guardar en BD."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Empresa(db.Model):
    """Institución / empresa independiente (multi-tenant) con su propio contexto IA, URL y horario."""
    __tablename__ = 'empresas'
    id = db.Column(db.Integer, primary_key=True)
    clave = db.Column(db.String(60), unique=True, nullable=False, index=True)
    nombre_comercial = db.Column(db.String(150), nullable=False)
    dominio_web = db.Column(db.String(255), default='')
    horario_atencion = db.Column(db.String(255), default='Lunes a viernes, 08:00 a 18:00')
    horario_json = db.Column(db.Text, default='')
    prompt_sistema = db.Column(db.Text, default='')
    base_conocimiento = db.Column(db.Text, default='')
    telefono_asesor = db.Column(db.String(30), default='')
    cola_asesores = db.Column(db.String(80), default='general')
    activo = db.Column(db.Boolean, default=True, nullable=False)
    orden = db.Column(db.Integer, default=0, nullable=False)
    actualizado_por = db.Column(db.String(60))
    actualizado_en = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    creado_en = db.Column(db.DateTime, default=utcnow)

    @staticmethod
    def seed_from_ajuste():
        """Si aún no hay empresas pero existe conocimiento previo en Ajuste('ia_conocimiento'),
        lo migra a una empresa inicial para no perder datos existentes."""
        if Empresa.query.count() > 0:
            return False
        aj = db.session.get(Ajuste, 'ia_conocimiento')
        if not aj or not (aj.valor or '').strip():
            return False
        emp = Empresa(
            clave='general',
            nombre_comercial=(Config.COMPANY_NAME or 'Institución Principal')[:150],
            base_conocimiento=aj.valor,
            activo=True,
            orden=1,
            actualizado_por=aj.actualizado_por or 'migracion',
        )
        db.session.add(emp)
        db.session.commit()
        return True


class Usuario(db.Model):
    """Contacto de WhatsApp (quien escribe al bot)."""
    __tablename__ = 'usuarios'
    id = db.Column(db.Integer, primary_key=True)
    telefono = db.Column(db.String(20), unique=True, index=True)
    nombre_wa = db.Column(db.String(100))
    dni = db.Column(db.String(20), nullable=True, index=True)  # ID validado (memoria)
    ultima_vez = db.Column(db.DateTime, default=utcnow)
    bot_pausado = db.Column(db.Boolean, default=False)
    intentos_id = db.Column(db.Integer, default=0, nullable=False)
    bloqueado_id_hasta = db.Column(db.DateTime, nullable=True)
    intentos_acad = db.Column(db.Integer, default=0, nullable=False)
    bloqueado_acad_hasta = db.Column(db.DateTime, nullable=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=True, index=True)
    saludo_enviado = db.Column(db.Boolean, default=False, nullable=False)
    estado_chat = db.Column(db.String(20), default='bot', nullable=False, index=True)  # bot | en_cola | atendido
    asignado_a = db.Column(db.String(60), nullable=True, index=True)
    esperando_asesor_desde = db.Column(db.DateTime, nullable=True)
    esperando_usuario_desde = db.Column(db.DateTime, nullable=True)
    aviso_inactividad_enviado = db.Column(db.Boolean, default=False, nullable=False)
    opt_out = db.Column(db.Boolean, default=False, nullable=False)            # True si pidió BAJA/STOP de campañas HSM
    ultimo_msg_entrada = db.Column(db.DateTime, nullable=True)                # para calcular ventana gratuita de 24h Meta


class Mensaje(db.Model):
    __tablename__ = 'mensajes'
    id = db.Column(db.Integer, primary_key=True)
    telefono = db.Column(db.String(20), index=True)
    nombre = db.Column(db.String(100))
    contenido = db.Column(db.Text)
    direccion = db.Column(db.String(10))  # ENTRADA / SALIDA / INTERNO
    es_interno = db.Column(db.Boolean, default=False, nullable=False)  # Modo Susurro (solo panel web)
    estado_envio = db.Column(db.String(20), nullable=True)             # sent | delivered | read | failed
    error_meta = db.Column(db.String(300), nullable=True)
    media_tipo = db.Column(db.String(20), nullable=True)               # image | video | document | audio
    media_url = db.Column(db.String(500), nullable=True)
    fecha = db.Column(db.DateTime, default=utcnow, index=True)
    wa_id = db.Column(db.String(128), unique=True, nullable=True)  # id de Meta: evita duplicados y recibe checks de lectura


class Conversacion(db.Model):
    """Estado de una conversación guiada (flujo + paso + datos recogidos). Una por teléfono."""
    __tablename__ = 'conversaciones'
    id = db.Column(db.Integer, primary_key=True)
    telefono = db.Column(db.String(20), unique=True, index=True)
    flujo = db.Column(db.String(30), nullable=False)
    paso = db.Column(db.String(30), nullable=False)
    datos = db.Column(db.Text, default='{}')
    expira_en = db.Column(db.DateTime, nullable=False)


class Comprobante(db.Model):
    """Comprobante de pago recibido por WhatsApp, pendiente de revisión por contabilidad."""
    __tablename__ = 'comprobantes'
    id = db.Column(db.Integer, primary_key=True)
    telefono = db.Column(db.String(20), index=True)
    nombre_wa = db.Column(db.String(100))
    dni_estudiante = db.Column(db.String(20), index=True)
    archivo = db.Column(db.String(80), nullable=False)       # nombre del archivo (uuid + extensión)
    contenido = db.deferred(db.Column(db.LargeBinary))       # bytes del comprobante cuando COMPROBANTE_ALMACEN=bd
    mime = db.Column(db.String(60))
    tamano = db.Column(db.Integer)
    sha256 = db.Column(db.String(64), index=True)
    duplicado = db.Column(db.Boolean, default=False)         # mismo archivo ya recibido antes
    nota = db.Column(db.String(300))                         # texto que acompañó al archivo
    estado = db.Column(db.String(12), default='pendiente', index=True)   # pendiente|aprobado|rechazado
    motivo = db.Column(db.String(300))
    creado_en = db.Column(db.DateTime, default=utcnow, index=True)
    revisado_por = db.Column(db.String(50))
    revisado_en = db.Column(db.DateTime)

    @property
    def codigo(self):
        return f"CP-{self.id:06d}"


class AuditoriaConsulta(db.Model):
    """Registro de cada consulta de deudas/notas (quién, qué, resultado). El DNI se guarda enmascarado."""
    __tablename__ = 'auditoria_consultas'
    id = db.Column(db.Integer, primary_key=True)
    telefono = db.Column(db.String(20), index=True)
    tipo = db.Column(db.String(10))
    dni_mask = db.Column(db.String(20))
    anio = db.Column(db.String(10))
    periodo = db.Column(db.String(20))
    resultado = db.Column(db.String(20))     # ok | no_coincide | bloqueado | pagos_pendientes | error
    fecha = db.Column(db.DateTime, default=utcnow, index=True)


class Lead(db.Model):
    """Interesado captado por el asistente de ventas (seguimiento comercial)."""
    __tablename__ = 'leads'
    id = db.Column(db.Integer, primary_key=True)
    telefono = db.Column(db.String(20), index=True)
    nombre = db.Column(db.String(120))
    sede_id = db.Column(db.String(60))
    sede_nombre = db.Column(db.String(120))
    nivel = db.Column(db.String(80))
    edad_hijo = db.Column(db.String(40))
    comentario = db.Column(db.Text)
    visita_pref = db.Column(db.String(120))          # "sábado 10am" tal como lo dijo la persona
    quiere_asesor = db.Column(db.Boolean, default=False)
    estado = db.Column(db.String(20), default='nuevo', index=True)   # nuevo|contactado|visita|matriculado|descartado
    asesor_avisado = db.Column(db.Boolean, default=False)
    notas_internas = db.Column(db.Text)
    atendido_por = db.Column(db.String(60))
    asignado_a = db.Column(db.String(60), index=True)
    prioridad = db.Column(db.String(15), default='media', index=True)  # alta|media|baja
    telefono_contacto = db.Column(db.String(30))
    resultado_llamada = db.Column(db.String(30), default='pendiente')
    resumen_ia = db.Column(db.Text)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=True, index=True)
    origen = db.Column(db.String(30), default='organico', index=True)     # organico|facebook_ads|instagram_ads|menu
    campana_id = db.Column(db.String(100), nullable=True, index=True)
    campana_nombre = db.Column(db.String(150), nullable=True)
    ctwa_clid = db.Column(db.String(200), nullable=True)
    score = db.Column(db.String(15), default='frio', index=True)          # frio|tibio|caliente
    tags = db.Column(db.String(255), default='')                          # etiquetas separadas por coma (ej. Beca,Turno Noche)
    csat = db.Column(db.Integer, nullable=True)                           # satisfacción 1-5 estrellas
    primera_respuesta_en = db.Column(db.DateTime, nullable=True)
    resuelto_en = db.Column(db.DateTime, nullable=True)
    creado_en = db.Column(db.DateTime, default=utcnow, index=True)
    actualizado_en = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    ESTADOS = ('nuevo', 'contactado', 'visita', 'matriculado', 'descartado')
    PRIORIDADES = ('alta', 'media', 'baja')
    SCORES = ('frio', 'tibio', 'caliente')
    RESULTADOS_LLAMADA = ('pendiente', 'contactado', 'no_contesta', 'volver_llamar', 'cita_agendada', 'matriculado', 'descartado')


class MaterialComercial(db.Model):
    """Biblioteca de flyers (imágenes), videos y brochures PDF por empresa para envío en 1 clic desde /monitor."""
    __tablename__ = 'materiales_comerciales'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=True, index=True)
    titulo = db.Column(db.String(150), nullable=False)
    tipo = db.Column(db.String(20), nullable=False, default='image')      # image | video | document
    url = db.Column(db.String(500), nullable=False)
    filename = db.Column(db.String(120), default='')
    caption = db.Column(db.String(500), default='')
    activo = db.Column(db.Boolean, default=True, nullable=False)
    creado_en = db.Column(db.DateTime, default=utcnow)


class LeadEvento(db.Model):
    """Timeline de auditoría comercial de un Lead (cambios de estado, asignaciones, HSM, citas)."""
    __tablename__ = 'lead_eventos'
    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey('leads.id'), nullable=False, index=True)
    tipo = db.Column(db.String(30), nullable=False)                       # estado | asignacion | hsm | cita | material | csat
    detalle = db.Column(db.String(300), default='')
    usuario = db.Column(db.String(60), default='sistema')
    creado_en = db.Column(db.DateTime, default=utcnow, index=True)


class Cita(db.Model):
    """Agenda de citas y llamadas programadas (fuera de horario, SLA vencido o visita guiada)."""
    __tablename__ = 'citas'
    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey('leads.id'), nullable=True, index=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=True, index=True)
    telefono = db.Column(db.String(20), nullable=False, index=True)
    nombre = db.Column(db.String(120), default='')
    fecha_preferida = db.Column(db.String(60), default='')
    franja = db.Column(db.String(80), default='')
    motivo = db.Column(db.Text, default='')
    asignado_a = db.Column(db.String(60), nullable=True, index=True)
    estado = db.Column(db.String(20), default='pendiente', nullable=False, index=True)
    recordatorio_enviado = db.Column(db.Boolean, default=False, nullable=False)
    creado_en = db.Column(db.DateTime, default=utcnow, index=True)
    actualizado_en = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    ESTADOS = ('pendiente', 'confirmada', 'realizada', 'cancelada', 'no_contesta')


class ReglaCampana(db.Model):
    """Reglas de atribución Zero-Friction para campañas Click-to-WhatsApp (Facebook / Instagram Ads)."""
    __tablename__ = 'reglas_campana'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(150), nullable=False)
    source_id = db.Column(db.String(100), nullable=True, index=True)      # ID de anuncio de Meta (referral.source_id)
    palabras_clave = db.Column(db.String(300), default='')                # palabras separadas por coma en mensaje predeterminado
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=True, index=True)
    programa = db.Column(db.String(150), default='')
    mensaje_bienvenida = db.Column(db.Text, default='')
    brochure_url = db.Column(db.String(500), default='')
    brochure_nombre = db.Column(db.String(120), default='brochure.pdf')
    nodo_destino = db.Column(db.String(50), default='')
    activo = db.Column(db.Boolean, default=True, nullable=False)
    creado_en = db.Column(db.DateTime, default=utcnow)


class PlantillaRapida(db.Model):
    """Respuestas rápidas formales (/atajos) para colaboradores con variables {{usuario}}, {{asesor}}, {{url_empresa}}."""
    __tablename__ = 'plantillas_rapidas'
    id = db.Column(db.Integer, primary_key=True)
    atajo = db.Column(db.String(40), unique=True, nullable=False, index=True)
    titulo = db.Column(db.String(100), nullable=False)
    contenido = db.Column(db.Text, nullable=False)
    activo = db.Column(db.Boolean, default=True, nullable=False)

    @staticmethod
    def seed():
        iniciales = [
            (
                '/saludo',
                'Saludo formal de asesor',
                "¡Hola, {{usuario}}! 😎\nTe saluda {{asesor}}, gracias por comunicarte con nosotros.\n"
                "Para poder ayudarte mejor, ¿podrías brindarme un poco más de detalle sobre tu consulta, por favor? Estoy atento para apoyarte. 😊",
            ),
            (
                '/duda',
                'Seguimiento post-información',
                "¿Tienes alguna duda sobre la información brindada? 🙌🏻",
            ),
            (
                '/espera',
                'Aviso de espera (2 min)',
                "Tal vez en estos momentos no puedas responder, pero no te preocupes, estaré esperándote 2 minutos más para poderte ayudar.",
            ),
            (
                '/cierre',
                'Cierre formal por inactividad',
                "Al no haber alguna respuesta, concluiremos con la conversación.\n"
                "¡Gracias por escribirnos, {{usuario}}!\n"
                "Puedes platicar con nosotros cuando lo necesites.\n"
                "También te invitamos a {{url_empresa}} para acceder a tus beneficios.",
            ),
        ]
        for atajo, titulo, contenido in iniciales:
            if not PlantillaRapida.query.filter_by(atajo=atajo).first():
                db.session.add(PlantillaRapida(atajo=atajo, titulo=titulo, contenido=contenido, activo=True))
        db.session.commit()


class IaUso(db.Model):
    """Contador diario de uso de la IA por teléfono (tope de gasto)."""
    __tablename__ = 'ia_uso'
    id = db.Column(db.Integer, primary_key=True)
    telefono = db.Column(db.String(20), index=True)
    dia = db.Column(db.Date, index=True)
    mensajes = db.Column(db.Integer, default=0, nullable=False)
    tokens = db.Column(db.Integer, default=0, nullable=False)
    __table_args__ = (db.UniqueConstraint('telefono', 'dia', name='uq_ia_uso_tel_dia'),)


class PreguntaPendiente(db.Model):
    """Cola de aprendizaje continuo (Knowledge Gap Tracker): preguntas que la IA no pudo responder."""
    __tablename__ = 'ia_preguntas_pendientes'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=True, index=True)
    pregunta = db.Column(db.Text, nullable=False)
    pregunta_norm = db.Column(db.String(300), nullable=False, index=True)
    telefono = db.Column(db.String(20), index=True)
    contexto_previo = db.Column(db.Text, default='')
    veces = db.Column(db.Integer, default=1, nullable=False)
    estado = db.Column(db.String(20), default='pendiente', nullable=False, index=True)  # pendiente|aprobada|descartada
    respuesta_oficial = db.Column(db.Text, default='')
    revisado_por = db.Column(db.String(60))
    creado_en = db.Column(db.DateTime, default=utcnow, index=True)
    revisado_en = db.Column(db.DateTime, nullable=True)

    @staticmethod
    def normalizar(texto):
        import re
        import unicodedata
        t = (texto or '').lower().strip()
        t = ''.join(c for c in unicodedata.normalize('NFD', t) if unicodedata.category(c) != 'Mn')
        t = re.sub(r'[^a-z0-9ñ\s]+', ' ', t)
        return re.sub(r'\s+', ' ', t).strip()[:300]

    @staticmethod
    def registrar(pregunta, empresa_id=None, telefono='', contexto_previo=''):
        p = (pregunta or '').strip()[:1000]
        if not p:
            return None
        norm = PreguntaPendiente.normalizar(p)
        if not norm:
            return None
        existente = PreguntaPendiente.query.filter_by(
            empresa_id=empresa_id, pregunta_norm=norm, estado='pendiente'
        ).first()
        if existente:
            existente.veces = (existente.veces or 1) + 1
            if telefono:
                existente.telefono = str(telefono)[:20]
            if contexto_previo and not existente.contexto_previo:
                existente.contexto_previo = contexto_previo[:1000]
            db.session.commit()
            return existente
        nueva = PreguntaPendiente(
            empresa_id=empresa_id,
            pregunta=p,
            pregunta_norm=norm,
            telefono=str(telefono or '')[:20],
            contexto_previo=(contexto_previo or '')[:1000],
            veces=1,
            estado='pendiente',
        )
        db.session.add(nueva)
        db.session.commit()
        return nueva


class Ajuste(db.Model):
    """Ajustes editables desde el panel (p. ej. la propuesta de venta que usa la IA)."""
    __tablename__ = 'ajustes'
    clave = db.Column(db.String(60), primary_key=True)
    valor = db.Column(db.Text, default='')
    actualizado_por = db.Column(db.String(60))
    actualizado_en = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    @staticmethod
    def obtener(clave, defecto=''):
        a = db.session.get(Ajuste, clave)
        return a.valor if a and a.valor else defecto

    @staticmethod
    def guardar(clave, valor, usuario=None):
        a = db.session.get(Ajuste, clave) or Ajuste(clave=clave)
        a.valor, a.actualizado_por = valor, usuario
        db.session.add(a)
        db.session.commit()


class ApiEndpoint(db.Model):
    """Endpoints HTTP configurables desde /admin/endpoints (URL, token y cuerpo JSON con variables dinámicas)."""
    __tablename__ = 'api_endpoints'
    id = db.Column(db.Integer, primary_key=True)
    clave = db.Column(db.String(60), unique=True, nullable=False, index=True)
    nombre = db.Column(db.String(120), nullable=False)
    descripcion = db.Column(db.String(300), default='')
    metodo = db.Column(db.String(10), nullable=False, default='POST')
    url = db.Column(db.String(500), nullable=False)
    auth_header = db.Column(db.String(60), default='Authorization')
    auth_scheme = db.Column(db.String(30), default='Bearer')
    token = db.Column(db.Text, default='')
    headers_json = db.Column(db.Text, default='{"Content-Type": "application/json"}')
    body_template = db.Column(db.Text, default='{}')
    timeout = db.Column(db.Integer, default=10, nullable=False)
    activo = db.Column(db.Boolean, default=True, nullable=False)
    actualizado_por = db.Column(db.String(60))
    actualizado_en = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    @staticmethod
    def seed():
        """Crea los endpoints base si no existen (nunca pisa los que ya editaste desde el panel)."""
        base_url = (Config.ACAD_API_URL or "").rstrip("/")   # definir ACAD_API_URL en el .env
        tok = Config.ACAD_API_TOKEN or ""
        iniciales = [
            dict(
                clave="periodos_estudiante",
                nombre="Periodos del Estudiante",
                descripcion="Devuelve los datos del estudiante y las empresas/periodos matriculados según su DNI.",
                metodo="POST",
                url=f"{base_url}/PeriodosEstudiante.php",
                auth_header=Config.ACAD_AUTH_HEADER or "Authorization",
                auth_scheme=Config.ACAD_AUTH_SCHEME or "Bearer",
                token=tok,
                headers_json='{"Content-Type": "application/json"}',
                body_template='{\n  "dni": "{dni}"\n}',
                timeout=Config.ACAD_API_TIMEOUT or 10,
                activo=True,
            ),
            dict(
                clave="consultar_pagos",
                nombre="Cuotas de Pago",
                descripcion="Consulta las cuotas y estado de pagos usando IdEstudiante e IdPeriodo seleccionados.",
                metodo="POST",
                url=f"{base_url}/ConsultarPagos.php",
                auth_header=Config.ACAD_AUTH_HEADER or "Authorization",
                auth_scheme=Config.ACAD_AUTH_SCHEME or "Bearer",
                token=tok,
                headers_json='{"Content-Type": "application/json"}',
                body_template='{\n  "IdEstudiante": {IdEstudiante},\n  "IdPeriodo": {IdPeriodo}\n}',
                timeout=Config.ACAD_API_TIMEOUT or 10,
                activo=True,
            ),
            dict(
                clave="consultar_notas",
                nombre="Notas del Estudiante (Colegio)",
                descripcion="Consulta la boleta de notas por subperiodos (solo Colegio, no Pedagógico).",
                metodo="POST",
                url=f"{base_url}/ConsultarNotas.php",
                auth_header=Config.ACAD_AUTH_HEADER or "Authorization",
                auth_scheme=Config.ACAD_AUTH_SCHEME or "Bearer",
                token=tok,
                headers_json='{"Content-Type": "application/json"}',
                body_template='{\n  "IdEstudiante": {IdEstudiante},\n  "IdPeriodo": {IdPeriodo}\n}',
                timeout=Config.ACAD_API_TIMEOUT or 10,
                activo=True,
            ),
            dict(
                clave="consultar_comunicados",
                nombre="Comunicados",
                descripcion="Consulta de comunicados institucionales (actívalo cuando configures su URL y cuerpo).",
                metodo="POST",
                url=f"{base_url}/ConsultarComunicados.php",
                auth_header=Config.ACAD_AUTH_HEADER or "Authorization",
                auth_scheme=Config.ACAD_AUTH_SCHEME or "Bearer",
                token=tok,
                headers_json='{"Content-Type": "application/json"}',
                body_template='{\n  "IdEstudiante": {IdEstudiante},\n  "IdEmpresa": {IdEmpresa},\n  "IdCarrera": {IdCarrera},\n  "IdCiclo": {IdCiclo},\n  "IdPeriodo": {IdPeriodo}\n}',
                timeout=Config.ACAD_API_TIMEOUT or 10,
                activo=False,
            ),
            dict(
                clave="notificar_callcenter",
                nombre="Webhook Call Center / CRM Externo",
                descripcion="Opcional: reenvía automáticamente cada nuevo lead/solicitud de llamada a tu CRM, Teams o central telefónica.",
                metodo="POST",
                url=Config.CALLCENTER_WEBHOOK_URL,   # definir CALLCENTER_WEBHOOK_URL en el .env
                auth_header=Config.ACAD_AUTH_HEADER or "Authorization",
                auth_scheme=Config.ACAD_AUTH_SCHEME or "Bearer",
                token=tok,
                headers_json='{"Content-Type": "application/json"}',
                body_template='{\n  "lead_id": {lead_id},\n  "nombre": "{nombre}",\n  "telefono": "{telefono}",\n  "sede": "{sede}",\n  "nivel": "{nivel}",\n  "prioridad": "{prioridad}",\n  "resumen": "{resumen}"\n}',
                timeout=8,
                activo=False,
            ),
            dict(
                clave="registrar_comprobante",
                nombre="Registrar Comprobante en Caja/Facturación",
                descripcion="Opcional: sincroniza comprobantes recibidos por WhatsApp con el sistema financiero y devuelve numero_ticket.",
                metodo="POST",
                url=f"{base_url}/api/v1/estudiantes/comprobantes" if base_url else "",
                auth_header=Config.ACAD_AUTH_HEADER or "Authorization",
                auth_scheme=Config.ACAD_AUTH_SCHEME or "Bearer",
                token=tok,
                headers_json='{"Content-Type": "application/json"}',
                body_template='{\n  "dni": "{dni}",\n  "empresa_id": {empresa_id},\n  "codigo_comprobante": "{codigo}",\n  "telefono": "{telefono}",\n  "nota": "{nota}"\n}',
                timeout=10,
                activo=False,
            ),
            dict(
                clave="agendar_cita",
                nombre="Agendar Cita / Llamada Programada (CRM)",
                descripcion="Opcional: sincroniza citas o llamadas agendadas fuera de horario con tu CRM o agenda externa.",
                metodo="POST",
                url=f"{base_url}/api/v1/crm/citas" if base_url else "",
                auth_header=Config.ACAD_AUTH_HEADER or "Authorization",
                auth_scheme=Config.ACAD_AUTH_SCHEME or "Bearer",
                token=tok,
                headers_json='{"Content-Type": "application/json"}',
                body_template='{\n  "lead_id": {lead_id},\n  "empresa_id": {empresa_id},\n  "franja": "{franja}",\n  "motivo": "{motivo}"\n}',
                timeout=10,
                activo=False,
            ),
        ]
        for item in iniciales:
            existente = ApiEndpoint.query.filter_by(clave=item["clave"]).first()
            if not existente:
                db.session.add(ApiEndpoint(**item))
            elif (
                item["clave"] == "consultar_notas"
                and not existente.activo
                and not existente.actualizado_por
            ):
                # Actualización automática de plantilla no editada manualmente
                existente.nombre = item["nombre"]
                existente.descripcion = item["descripcion"]
                existente.body_template = item["body_template"]
                existente.activo = True
                if not existente.token and tok:
                    existente.token = tok
        db.session.commit()


class RespuestaBot(db.Model):
    """Nodo del flujo: texto + link/documento opcional + botones o lista (JSON) editable desde el panel."""
    __tablename__ = 'respuestas_bot'
    id = db.Column(db.Integer, primary_key=True)
    clave = db.Column(db.String(50), unique=True)
    titulo = db.Column(db.String(150), nullable=True)
    texto = db.Column(db.Text)
    link = db.Column(db.String(255), nullable=True)
    botones_json = db.Column(db.Text, nullable=True)
    tipo_nodo = db.Column(db.String(20), default='botones', nullable=False)  # botones | lista | documento | accion
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=True, index=True)
    nodo_padre = db.Column(db.String(50), nullable=True)
    texto_boton_lista = db.Column(db.String(30), default='Ver opciones')
    documento_url = db.Column(db.String(500), nullable=True)
    documento_nombre = db.Column(db.String(120), nullable=True)

    @staticmethod
    def seed():
        """Contenido inicial del menú (colegio + instituto + alumnos). Todo se edita desde /admin/editor.
        Solo crea los nodos que no existen: nunca pisa lo que ya editaste."""
        import json
        bot, empresa = Config.BOT_NAME, Config.COMPANY_NAME

        def b(*items):
            return json.dumps([{"id": i, "title": t} for i, t in items], ensure_ascii=False)

        pie = lambda back: b((back, "⬅️ Volver"), ("acc_asesor", "📅 Agendar cita"), ("CMD_MAIN_MENU", "🏠 Menú Principal"))
        datos = [
            # clave, título, texto, link, botones
            ('bienvenida', '👋 Bienvenida (tras validar ID)',
             '¡Hola *{nombre}*! ✨\n\nGracias, ya validé tus datos en *{empresa}* 🙌\n\n¿En qué puedo ayudarte hoy?',
             '', b(('m_gest', '📚 Informes'), ('m_segu', '🎓 Soy estudiante'), ('m_bene', '📍 Contáctanos'))),
            ('inicio', '🏠 Menú principal',
             f'¡Hola *{{nombre}}*! ✨\n\nSoy *{bot}*, tu asistente virtual de *{empresa}*.\n\nElige una opción o escríbeme tu consulta directamente:',
             '', b(('m_gest', '📚 Informes'), ('m_segu', '🎓 Soy estudiante'), ('m_bene', '📍 Contáctanos'))),

            # --- Rama 1: Informes (2 Colegios: Comas y Ventanilla + Instituto Pedagógico) ---
            ('m_gest', '📚 Informes y Vacantes',
             '📚 *Informes y Vacantes 2026*\n\nContamos con *2 sedes de Colegio* (*Comas* y *Ventanilla*) y nuestro *Instituto Pedagógico* (inicio cada semestre · vacantes limitadas).\n\n¿Sobre cuál deseas información?', '',
             b(('v_colegio', '🏫 Colegios (2 sedes)'), ('v_instituto', '🎓 Instituto'), ('f_matricula', '📝 Matrícula'))),

            ('v_colegio', '🏫 Colegios (Comas y Ventanilla)',
             '🏫 *Nuestros Colegios*\n\nCada sede cuenta con infraestructura propia, talleres y pensiones diferenciadas por nivel.\n¿Qué sede deseas consultar?', '',
             b(('v_col_comas', '🏫 Sede Comas'), ('v_col_ventanilla', '🏫 Sede Ventanilla'), ('m_gest', '⬅️ Volver'))),

            # --- Colegio Sede Comas ---
            ('v_col_comas', '🏫 Colegio · Sede Comas',
             '🏫 *Colegio — Sede Comas*\n\n📍 Av. Universitaria Norte 1450, Comas\n✨ Inglés intensivo, robótica, laboratorio de ciencias y talleres deportivos.\n🔥 *Vacantes limitadas 2026*\n\nElige el nivel para ver precios y horarios:', '',
             b(('v_comas_inicial', '🧸 Inicial Comas'), ('v_comas_primaria', '📘 Primaria Comas'), ('v_comas_secundaria', '📗 Secundaria Comas'))),
            ('v_comas_inicial', 'Comas · Nivel Inicial',
             '🧸 *Colegio Comas — Nivel Inicial (3, 4 y 5 años)*\n\n💰 Matrícula: *S/ 420*\n💳 Pensión mensual (10 cuotas): *S/ 420*\n🕒 Horario: 08:15 a 13:15\n🎨 Talleres incluidos: Psicomotricidad, Música e Inglés lúdico.\n🔥 *Vacantes limitadas.*', '', pie('v_col_comas')),
            ('v_comas_primaria', 'Comas · Nivel Primaria',
             '📘 *Colegio Comas — Nivel Primaria (1° a 6° grado)*\n\n💰 Matrícula: *S/ 480*\n💳 Pensión mensual (10 cuotas): *S/ 480*\n🕒 Horario: 07:45 a 14:15\n🤖 Talleres incluidos: Robótica, Inglés intensivo, Ajedrez y Deportes.\n🔥 *Vacantes limitadas.*', '', pie('v_col_comas')),
            ('v_comas_secundaria', 'Comas · Nivel Secundaria',
             '📗 *Colegio Comas — Nivel Secundaria (1° a 5° año)*\n\n💰 Matrícula: *S/ 530*\n💳 Pensión mensual (10 cuotas): *S/ 530*\n🕒 Horario: 07:45 a 14:45\n🎯 Preparación preuniversitaria, Inglés certificable y Computación.\n🔥 *Vacantes limitadas.*', '', pie('v_col_comas')),

            # --- Colegio Sede Ventanilla ---
            ('v_col_ventanilla', '🏫 Colegio · Sede Ventanilla',
             '🏫 *Colegio — Sede Ventanilla*\n\n📍 Av. Néstor Gambetta Km 14.5, Ventanilla\n✨ Campus amplio, laboratorios modernos, inglés diario y escuela para padres.\n🔥 *Vacantes limitadas 2026*\n\nElige el nivel para ver precios y horarios:', '',
             b(('v_vent_inicial', '🧸 Inicial Vent.'), ('v_vent_primaria', '📘 Primaria Vent.'), ('v_vent_secundaria', '📗 Secund. Vent.'))),
            ('v_vent_inicial', 'Ventanilla · Nivel Inicial',
             '🧸 *Colegio Ventanilla — Nivel Inicial (3, 4 y 5 años)*\n\n💰 Matrícula: *S/ 380*\n💳 Pensión mensual (10 cuotas): *S/ 380*\n🕒 Horario: 08:15 a 13:15\n🎨 Estimulación temprana, Inglés y Arte.\n🔥 *Vacantes limitadas.*', '', pie('v_col_ventanilla')),
            ('v_vent_primaria', 'Ventanilla · Nivel Primaria',
             '📘 *Colegio Ventanilla — Nivel Primaria (1° a 6° grado)*\n\n💰 Matrícula: *S/ 430*\n💳 Pensión mensual (10 cuotas): *S/ 430*\n🕒 Horario: 07:45 a 14:15\n🤖 Robótica, Inglés, Danza y Deportes.\n🔥 *Vacantes limitadas.*', '', pie('v_col_ventanilla')),
            ('v_vent_secundaria', 'Ventanilla · Nivel Secundaria',
             '📗 *Colegio Ventanilla — Nivel Secundaria (1° a 5° año)*\n\n💰 Matrícula: *S/ 470*\n💳 Pensión mensual (10 cuotas): *S/ 470*\n🕒 Horario: 07:45 a 14:45\n🎯 Orientación vocacional, Pre-U y Laboratorios.\n🔥 *Vacantes limitadas.*', '', pie('v_col_ventanilla')),

            # --- Instituto Pedagógico (3 carreras, inicio cada semestre, vacantes limitadas) ---
            ('v_instituto', '🎓 Instituto Pedagógico',
             '🎓 *Instituto Pedagógico*\n\n📅 *Inicio de clases: cada semestre (Marzo y Agosto)*\n🔥 *¡Vacantes limitadas por carrera!*\n🏅 Título profesional a nombre de la Nación (5 años / 10 ciclos).\n\nElige la carrera de tu interés:', '',
             b(('v_inst_inicial', '🧸 Educ. Inicial'), ('v_inst_primaria', '📘 Educ. Primaria'), ('v_inst_fisica', '🏃 Educ. Física'))),
            ('v_inst_inicial', 'Instituto · Educación Inicial',
             '🧸 *Carrera: Educación Inicial*\n\n📅 Inicio: *Cada semestre (Marzo / Agosto)* · 🔥 *Vacantes limitadas*\n⏱️ Duración: 10 ciclos (5 años) · Turnos Mañana y Tarde\n💰 Matrícula semestral: *S/ 250*\n💳 Pensión mensual (5 cuotas por ciclo): *S/ 460*\n📋 Requisitos: DNI vigente y certificado de secundaria completa.', '', pie('v_instituto')),
            ('v_inst_primaria', 'Instituto · Educación Primaria',
             '📘 *Carrera: Educación Primaria*\n\n📅 Inicio: *Cada semestre (Marzo / Agosto)* · 🔥 *Vacantes limitadas*\n⏱️ Duración: 10 ciclos (5 años) · Turnos Mañana y Tarde\n💰 Matrícula semestral: *S/ 250*\n💳 Pensión mensual (5 cuotas por ciclo): *S/ 460*\n📋 Requisitos: DNI vigente y certificado de secundaria completa.', '', pie('v_instituto')),
            ('v_inst_fisica', 'Instituto · Educación Física',
             '🏃 *Carrera: Educación Física*\n\n📅 Inicio: *Cada semestre (Marzo / Agosto)* · 🔥 *Vacantes limitadas*\n⏱️ Duración: 10 ciclos (5 años) · Prácticas deportivas desde el 1er ciclo\n💰 Matrícula semestral: *S/ 250*\n💳 Pensión mensual (5 cuotas por ciclo): *S/ 490*\n📋 Requisitos: DNI vigente, certificado de secundaria y evaluación física básica.', '', pie('v_instituto')),

            ('f_matricula', '📝 Proceso de Matrícula',
             '📝 *Proceso de Matrícula y Requisitos*\n\n1️⃣ Elige sede/nivel o carrera.\n2️⃣ Presenta copia de DNI (alumno y apoderado) y libreta/certificado de estudios.\n3️⃣ Realiza el pago de matrícula y envía tu comprobante por este mismo WhatsApp.\n\n🔥 *Vacantes limitadas.* ¿Deseas agendar una cita o llamada?', '', pie('m_gest')),

            # --- Rama 2: Soy Estudiante ---
            ('m_segu', '🎓 Servicios para Estudiantes',
             '🎓 *Portal del Estudiante*\n\nConsulta tus *Notas*, *Cuotas de pago*, *Comunicados* o envía tu *Comprobante de pago*:', '',
             b(('acc_alumno', '🔍 Consultar con DNI'), ('comp_enviar', '📤 Enviar comprobante'), ('CMD_MAIN_MENU', '🏠 Menú Principal'))),
            ('acc_alumno', '🎓 Portal del Alumno (DNI)',
             '🎓 *Portal del Alumno*\n\nEscribe tu número de *DNI* (solo números) para ver tus periodos matriculados y consultar *Notas*, *Cuotas de pago* o *Comunicados*.', '', None),
            ('acc_deudas', '💳 Consulta de pagos', '💳 *Consulta de pagos*\n\nTe haré unas preguntas rápidas. Escribe *cancelar* en cualquier momento para salir.', '', None),
            ('acc_notas', '📝 Consulta de notas', '📝 *Consulta de notas*\n\nTe haré unas preguntas rápidas. Escribe *cancelar* en cualquier momento para salir.', '', None),
            ('comp_enviar', '📤 Envío de comprobante', '📤 *Envío de comprobante de pago*\n\nEl área contable lo revisará y te avisaremos por este chat.', '', None),

            # --- Rama 3: Contáctanos y Citas ---
            ('m_bene', '📍 Contáctanos',
             '📍 *Contáctanos*\n\n¿Qué información necesitas o prefieres agendar una cita/llamada?', '',
             b(('f_horarios', '🕒 Horarios'), ('f_ubicacion', '📍 Ubicaciones'), ('acc_asesor', '📅 Cita / Asesor'))),
            ('f_horarios', '🕒 Horarios de atención',
             '🕒 *Horarios de Atención*\n\n• *Lunes a Viernes:* 08:00 a 18:00\n• *Sábados:* 09:00 a 13:00\n\nSi nos escribes fuera de horario, puedes dejar tu *cita o llamada programada* y te contactamos al día siguiente.', '', pie('m_bene')),
            ('f_ubicacion', '📍 Ubicaciones de Sedes',
             '📍 *Nuestras Sedes*\n\n🏫 *Colegio Comas:* Av. Universitaria Norte 1450, Comas.\n🏫 *Colegio Ventanilla:* Av. Néstor Gambetta Km 14.5, Ventanilla.\n🎓 *Instituto Pedagógico:* Sede Central Lima Norte.\n\n¿Deseas agendar una visita guiada?', '', pie('m_bene')),
            ('f_asesor', '🙋 Hablar con un asesor',
             f'🙋 *Atención Personalizada*\n\nPulsa *Agendar cita* para coordinar una llamada, visita guiada o hablar con un asesor de *{empresa}*.', '',
             b(('acc_asesor', '📅 Agendar cita'), ('CMD_MAIN_MENU', '🏠 Menú Principal'))),
        ]
        for clave, titulo, texto, link, botones in datos:
            if not RespuestaBot.query.filter_by(clave=clave).first():
                db.session.add(RespuestaBot(clave=clave, titulo=titulo, texto=texto, link=link, botones_json=botones))
        db.session.commit()


class Colaborador(db.Model):
    """Base de personas autorizadas (validación por DNI/ID). Se carga por Excel o a mano."""
    __tablename__ = 'colaboradores'
    id = db.Column(db.Integer, primary_key=True)
    dni = db.Column(db.String(20), unique=True, index=True)
    nombre = db.Column(db.String(100))
    empresa = db.Column(db.String(100))
    cargo = db.Column(db.String(100))
    estado = db.Column(db.String(50))


class AuditoriaAdmin(db.Model):
    """Registro de auditoría de acciones administrativas críticas en el panel web."""
    __tablename__ = 'auditoria_admin'
    id = db.Column(db.Integer, primary_key=True)
    usuario = db.Column(db.String(60), nullable=False, index=True)
    accion = db.Column(db.String(60), nullable=False, index=True)
    detalle = db.Column(db.String(400), default='')
    ip = db.Column(db.String(60), default='')
    creado_en = db.Column(db.DateTime, default=utcnow, index=True)

    @staticmethod
    def registrar(usuario, accion, detalle="", ip=""):
        try:
            db.session.add(AuditoriaAdmin(
                usuario=str(usuario or 'sistema')[:60],
                accion=str(accion)[:60],
                detalle=str(detalle or '')[:400],
                ip=str(ip or '')[:60],
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()


class UsuarioSistema(db.Model):
    """Usuarios del panel web."""
    __tablename__ = 'usuarios_sistema'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    nombre_completo = db.Column(db.String(100), nullable=True)
    rol = db.Column(db.String(20), nullable=False, default='lector')  # 'admin' o 'lector'
    activo = db.Column(db.Boolean, default=True, nullable=False)
    en_linea = db.Column(db.Boolean, default=True, nullable=False)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=True, index=True)
    max_chats = db.Column(db.Integer, default=10, nullable=False)
    intentos_fallidos = db.Column(db.Integer, default=0, nullable=False)
    bloqueado_hasta = db.Column(db.DateTime, nullable=True)
    ultimo_login = db.Column(db.DateTime, nullable=True)
    creado_en = db.Column(db.DateTime, default=utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def esta_bloqueado(self):
        return bool(self.bloqueado_hasta and self.bloqueado_hasta > utcnow())

    def registrar_fallo(self):
        self.intentos_fallidos = (self.intentos_fallidos or 0) + 1
        if self.intentos_fallidos >= Config.LOGIN_MAX_ATTEMPTS:
            self.bloqueado_hasta = utcnow() + timedelta(minutes=Config.LOGIN_LOCK_MINUTES)
        db.session.commit()

    def registrar_exito(self):
        self.intentos_fallidos = 0
        self.bloqueado_hasta = None
        self.ultimo_login = utcnow()
        db.session.commit()

    @staticmethod
    def seed_admin():
        """Crea el admin inicial desde ADMIN_USER/ADMIN_PASS (.env) si no existe.
        Nunca pisa la contraseña de un usuario existente (para eso: manage.py reset-admin)."""
        username = Config.ADMIN_USER.strip().lower()
        if UsuarioSistema.query.filter_by(username=username).first():
            return False
        admin = UsuarioSistema(username=username, nombre_completo=Config.ADMIN_NAME, rol='admin', activo=True)
        admin.set_password(Config.ADMIN_PASS)
        db.session.add(admin)
        db.session.commit()
        return True
