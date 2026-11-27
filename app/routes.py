import hashlib
import hmac
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import wraps
from io import BytesIO

from flask import (Blueprint, Response, current_app, flash, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from flask_limiter.util import get_remote_address
from sqlalchemy import func, text
from werkzeug.security import check_password_hash

from config import Config
from . import csrf, endpoints_client as ep_cli, limiter
from .bot_logic import brain_gethi
from .models import (Ajuste, ApiEndpoint, AuditoriaAdmin, Cita, Colaborador, Comprobante, Empresa, IaUso, Lead, LeadEvento,
                     MaterialComercial, Mensaje, PlantillaRapida, PreguntaPendiente, ReglaCampana, RespuestaBot,
                     Usuario, UsuarioSistema, WebhookSaliente, db, utcnow)
from .services import (enviar_documento, enviar_imagen, enviar_plantilla, enviar_texto, enviar_video,
                       registrar_msg, subir_media)
from .webhooks_out import disparar_evento
from .timeutil import to_local

logger = logging.getLogger(__name__)

main = Blueprint('main', __name__)
# Pool para procesar webhooks sin bloquear la respuesta 200 a Meta
executor = ThreadPoolExecutor(max_workers=Config.WEBHOOK_WORKERS)

MENSAJES_LOGIN = {'desactivada': 'Tu cuenta ha sido desactivada o eliminada.'}
MAX_CHAT_MENSAJES = 300

# --- 0. HEALTHCHECK ---
@main.route('/health')
@limiter.exempt
def health():
    try:
        db.session.execute(text('SELECT 1'))
        return jsonify(status='ok'), 200
    except Exception:
        db.session.rollback()
        return jsonify(status='db_error'), 503


# --- 1. WEBHOOK (VERIFICACIÓN) ---
@main.route('/webhook', methods=['GET'])
@csrf.exempt
@limiter.exempt
def verify():
    token = (request.args.get('hub.verify_token') or '').encode()
    if request.args.get('hub.mode') == 'subscribe' and hmac.compare_digest(token, Config.WEBHOOK_TOKEN.encode()):
        return Response(request.args.get('hub.challenge', ''), mimetype='text/plain')
    return "Error de token", 403


def _firma_valida(raw_body, header):
    """Valida X-Hub-Signature-256 = sha256=HMAC(APP_SECRET, cuerpo crudo)."""
    if not header or not header.startswith('sha256='):
        return False
    esperado = hmac.new(Config.APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado.encode(), header.split('=', 1)[1].strip().encode())


def _extraer_mensaje(msg):
    """Devuelve (tipo, texto_para_BD, id_de_boton, media). `media` solo para imagen/documento."""
    tipo = msg.get('type', '')
    if tipo == 'text':
        return 'text', msg.get('text', {}).get('body', ''), '', None
    if tipo == 'interactive':
        inter = msg.get('interactive', {})
        if inter.get('type') == 'button_reply':
            r = inter.get('button_reply', {})
            return 'interactive', f"Botón: {r.get('title', '')}", r.get('id', ''), None
        if inter.get('type') == 'list_reply':
            r = inter.get('list_reply', {})
            return 'interactive', f"Lista: {r.get('title', '')}", r.get('id', ''), None
    if tipo == 'button':  # respuesta rápida de una plantilla
        b = msg.get('button', {})
        return 'interactive', f"Botón: {b.get('text', '')}", b.get('payload', ''), None
    if tipo in ('image', 'document'):
        m = msg.get(tipo, {})
        media = {'id': m.get('id', ''), 'mime': m.get('mime_type', ''),
                 'filename': m.get('filename', ''), 'caption': m.get('caption', '')}
        return tipo, f"[{tipo}{': ' + media['filename'] if media['filename'] else ''}]", '', media
    if tipo == 'audio':
        m = msg.get('audio', {})
        media = {'id': m.get('id', ''), 'mime': m.get('mime_type', 'audio/ogg'),
                 'voice': bool(m.get('voice', True))}
        return 'audio', '[Nota de voz]', '', media
    return tipo or 'unknown', f"[{tipo or 'mensaje no soportado'}]", '', None


def _para_historial(txt):
    """Enmascara DNIs/IDs escritos en el chat (****1234) para que no queden completos en el historial."""
    t = (txt or '').strip()
    if Config.MASK_ID_IN_LOGS and t.isdigit() and len(t) in Config.ID_LENGTHS:
        return f"{Config.ID_LABEL} ****{t[-4:]}"
    return txt


def _procesar(app, tel, txt, btn_id, nombre, tipo, media=None, referral=None):
    with app.app_context():
        lock_conn = None
        lock_key = None
        if Config.WEBHOOK_LOCK_POR_TELEFONO and db.engine.dialect.name == 'postgresql':
            try:
                # Bigint firmado (64 bits) a partir del sha256 del teléfono
                lock_key = int.from_bytes(hashlib.sha256(str(tel).encode()).digest()[:8], 'big', signed=True)
                lock_conn = db.engine.connect()
                lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_key})
            except Exception as e:
                logger.warning("No se pudo obtener lock por teléfono para %s: %s", tel, e)
                if lock_conn:
                    try:
                        lock_conn.close()
                    except Exception:
                        pass
                lock_conn = None
        try:
            brain_gethi(tel, txt, btn_id, nombre, tipo, media, referral=referral)
        except Exception:
            logger.exception("Error procesando mensaje de %s", tel)
            db.session.rollback()
        finally:
            db.session.remove()
            if lock_conn:
                try:
                    lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key})
                except Exception:
                    pass
                finally:
                    lock_conn.close()


# --- 2. WEBHOOK (RECEPCIÓN) ---
@main.route('/webhook', methods=['POST'])
@csrf.exempt
@limiter.exempt
def receive():
    raw = request.get_data()
    if not _firma_valida(raw, request.headers.get('X-Hub-Signature-256')):
        logger.warning("Webhook con firma inválida desde %s", get_remote_address())
        return "Firma inválida", 403

    data = request.get_json(silent=True) or {}
    app = current_app._get_current_object()
    hubo_error = False

    for entry in data.get('entry', []):
        for change in entry.get('changes', []):
            val = change.get('value', {})
            # Actualizar checks de entrega/lectura (sent, delivered, read, failed) devueltos por Meta
            for st in val.get('statuses', []):
                st_id = st.get('id')
                st_val = st.get('status')
                if st_id and st_val in ('sent', 'delivered', 'read', 'failed'):
                    m_out = Mensaje.query.filter_by(wa_id=st_id).first()
                    if m_out:
                        m_out.estado_envio = st_val
                        errs = st.get('errors') or []
                        if errs and isinstance(errs[0], dict):
                            m_out.error_meta = f"{errs[0].get('code', '')}: {errs[0].get('title', '')}"[:300]
                        db.session.commit()
            contactos = {c.get('wa_id'): (c.get('profile') or {}).get('name') for c in val.get('contacts', [])}
            for msg in val.get('messages', []):
                tel = msg.get('from')
                if not tel:
                    continue
                nombre = contactos.get(tel) or 'Usuario WA'
                tipo, txt, btn_id, media = _extraer_mensaje(msg)
                referral = msg.get('referral') if isinstance(msg.get('referral'), dict) else None
                resultado = registrar_msg(tel, nombre, _para_historial(txt), 'ENTRADA', wa_id=msg.get('id'))
                if resultado == 'dup':
                    continue  # reintento de Meta: ya procesado
                if resultado == 'error':
                    hubo_error = True  # 500 => Meta reintenta
                    continue
                executor.submit(_procesar, app, tel, txt, btn_id, nombre, tipo, media, referral)

    if hubo_error:
        return jsonify({"status": "error"}), 500
    return jsonify({"status": "ok"}), 200


# --- DECORADORES DE SEGURIDAD Y CONTROL DE ROLES (RBAC) ---
def _usuario_actual():
    """Usuario logueado y activo (verificado contra la BD en cada petición) o None."""
    uid = session.get('user_id')
    if not session.get('auth') or not uid:
        return None
    user = db.session.get(UsuarioSistema, uid)
    if not user or not user.activo:
        session.clear()
        return None
    return user


def _sin_sesion():
    if request.path.startswith('/api/'):
        return jsonify({"error": "Unauthorized"}), 401
    return redirect(url_for('main.login'))


ROLES = ('admin', 'supervisor', 'callcenter', 'lector', 'contabilidad')


def _home_para(rol):
    return {
        'admin': 'main.dashboard',
        'supervisor': 'main.leads',
        'callcenter': 'main.leads',
        'contabilidad': 'main.comprobantes',
    }.get(rol, 'main.monitor')


def roles_required(*roles):
    """Permite el acceso solo a los roles indicados (verificados contra la BD en cada petición)."""
    def deco(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user = _usuario_actual()
            if not user:
                return _sin_sesion()
            if roles and user.rol not in roles:
                if request.path.startswith('/api/'):
                    return jsonify({"error": "Forbidden"}), 403
                flash('Acceso restringido: tu perfil no tiene permiso para esa sección.', 'warning')
                return redirect(url_for(_home_para(user.rol)))
            g.user = user
            return f(*args, **kwargs)
        return decorated_function
    return deco


login_required = roles_required()                                    # cualquier usuario autenticado
admin_required = roles_required('admin')
supervisor_required = roles_required('admin', 'supervisor')          # whisper, takeover, KPIs, broadcast
agente_required = roles_required('admin', 'supervisor', 'callcenter')  # atender leads, citas, chats y pausar bot
monitor_required = roles_required('admin', 'supervisor', 'callcenter', 'lector')  # ver conversaciones
contabilidad_required = roles_required('admin', 'contabilidad')


# --- 3. LOGIN ROBUSTO (ANTI-HACKERS) Y RUTAS BASE ---
@main.route('/')
def index():
    if session.get('auth'):
        return redirect(url_for(_home_para(session.get('user_role'))))
    return redirect(url_for('main.login'))

@main.route('/login', methods=['GET', 'POST'])
@limiter.limit(lambda: Config.LOGIN_RATE_LIMIT)
def login():
    if session.get('auth'):
        return redirect(url_for(_home_para(session.get('user_role'))))

    error = None
    warning = None

    if request.method == 'POST':
        username = (request.form.get('u') or '').strip().lower()
        password = request.form.get('p') or ''

        if not username or not password:
            error = "Por favor, ingresa tu usuario y contraseña."
            return render_template('login.html', error=error)

        user = UsuarioSistema.query.filter_by(username=username).first()

        # Mitigación de Timing Attacks (comparación constante)
        dummy_hash = 'scrypt:32768:8:1$dummy$00000000000000000000000000000000'

        if not user:
            check_password_hash(dummy_hash, password)
            logger.warning(f"Intento fallido de login con usuario inexistente '{username}' desde IP {get_remote_address()}")
            error = "Credenciales incorrectas. Verifica tu usuario y contraseña."
            return render_template('login.html', error=error)

        # Verificar si la cuenta está desactivada por el administrador
        if not user.activo:
            error = "Esta cuenta ha sido desactivada por el Administrador."
            return render_template('login.html', error=error)

        # Verificar si la cuenta está temporalmente bloqueada por ataques de fuerza bruta
        if user.esta_bloqueado():
            minutos = max(1, int((user.bloqueado_hasta - utcnow()).total_seconds() / 60))
            warning = f"Cuenta temporalmente bloqueada por exceso de intentos fallidos. Intenta nuevamente en {minutos} minuto(s)."
            logger.warning(f"Acceso denegado por bloqueo temporal a '{username}' desde IP {get_remote_address()}")
            return render_template('login.html', warning=warning)

        # Validación segura de contraseña
        if user.check_password(password):
            user.registrar_exito()
            session.clear()
            session['auth'] = True
            session['user_id'] = user.id
            session['username'] = user.username
            session['user_role'] = user.rol
            session['user_name'] = user.nombre_completo or user.username
            session.permanent = True
            
            logger.info(f"Login exitoso: '{user.username}' (Rol: {user.rol}) desde IP {get_remote_address()}")

            return redirect(url_for(_home_para(user.rol)))
        else:
            user.registrar_fallo()
            logger.warning(f"Contraseña errónea para '{username}'. Intentos fallidos: {user.intentos_fallidos} desde IP {get_remote_address()}")
            
            if user.esta_bloqueado():
                warning = f"Demasiados intentos fallidos. Por seguridad, tu cuenta ha sido bloqueada por {Config.LOGIN_LOCK_MINUTES} minutos."
                return render_template('login.html', warning=warning)
            else:
                restantes = max(0, Config.LOGIN_MAX_ATTEMPTS - user.intentos_fallidos)
                if restantes <= 2:
                    error = f"Credenciales incorrectas. Te quedan {restantes} intento(s) antes del bloqueo automático."
                else:
                    error = "Credenciales incorrectas. Verifica tu usuario y contraseña."
                return render_template('login.html', error=error)

    error = MENSAJES_LOGIN.get(request.args.get('error', ''))

    return render_template('login.html', error=error, warning=warning)

# --- 4. PANEL EJECUTIVO (DASHBOARD) ---
@main.route('/dashboard')
@admin_required
def dashboard():
    total_colaboradores = Colaborador.query.count()
    empresas = (db.session.query(Colaborador.empresa, func.count(Colaborador.id))
                .group_by(Colaborador.empresa).order_by(func.count(Colaborador.id).desc()).limit(8).all())
    total_empresas = db.session.query(func.count(func.distinct(Colaborador.empresa))).scalar() or 0

    total_usuarios = Usuario.query.count()
    total_validados = Usuario.query.filter(Usuario.dni.isnot(None)).count()
    porcentaje_validados = round((total_validados / total_usuarios * 100) if total_usuarios else 0, 1)

    total_mensajes = Mensaje.query.count()
    total_entradas = Mensaje.query.filter_by(direccion='ENTRADA').count()
    total_salidas = Mensaje.query.filter_by(direccion='SALIDA').count()

    # Opciones del menú más pulsadas (según el título del botón)
    top = (db.session.query(Mensaje.contenido, func.count(Mensaje.id))
           .filter(Mensaje.direccion == 'ENTRADA', Mensaje.contenido.like('Botón: %'))
           .group_by(Mensaje.contenido).order_by(func.count(Mensaje.id).desc()).limit(8).all())
    topicos_labels = [c.replace('Botón: ', '', 1) for c, _ in top]
    topicos_data = [n for _, n in top]

    contactos = (db.session.query(Mensaje.telefono, func.max(Mensaje.nombre).label('nombre'),
                                  func.max(Mensaje.fecha).label('ultima_fecha'))
                 .filter(Mensaje.nombre != Config.bot_sender())
                 .group_by(Mensaje.telefono).order_by(func.max(Mensaje.fecha).desc()).limit(10).all())
    # Optimización N+1: consultas en bloque para los 10 contactos recientes
    tels_rec = [c.telefono for c in contactos]
    usuarios_map = {u.telefono: u for u in (Usuario.query.filter(Usuario.telefono.in_(tels_rec)).all() if tels_rec else [])}
    dnis_rec = [u.dni for u in usuarios_map.values() if u.dni]
    colabs_map = {col.dni: col.empresa for col in (Colaborador.query.filter(Colaborador.dni.in_(dnis_rec)).all() if dnis_rec else [])}
    emp_ids = [u.empresa_id for u in usuarios_map.values() if u.empresa_id]
    emps_map = {e.id: e.nombre_comercial for e in (Empresa.query.filter(Empresa.id.in_(emp_ids)).all() if emp_ids else [])}
    sub_last = (db.session.query(Mensaje.telefono, func.max(Mensaje.id).label('max_id'))
                .filter(Mensaje.telefono.in_(tels_rec)).group_by(Mensaje.telefono).subquery()) if tels_rec else None
    last_msgs = (
        {m.telefono: m.contenido for m in Mensaje.query.join(sub_last, Mensaje.id == sub_last.c.max_id).all()}
        if sub_last is not None else {}
    )
    ultimos_chats = []
    for c in contactos:
        usu = usuarios_map.get(c.telefono)
        empresa = "General"
        if usu and usu.empresa_id and usu.empresa_id in emps_map:
            empresa = emps_map[usu.empresa_id]
        elif usu and usu.dni and colabs_map.get(usu.dni):
            empresa = colabs_map[usu.dni]
        ultimos_chats.append({
            "nombre": c.nombre or c.telefono,
            "telefono": c.telefono,
            "empresa": empresa,
            "ultimo_msg": last_msgs.get(c.telefono) or "Sin mensajes",
            "hora": to_local(c.ultima_fecha),
        })

    kpis_evo = _calcular_kpis()
    brechas_pendientes = PreguntaPendiente.query.filter_by(estado='pendiente').count()

    return render_template('dashboard.html',
                           total_colaboradores=total_colaboradores,
                           total_empresas=total_empresas,
                           empresas_labels=[e or 'Sin empresa' for e, _ in empresas],
                           empresas_data=[n for _, n in empresas],
                           total_usuarios=total_usuarios,
                           total_validados=total_validados,
                           porcentaje_validados=porcentaje_validados,
                           total_mensajes=total_mensajes,
                           total_entradas=total_entradas,
                           total_salidas=total_salidas,
                           topicos_labels=topicos_labels,
                           topicos_data=topicos_data,
                           ultimos_chats=ultimos_chats,
                           kpis_evo=kpis_evo,
                           brechas_pendientes=brechas_pendientes)


# --- 4.1 MONITOR EN VIVO & CHATS ---
def _contactos_query():
    return (db.session.query(Mensaje.telefono, func.max(Mensaje.nombre).label('nombre'),
                             func.max(Mensaje.fecha).label('ultima_fecha'))
            .filter(Mensaje.nombre != Config.bot_sender())
            .group_by(Mensaje.telefono).order_by(func.max(Mensaje.fecha).desc()).limit(200).all())


def _mensajes_de(tel):
    ult = Mensaje.query.filter_by(telefono=tel).order_by(Mensaje.fecha.desc()).limit(MAX_CHAT_MENSAJES).all()
    return list(reversed(ult))


@main.route('/monitor')
@main.route('/monitor/<tel>')
@main.route('/dashboard/<tel>')
@monitor_required
def monitor(tel=None):
    mensajes = _mensajes_de(tel) if tel else []
    return render_template('monitor.html', contactos=_contactos_query(), mensajes=mensajes, tel_activo=tel)


@main.route('/api/chat_updates')
@main.route('/api/chat_updates/<tel>')
@limiter.exempt
@monitor_required
def chat_updates(tel=None):
    rows = _contactos_query()
    tels = [c.telefono for c in rows]
    estados_map = {
        u.telefono: (u.estado_chat or 'bot', u.asignado_a or '')
        for u in (Usuario.query.filter(Usuario.telefono.in_(tels)).all() if tels else [])
    }
    contactos_list = [
        {
            "tel": c.telefono,
            "nom": c.nombre or c.telefono,
            "ultima_fecha": to_local(c.ultima_fecha, '%H:%M'),
            "estado_chat": estados_map.get(c.telefono, ('bot', ''))[0],
            "asignado_a": estados_map.get(c.telefono, ('bot', ''))[1],
        }
        for c in rows
    ]
    mensajes_list = []
    bot_pausado_val = False
    estado_chat = 'bot'
    asignado_a = ''
    handoff_brief = None
    ventana_24h_activa = False
    ventana_24h_min = 0
    opt_out = False
    if tel and tel != 'none':
        mensajes_list = [
            {
                "id": m.id,
                "direccion": m.direccion,
                "es_interno": bool(getattr(m, 'es_interno', False)),
                "estado_envio": getattr(m, 'estado_envio', None) or '',
                "error_meta": getattr(m, 'error_meta', None) or '',
                "media_tipo": getattr(m, 'media_tipo', None) or '',
                "media_url": getattr(m, 'media_url', None) or '',
                "nombre": m.nombre if (m.direccion == 'ENTRADA' or getattr(m, 'es_interno', False)) else Config.bot_sender(),
                "contenido": m.contenido,
                "fecha": to_local(m.fecha, '%H:%M'),
            } for m in _mensajes_de(tel)
        ]
        usu = Usuario.query.filter_by(telefono=tel).first()
        bot_pausado_val = bool(usu and usu.bot_pausado)
        if usu:
            estado_chat = usu.estado_chat or 'bot'
            asignado_a = usu.asignado_a or ''
            opt_out = bool(getattr(usu, 'opt_out', False))
            ref_in = getattr(usu, 'ultimo_msg_entrada', None) or usu.ultima_vez
            if ref_in:
                restante = (ref_in + timedelta(hours=24) - utcnow()).total_seconds()
                if restante > 0:
                    ventana_24h_activa = True
                    ventana_24h_min = int(restante // 60)
        lead = Lead.query.filter_by(telefono=tel).order_by(Lead.id.desc()).first()
        if lead:
            handoff_brief = {
                "lead_id": lead.id,
                "nombre": lead.nombre or '',
                "empresa": lead.sede_nombre or '',
                "programa": lead.nivel or '',
                "origen": lead.origen or 'organico',
                "campana": lead.campana_nombre or '',
                "score": lead.score or 'frio',
                "tags": lead.tags or '',
                "resumen": lead.resumen_ia or lead.comentario or '',
            }
    return jsonify({
        "contactos": contactos_list,
        "mensajes": mensajes_list,
        "bot_pausado": bot_pausado_val,
        "estado_chat": estado_chat,
        "asignado_a": asignado_a,
        "handoff_brief": handoff_brief,
        "ventana_24h_activa": ventana_24h_activa,
        "ventana_24h_min": ventana_24h_min,
        "opt_out": opt_out,
    })


def _importar_excel(file_storage, max_filas=50000):
    """Importa/actualiza colaboradores desde un .xlsx (columnas: DNI, Nombre, Empresa, Cargo, [Estado])."""
    import openpyxl
    wb = openpyxl.load_workbook(file_storage, read_only=True, data_only=True)
    filas = wb.active.iter_rows(values_only=True)
    try:
        encabezado = next(filas)
    except StopIteration:
        raise ValueError("El archivo está vacío")
    cols = {str(c).strip().lower(): i for i, c in enumerate(encabezado) if c is not None}
    faltan = [c for c in ('dni', 'nombre', 'empresa', 'cargo') if c not in cols]
    if faltan:
        raise ValueError("Faltan columnas requeridas (DNI, Nombre, Empresa, Cargo)")

    def val(fila, nombre):
        i = cols.get(nombre)
        v = fila[i] if i is not None and i < len(fila) else None
        return '' if v is None else str(v).strip()

    existentes = {c.dni: c for c in Colaborador.query.all()}
    count = 0
    for fila in filas:
        if count >= max_filas:
            break
        dni = val(fila, 'dni')
        if dni.endswith('.0'):
            dni = dni[:-2]
        if not dni or dni.lower() in ('nan', 'none'):
            continue
        if dni.isdigit() and len(dni) == 7:      # Excel pierde el cero inicial
            dni = dni.zfill(8)
        dni = dni[:20]
        colab = existentes.get(dni)
        if not colab:
            colab = Colaborador(dni=dni)
            db.session.add(colab)
            existentes[dni] = colab
        colab.nombre = val(fila, 'nombre')[:100]
        colab.empresa = val(fila, 'empresa')[:100]
        colab.cargo = (val(fila, 'cargo') or '-')[:100]
        estado = val(fila, 'estado')[:50]
        colab.estado = estado or colab.estado or 'Activo'
        count += 1
    db.session.commit()
    return count


@main.route('/admin/colaboradores', methods=['GET', 'POST'])
@admin_required
def admin_colaboradores():
    
    if request.method == 'POST':
        action = request.form.get('action', 'upload_excel')
        
        # 1. GUARDAR O EDITAR COLABORADOR INDIVIDUAL
        if action == 'save_individual':
            colab_id = request.form.get('id', '').strip()
            dni_val = request.form.get('dni', '').strip()
            nombre_val = request.form.get('nombre', '').strip()
            empresa_val = request.form.get('empresa', '').strip()
            cargo_val = request.form.get('cargo', '').strip()
            estado_val = request.form.get('estado', 'Activo').strip()
            
            if not dni_val or not nombre_val:
                return redirect(url_for('main.admin_colaboradores', error="DNI y Nombre son obligatorios"))
                
            try:
                if colab_id: # Edición
                    colab = db.session.get(Colaborador, int(colab_id))
                    if not colab:
                        return redirect(url_for('main.admin_colaboradores', error="Colaborador no encontrado"))
                    # Verificar si el DNI fue cambiado y ya pertenece a otro
                    existente = Colaborador.query.filter_by(dni=dni_val).first()
                    if existente and existente.id != colab.id:
                        return redirect(url_for('main.admin_colaboradores', error="El DNI ya pertenece a otro colaborador"))
                else: # Nuevo
                    existente = Colaborador.query.filter_by(dni=dni_val).first()
                    if existente:
                        return redirect(url_for('main.admin_colaboradores', error=f"El DNI {dni_val} ya se encuentra registrado"))
                    colab = Colaborador()
                    db.session.add(colab)
                    
                colab.dni = dni_val
                colab.nombre = nombre_val
                colab.empresa = empresa_val
                colab.cargo = cargo_val
                colab.estado = estado_val
                db.session.commit()
                return redirect(url_for('main.admin_colaboradores', success_msg="Colaborador guardado correctamente"))
            except Exception as e:
                db.session.rollback()
                logger.exception("Error guardando colaborador")
                return redirect(url_for('main.admin_colaboradores', error="Error al guardar el colaborador"))
                
        # 2. ELIMINAR COLABORADOR INDIVIDUAL
        elif action == 'delete_individual':
            colab_id = request.form.get('id', '').strip()
            try:
                colab = db.session.get(Colaborador, int(colab_id))
                if colab:
                    db.session.delete(colab)
                    db.session.commit()
                    return redirect(url_for('main.admin_colaboradores', success_msg="Colaborador eliminado correctamente"))
                return redirect(url_for('main.admin_colaboradores', error="No se encontró el colaborador a eliminar"))
            except Exception as e:
                db.session.rollback()
                logger.exception("Error eliminando colaborador")
                return redirect(url_for('main.admin_colaboradores', error="Error al eliminar el colaborador"))

        # 3. CARGA MASIVA EXCEL
        else:
            file = request.files.get('archivo_excel')
            if not file or file.filename == '':
                return redirect(url_for('main.admin_colaboradores', error="No se seleccionó archivo"))
            if not file.filename.lower().endswith('.xlsx'):
                return redirect(url_for('main.admin_colaboradores', error="El archivo debe ser .xlsx"))
            try:
                count = _importar_excel(file)
                return redirect(url_for('main.admin_colaboradores',
                                        success_msg=f"Base de datos actualizada con éxito. Registros procesados: {count}"))
            except ValueError as e:
                db.session.rollback()
                return redirect(url_for('main.admin_colaboradores', error=str(e)))
            except Exception:
                db.session.rollback()
                logger.exception("Error importando Excel de colaboradores")
                return redirect(url_for('main.admin_colaboradores', error="No se pudo leer el archivo Excel"))

    # GET: Búsqueda, Filtrado y Paginación
    page = request.args.get('page', 1, type=int)
    q = request.args.get('q', '').strip()
    empresa_filtro = request.args.get('empresa', '').strip()
    
    query = Colaborador.query
    if q:
        search_pattern = f"%{q}%"
        query = query.filter(
            Colaborador.nombre.ilike(search_pattern) | 
            Colaborador.dni.ilike(search_pattern) | 
            Colaborador.cargo.ilike(search_pattern)
        )
    if empresa_filtro:
        query = query.filter(Colaborador.empresa == empresa_filtro)
        
    colaboradores_pag = query.order_by(Colaborador.id.desc()).paginate(page=page, per_page=15, error_out=False)
    
    total_colaboradores = Colaborador.query.count()
    total_validados = Usuario.query.filter(Usuario.dni.isnot(None)).count()
    empresas = [e for (e,) in db.session.query(Colaborador.empresa).distinct().order_by(Colaborador.empresa).all() if e]

    return render_template('colaboradores.html',
                           colaboradores_pag=colaboradores_pag,
                           total_colaboradores=total_colaboradores,
                           total_empresas=len(empresas),
                           empresas=empresas,
                           total_validados=total_validados,
                           q=q,
                           empresa_filtro=empresa_filtro)

@main.route('/admin/colaboradores/plantilla')
@admin_required
def descargar_plantilla_colaboradores():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Colaboradores"
    
    # Encabezados oficiales
    headers = ["DNI", "Nombre", "Empresa", "Cargo", "Estado"]
    ws.append(headers)
    
    # Filas de ejemplo con formato explícito de texto en DNI para preservar ceros iniciales
    ejemplos = [
        ["07845123", "Juan Pérez (ejemplo)", "Empresa A", "Analista", "Activo"],
        ["06123456", "María García (ejemplo)", "Empresa A", "Coordinadora", "Activo"],
        ["42643211", "Luis Torres (ejemplo)", "Empresa B", "Supervisor", "Activo"],
    ]
    for row in ejemplos:
        ws.append(row)
        
    # Estilos profesionales
    header_fill = PatternFill(start_color="00A884", end_color="00A884", fill_type="solid")
    header_font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
    data_font = Font(name="Segoe UI", size=10)
    thin_border = Border(
        left=Side(style='thin', color='D2D6DC'),
        right=Side(style='thin', color='D2D6DC'),
        top=Side(style='thin', color='D2D6DC'),
        bottom=Side(style='thin', color='D2D6DC')
    )
    
    # Estilizar encabezados
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border
        
    # Configurar formato de texto '@' explícito en toda la columna DNI (filas 2 a 1000)
    for r in range(2, 1000):
        ws.cell(row=r, column=1).number_format = '@'
        
    # Estilizar las filas de ejemplo
    for row in ws.iter_rows(min_row=2, max_row=len(ejemplos) + 1, min_col=1, max_col=len(headers)):
        for cell in row:
            cell.font = data_font
            cell.border = thin_border
            if cell.column == 1:
                cell.number_format = '@'
                cell.alignment = Alignment(horizontal="center")
            elif cell.column in [3, 5]:
                cell.alignment = Alignment(horizontal="center")
            else:
                cell.alignment = Alignment(horizontal="left")
                
    # Anchos de columna cómodos
    ws.column_dimensions['A'].width = 18
    ws.column_dimensions['B'].width = 38
    ws.column_dimensions['C'].width = 34
    ws.column_dimensions['D'].width = 30
    ws.column_dimensions['E'].width = 16
    
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="plantilla_colaboradores.xlsx"
    )

@main.route('/api/toggle_bot/<tel>', methods=['POST'])
@limiter.exempt
@agente_required
def toggle_bot(tel):
    usu = Usuario.query.filter_by(telefono=tel).first()
    if not usu:
        return jsonify({"error": "Usuario no encontrado"}), 404
    usu.bot_pausado = not bool(usu.bot_pausado)
    if usu.bot_pausado:
        usu.estado_chat = 'atendido'
        usu.asignado_a = usu.asignado_a or g.user.username
    else:
        usu.estado_chat = 'bot'
        usu.esperando_asesor_desde = None
        usu.esperando_usuario_desde = None
        usu.aviso_inactividad_enviado = False
    db.session.commit()
    return jsonify({"status": "success", "bot_pausado": usu.bot_pausado, "estado_chat": usu.estado_chat})


@main.route('/api/toggle_online', methods=['POST'])
@limiter.exempt
@agente_required
def toggle_online():
    g.user.en_linea = not bool(getattr(g.user, 'en_linea', True))
    db.session.commit()
    return jsonify({"status": "success", "en_linea": g.user.en_linea}), 200


@main.route('/api/send_message/<tel>', methods=['POST'])
@limiter.exempt
@agente_required
def send_message(tel):
    data = request.get_json(silent=True) or {}
    mensaje_texto = data.get('mensaje')
    if not isinstance(mensaje_texto, str) or not mensaje_texto.strip():
        return jsonify({"error": "Mensaje vacío"}), 400
    if len(mensaje_texto) > 4096:
        return jsonify({"error": "El mensaje supera los 4096 caracteres"}), 400

    resp = enviar_texto(tel, mensaje_texto.strip())   # enviar_api registra la SALIDA si Meta lo acepta
    if resp is None:
        return jsonify({"error": "No se pudo contactar con WhatsApp"}), 502
    if resp.status_code != 200:
        try:
            detalle = resp.json().get('error', {}).get('message', '')
        except ValueError:
            detalle = ''
        return jsonify({"error": f"WhatsApp rechazó el mensaje. {detalle}".strip()}), 502

    # Marcar atención humana activa y armar timer de inactividad del usuario + FRT del Lead
    ahora = utcnow()
    usu = Usuario.query.filter_by(telefono=tel).first()
    if usu:
        usu.bot_pausado = True
        usu.estado_chat = 'atendido'
        usu.asignado_a = g.user.username
        usu.esperando_asesor_desde = None
        usu.esperando_usuario_desde = ahora
        usu.aviso_inactividad_enviado = False
    lead = Lead.query.filter_by(telefono=tel).order_by(Lead.id.desc()).first()
    if lead and not lead.primera_respuesta_en:
        lead.primera_respuesta_en = ahora
        if not lead.asignado_a:
            lead.asignado_a = g.user.username
    db.session.commit()
    return jsonify({"status": "success"})


@main.route('/api/materiales')
@limiter.exempt
@agente_required
def api_materiales():
    items = MaterialComercial.query.filter_by(activo=True).order_by(MaterialComercial.id.desc()).all()
    emps = {e.id: e.nombre_comercial for e in Empresa.query.all()}
    return jsonify({
        "materiales": [
            {
                "id": m.id,
                "titulo": m.titulo,
                "tipo": m.tipo,
                "empresa": emps.get(m.empresa_id, "General"),
                "url": m.url,
                "caption": m.caption or "",
            }
            for m in items
        ]
    }), 200


@main.route('/api/send_media/<tel>', methods=['POST'])
@limiter.exempt
@agente_required
def api_send_media(tel):
    """Envía una imagen, video o PDF al contacto desde la biblioteca (material_id), archivo subido o URL HTTPS."""
    tipo = (request.form.get('tipo') or '').strip().lower()
    caption = (request.form.get('caption') or '').strip()[:1024]
    material_id = (request.form.get('material_id') or '').strip()
    url_directa = (request.form.get('url') or '').strip()
    archivo = request.files.get('archivo')

    if request.is_json:
        j = request.get_json(silent=True) or {}
        material_id = str(j.get('material_id') or '')
        tipo = str(j.get('tipo') or '').strip().lower()
        url_directa = str(j.get('url') or '').strip()
        caption = str(j.get('caption') or '').strip()[:1024]

    resp = None
    if material_id.isdigit():
        mat = db.session.get(MaterialComercial, int(material_id))
        if not mat:
            return jsonify({"error": "Material no encontrado"}), 404
        cap = caption or mat.caption or mat.titulo
        if mat.tipo == 'image':
            resp = enviar_imagen(tel, url=mat.url, caption=cap)
        elif mat.tipo == 'video':
            resp = enviar_video(tel, url=mat.url, caption=cap)
        else:
            resp = enviar_documento(tel, url=mat.url, filename=mat.filename or 'brochure.pdf', caption=cap)
    elif archivo and archivo.filename:
        mime = (archivo.mimetype or '').lower()
        data = archivo.read()
        if not data:
            return jsonify({"error": "Archivo vacío"}), 400
        if len(data) > 16 * 1024 * 1024:
            return jsonify({"error": "El archivo supera el máximo permitido por WhatsApp (16 MB)"}), 400
        media_id = subir_media(data, mime or 'application/octet-stream', archivo.filename)
        if not media_id:
            return jsonify({"error": "Meta Media API no aceptó el archivo"}), 502
        if mime.startswith('image/'):
            resp = enviar_imagen(tel, media_id=media_id, caption=caption)
        elif mime.startswith('video/'):
            resp = enviar_video(tel, media_id=media_id, caption=caption)
        else:
            resp = enviar_documento(tel, media_id=media_id, filename=archivo.filename, caption=caption)
    elif url_directa and url_directa.startswith(('http://', 'https://')):
        if tipo == 'video':
            resp = enviar_video(tel, url=url_directa, caption=caption)
        elif tipo == 'document':
            resp = enviar_documento(tel, url=url_directa, filename='documento.pdf', caption=caption)
        else:
            resp = enviar_imagen(tel, url=url_directa, caption=caption)
    else:
        return jsonify({"error": "Envía material_id, archivo o url HTTPS"}), 400

    if resp is None or resp.status_code != 200:
        return jsonify({"error": "WhatsApp rechazó el envío multimedia"}), 502
    return jsonify({"status": "success"}), 200


@main.route('/admin/leads/broadcast', methods=['POST'])
@admin_required
def leads_broadcast():
    """Campaña saliente HSM segmentada (respeta automáticamente opt_out=True)."""
    tpl = (request.form.get('template_name') or '').strip()[:100]
    idioma = (request.form.get('language') or 'es').strip()[:10]
    estado_f = (request.form.get('estado') or 'todos').strip()
    score_f = (request.form.get('score') or 'todos').strip()
    emp_f = (request.form.get('empresa_id') or '').strip()
    if not tpl:
        flash('Indica el nombre de la plantilla HSM aprobada por Meta.', 'danger')
        return redirect(url_for('main.leads'))

    q = Lead.query.filter(Lead.estado != 'descartado')
    if estado_f in Lead.ESTADOS:
        q = q.filter_by(estado=estado_f)
    if score_f in Lead.SCORES:
        q = q.filter_by(score=score_f)
    if emp_f.isdigit():
        q = q.filter_by(empresa_id=int(emp_f))

    leads_dest = q.limit(200).all()
    enviados, omitidos_optout, fallidos = 0, 0, 0
    vistos = set()
    for l in leads_dest:
        if l.telefono in vistos:
            continue
        vistos.add(l.telefono)
        usu = Usuario.query.filter_by(telefono=l.telefono).first()
        if usu and getattr(usu, 'opt_out', False):
            omitidos_optout += 1
            continue
        r = enviar_plantilla(l.telefono, tpl, idioma=idioma)
        if r is not None and r.status_code == 200:
            enviados += 1
            db.session.add(LeadEvento(lead_id=l.id, tipo='hsm', detalle=f'Broadcast HSM: {tpl}', usuario=g.user.username))
        else:
            fallidos += 1
    db.session.commit()
    flash(f'Broadcast «{tpl}»: {enviados} enviados, {omitidos_optout} omitidos por opt-out, {fallidos} fallidos.', 'success' if enviados else 'warning')
    return redirect(url_for('main.leads'))


@main.route('/api/plantillas_rapidas/<tel>')
@limiter.exempt
@agente_required
def api_plantillas_rapidas(tel):
    """Devuelve respuestas rápidas (/atajos) ya interpoladas con {{usuario}}, {{asesor}} y {{url_empresa}}."""
    from .timers import interpolar_plantilla
    PlantillaRapida.seed()
    usu = Usuario.query.filter_by(telefono=tel).first()
    emp = db.session.get(Empresa, usu.empresa_id) if (usu and usu.empresa_id) else None
    nom_usuario = (usu.nombre_wa if usu and usu.nombre_wa else "") or "estimado(a)"
    nom_asesor = (g.user.nombre_completo or g.user.username)
    items = PlantillaRapida.query.filter_by(activo=True).order_by(PlantillaRapida.id.asc()).all()
    return jsonify({
        "plantillas": [
            {
                "atajo": p.atajo,
                "titulo": p.titulo,
                "texto": interpolar_plantilla(p.contenido, usuario_nombre=nom_usuario, asesor_nombre=nom_asesor, empresa=emp),
            }
            for p in items
        ]
    }), 200


@main.route('/api/whisper/<tel>', methods=['POST'])
@limiter.exempt
@supervisor_required
def api_whisper(tel):
    """Modo Susurro (Whisper): nota interna del supervisor visible solo en el panel, jamás enviada a WhatsApp."""
    data = request.get_json(silent=True) or {}
    nota = (data.get('mensaje') or '').strip()
    if not nota:
        return jsonify({"error": "Nota vacía"}), 400
    m = Mensaje(
        telefono=str(tel),
        nombre=f"[Susurro] {g.user.username}",
        contenido=nota[:2000],
        direccion='INTERNO',
        es_interno=True,
    )
    db.session.add(m)
    db.session.commit()
    return jsonify({"status": "success", "id": m.id}), 201


@main.route('/api/takeover/<tel>', methods=['POST'])
@limiter.exempt
@supervisor_required
def api_takeover(tel):
    """Reasigna un chat a otro colaborador o lo devuelve al bot EVO en 1 clic."""
    data = request.get_json(silent=True) or {}
    nuevo_asesor = (data.get('asignado_a') or '').strip()[:60]
    devolver_bot = bool(data.get('devolver_bot'))
    usu = Usuario.query.filter_by(telefono=tel).first()
    if not usu:
        return jsonify({"error": "Contacto no encontrado"}), 404
    if devolver_bot:
        usu.bot_pausado = False
        usu.estado_chat = 'bot'
        usu.asignado_a = None
        usu.esperando_asesor_desde = None
        usu.esperando_usuario_desde = None
        usu.aviso_inactividad_enviado = False
    else:
        usu.bot_pausado = True
        usu.estado_chat = 'atendido' if nuevo_asesor else 'en_cola'
        usu.asignado_a = nuevo_asesor or None
        lead = Lead.query.filter_by(telefono=tel).order_by(Lead.id.desc()).first()
        if lead and nuevo_asesor:
            lead.asignado_a = nuevo_asesor
    db.session.commit()
    return jsonify({"status": "success", "estado_chat": usu.estado_chat, "asignado_a": usu.asignado_a or ""}), 200


@main.route('/admin/leads/<int:lid>/enviar_hsm', methods=['POST'])
@agente_required
def lead_enviar_hsm(lid):
    """Envía una plantilla HSM aprobada por Meta al lead (válida incluso fuera de la ventana de 24 h)."""
    l = db.get_or_404(Lead, lid)
    tpl_nombre = (request.form.get('template_name') or '').strip()[:100]
    idioma = (request.form.get('language') or 'es').strip()[:10]
    if not tpl_nombre:
        flash('Indica el nombre de la plantilla HSM aprobada por Meta.', 'danger')
        return redirect(_destino_leads(request.form.get('next', '')))
    resp = enviar_plantilla(l.telefono, tpl_nombre, idioma=idioma)
    if resp is not None and resp.status_code == 200:
        l.atendido_por = g.user.username
        if l.estado == 'nuevo':
            l.estado = 'contactado'
        if not l.primera_respuesta_en:
            l.primera_respuesta_en = utcnow()
        db.session.commit()
        flash(f'Plantilla HSM «{tpl_nombre}» enviada al lead #{l.id}.', 'success')
    else:
        flash(f'Meta rechazó o no respondió al envío de la plantilla «{tpl_nombre}».', 'danger')
    return redirect(_destino_leads(request.form.get('next', '')))


def _calcular_kpis():
    total_usuarios = Usuario.query.count()
    derivados = Lead.query.filter(Lead.quiere_asesor.is_(True)).count()
    contencion = round(((total_usuarios - derivados) / total_usuarios * 100), 1) if total_usuarios else 100.0
    contencion = max(0.0, min(100.0, contencion))

    leads_todos = Lead.query.all()
    frt_vals, art_vals = [], []
    por_asesor = {}
    embudo_origen = {}
    for l in leads_todos:
        orig = l.origen or 'organico'
        e = embudo_origen.setdefault(orig, {'ingresados': 0, 'contactados': 0, 'citas': 0, 'matriculados': 0})
        e['ingresados'] += 1
        if l.estado in ('contactado', 'visita', 'matriculado'):
            e['contactados'] += 1
        if l.estado in ('visita', 'matriculado') or l.resultado_llamada == 'cita_agendada':
            e['citas'] += 1
        if l.estado == 'matriculado':
            e['matriculados'] += 1

        asesor_k = l.asignado_a or 'sin_asignar'
        a_stat = por_asesor.setdefault(asesor_k, {'leads': 0, 'frt_s': [], 'art_s': []})
        a_stat['leads'] += 1
        if l.primera_respuesta_en and l.creado_en:
            diff = max(0, (l.primera_respuesta_en - l.creado_en).total_seconds())
            frt_vals.append(diff)
            a_stat['frt_s'].append(diff)
        if l.resuelto_en and l.creado_en:
            diff = max(0, (l.resuelto_en - l.creado_en).total_seconds())
            art_vals.append(diff)
            a_stat['art_s'].append(diff)

    asesores_resumen = {
        k: {
            'leads': v['leads'],
            'frt_promedio_seg': round(sum(v['frt_s']) / len(v['frt_s']), 1) if v['frt_s'] else None,
            'art_promedio_seg': round(sum(v['art_s']) / len(v['art_s']), 1) if v['art_s'] else None,
        }
        for k, v in por_asesor.items()
    }
    top_gaps = [
        {"pregunta": p.pregunta, "veces": p.veces, "empresa_id": p.empresa_id, "estado": p.estado}
        for p in PreguntaPendiente.query.order_by(PreguntaPendiente.veces.desc()).limit(10).all()
    ]
    return {
        "total_usuarios": total_usuarios,
        "tasa_contencion_bot_pct": contencion,
        "frt_promedio_seg": round(sum(frt_vals) / len(frt_vals), 1) if frt_vals else None,
        "art_promedio_seg": round(sum(art_vals) / len(art_vals), 1) if art_vals else None,
        "asesores": asesores_resumen,
        "embudo_por_origen": embudo_origen,
        "top_brechas_ia": top_gaps,
    }


@main.route('/api/reportes/kpis')
@supervisor_required
def api_reportes_kpis():
    return jsonify(_calcular_kpis()), 200


@main.route('/admin/reportes/kpis.csv')
@supervisor_required
def admin_reportes_kpis_csv():
    """Exporta reporte gerencial KPI en CSV delimitado por ';' con BOM UTF-8 (directo para Excel)."""
    import csv
    from io import StringIO
    k = _calcular_kpis()
    buf = StringIO()
    buf.write('\ufeff')  # BOM UTF-8 para Excel
    w = csv.writer(buf, delimiter=';')
    w.writerow(['SECCION', 'METRICA / ORIGEN / ASESOR', 'VALOR 1', 'VALOR 2', 'VALOR 3', 'VALOR 4'])
    w.writerow(['RESUMEN', 'Tasa de Contencion EVO (%)', k['tasa_contencion_bot_pct'], '', '', ''])
    w.writerow(['RESUMEN', 'Total Contactos', k['total_usuarios'], '', '', ''])
    w.writerow(['RESUMEN', 'FRT Promedio (seg)', k['frt_promedio_seg'] or '-', '', '', ''])
    w.writerow(['RESUMEN', 'ART Promedio (seg)', k['art_promedio_seg'] or '-', '', '', ''])
    w.writerow([])
    w.writerow(['EMBUDO_ORIGEN', 'Origen', 'Ingresados', 'Contactados', 'Citas', 'Matriculados'])
    for orig, d in k['embudo_por_origen'].items():
        w.writerow(['EMBUDO_ORIGEN', orig, d['ingresados'], d['contactados'], d['citas'], d['matriculados']])
    w.writerow([])
    w.writerow(['ASESORES', 'Asesor', 'Leads', 'FRT Prom (seg)', 'ART Prom (seg)', ''])
    for asr, d in k['asesores'].items():
        w.writerow(['ASESORES', asr, d['leads'], d['frt_promedio_seg'] or '-', d['art_promedio_seg'] or '-', ''])
    w.writerow([])
    w.writerow(['BRECHAS_IA', 'Pregunta', 'Veces', 'Estado', 'Empresa ID', ''])
    for g_item in k['top_brechas_ia']:
        w.writerow(['BRECHAS_IA', g_item['pregunta'], g_item['veces'], g_item['estado'], g_item['empresa_id'] or '', ''])
    return Response(
        buf.getvalue().encode('utf-8'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=reporte_kpis_evo.csv'},
    )


# --- 5. GESTIÓN DE USUARIOS Y PERFIL ---
@main.route('/admin/usuarios', methods=['GET', 'POST'])
@admin_required
def admin_usuarios():
    current_user = g.user
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'create':
            username = (request.form.get('username') or '').strip().lower()
            password = request.form.get('password') or ''
            nombre_completo = (request.form.get('nombre_completo') or '').strip()
            rol = request.form.get('rol', 'lector')
            
            if not username or not password:
                flash('Usuario y contraseña son requeridos.', 'danger')
            elif len(username) < 3:
                flash('El nombre de usuario debe tener al menos 3 caracteres.', 'danger')
            elif len(password) < Config.PASSWORD_MIN_LENGTH:
                flash(f'La contraseña debe tener al menos {Config.PASSWORD_MIN_LENGTH} caracteres.', 'danger')
            elif rol not in ROLES:
                flash('Rol seleccionado inválido.', 'danger')
            elif UsuarioSistema.query.filter_by(username=username).first():
                flash(f'El usuario "{username}" ya existe.', 'danger')
            else:
                emp_id_raw = (request.form.get('empresa_id') or '').strip()
                try:
                    max_chats = max(1, min(100, int(request.form.get('max_chats') or 10)))
                except ValueError:
                    max_chats = 10
                nuevo = UsuarioSistema(
                    username=username,
                    nombre_completo=nombre_completo or username,
                    rol=rol,
                    empresa_id=int(emp_id_raw) if emp_id_raw.isdigit() else None,
                    max_chats=max_chats,
                    en_linea=True,
                    activo=True,
                )
                nuevo.set_password(password)
                db.session.add(nuevo)
                db.session.commit()
                AuditoriaAdmin.registrar(g.user.username, 'usuario_creado', f'{username} ({rol})', get_remote_address())
                flash(f'Usuario "{username}" ({rol}) creado exitosamente.', 'success')
                return redirect(url_for('main.admin_usuarios'))
                
    usuarios = UsuarioSistema.query.order_by(UsuarioSistema.creado_en.desc()).all()
    empresas = Empresa.query.filter_by(activo=True).order_by(Empresa.orden.asc(), Empresa.id.asc()).all()
    return render_template('usuarios.html', usuarios=usuarios, empresas=empresas, current_user=current_user,
                           min_password=Config.PASSWORD_MIN_LENGTH)

@main.route('/admin/perfil', methods=['POST'])
@login_required
def admin_perfil():
    user = g.user
        
    nombre_completo = (request.form.get('nombre_completo') or '').strip()
    nuevo_username = (request.form.get('username') or '').strip().lower()
    pass_actual = request.form.get('pass_actual') or ''
    pass_nueva = request.form.get('pass_nueva') or ''
    pass_confirm = request.form.get('pass_confirm') or ''
    
    if nombre_completo:
        user.nombre_completo = nombre_completo
        session['user_name'] = nombre_completo
        
    if nuevo_username and nuevo_username != user.username:
        if len(nuevo_username) < 3:
            flash('El nombre de usuario debe tener al menos 3 caracteres.', 'danger')
            return redirect(url_for('main.admin_usuarios'))
        existe = UsuarioSistema.query.filter_by(username=nuevo_username).first()
        if existe and existe.id != user.id:
            flash(f'El usuario "{nuevo_username}" ya está registrado.', 'danger')
            return redirect(url_for('main.admin_usuarios'))
        user.username = nuevo_username
        session['username'] = nuevo_username
        
    if pass_nueva:
        if not user.check_password(pass_actual):
            flash('La contraseña actual ingresada es incorrecta.', 'danger')
            return redirect(url_for('main.admin_usuarios'))
        if len(pass_nueva) < Config.PASSWORD_MIN_LENGTH:
            flash(f'La nueva contraseña debe tener al menos {Config.PASSWORD_MIN_LENGTH} caracteres.', 'danger')
            return redirect(url_for('main.admin_usuarios'))
        if pass_nueva != pass_confirm:
            flash('La nueva contraseña y su confirmación no coinciden.', 'danger')
            return redirect(url_for('main.admin_usuarios'))
        user.set_password(pass_nueva)
        flash('Contraseña y perfil actualizados exitosamente.', 'success')
    else:
        flash('Datos de perfil actualizados exitosamente.', 'success')
        
    db.session.commit()
    return redirect(url_for('main.admin_usuarios'))

@main.route('/admin/usuarios/<int:uid>/toggle', methods=['POST'])
@admin_required
def admin_toggle_usuario(uid):
    if uid == session.get('user_id'):
        flash('No puedes desactivar tu propia cuenta en sesión.', 'danger')
        return redirect(url_for('main.admin_usuarios'))
        
    user = db.get_or_404(UsuarioSistema, uid)
    user.activo = not user.activo
    db.session.commit()
    estado = "activado" if user.activo else "desactivado"
    flash(f'El usuario "{user.username}" ha sido {estado}.', 'info')
    return redirect(url_for('main.admin_usuarios'))

@main.route('/admin/usuarios/<int:uid>/delete', methods=['POST'])
@admin_required
def admin_delete_usuario(uid):
    if uid == session.get('user_id'):
        flash('No puedes eliminar tu propia cuenta en sesión.', 'danger')
        return redirect(url_for('main.admin_usuarios'))
        
    user = db.get_or_404(UsuarioSistema, uid)
    username = user.username
    db.session.delete(user)
    db.session.commit()
    flash(f'Usuario "{username}" eliminado correctamente.', 'info')
    return redirect(url_for('main.admin_usuarios'))

# --- 6. OTROS ---
@main.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('main.login'))

# --- 5.1 COMPROBANTES DE PAGO (revisión por contabilidad) ---
def _destino_seguro(valor):
    return valor if valor and valor.startswith('/admin/comprobantes') else url_for('main.comprobantes')


@main.route('/admin/comprobantes')
@contabilidad_required
def comprobantes():
    estado = request.args.get('estado', 'pendiente')
    q = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    query = Comprobante.query
    if estado in ('pendiente', 'aprobado', 'rechazado'):
        query = query.filter_by(estado=estado)
    if q:
        patron = f"%{q}%"
        query = query.filter(Comprobante.dni_estudiante.ilike(patron) | Comprobante.telefono.ilike(patron)
                             | Comprobante.nota.ilike(patron))
    orden = Comprobante.creado_en.asc() if estado == 'pendiente' else Comprobante.creado_en.desc()
    pag = query.order_by(orden).paginate(page=page, per_page=15, error_out=False)
    conteos = dict(db.session.query(Comprobante.estado, func.count(Comprobante.id)).group_by(Comprobante.estado).all())
    return render_template('comprobantes.html', pag=pag, estado=estado, q=q,
                           pendientes=conteos.get('pendiente', 0), aprobados=conteos.get('aprobado', 0),
                           rechazados=conteos.get('rechazado', 0))


@main.route('/admin/comprobantes/<int:cid>/archivo')
@contabilidad_required
def comprobante_archivo(cid):
    import os
    c = db.get_or_404(Comprobante, cid)
    nombre = f"{c.codigo}{os.path.splitext(c.archivo)[1]}"
    if c.contenido:
        logger.info("comprobante %s consultado por %s", c.codigo, g.user.username)
        return send_file(BytesIO(c.contenido), mimetype=c.mime or 'application/octet-stream', as_attachment=False, download_name=nombre)
    ruta = os.path.join(Config.COMPROBANTES_DIR, os.path.basename(c.archivo))
    if not os.path.isfile(ruta):
        return "Archivo no encontrado en el servidor", 404
    logger.info("comprobante %s consultado por %s", c.codigo, g.user.username)
    return send_file(ruta, mimetype=c.mime or 'application/octet-stream', as_attachment=False,
                     download_name=f"{c.codigo}{os.path.splitext(c.archivo)[1]}")


@main.route('/admin/comprobantes/<int:cid>/revisar', methods=['POST'])
@contabilidad_required
def comprobante_revisar(cid):
    c = db.get_or_404(Comprobante, cid)
    destino = _destino_seguro(request.form.get('next', ''))
    accion = request.form.get('accion')
    motivo = (request.form.get('motivo') or '').strip()[:300]
    if accion == 'aprobar':
        c.estado, c.motivo = 'aprobado', None
        aviso = f"✅ Tu comprobante *{c.codigo}* fue validado por el área contable. ¡Gracias!"
    elif accion == 'rechazar':
        if not motivo:
            flash('Indica el motivo del rechazo para avisar al usuario.', 'danger')
            return redirect(destino)
        c.estado, c.motivo = 'rechazado', motivo
        aviso = (f"⚠️ Tu comprobante *{c.codigo}* fue observado: {motivo}\n\n"
                 "Puedes enviarlo nuevamente desde el menú *Soy alumno → Enviar comprobante*.")
    else:
        return redirect(destino)
    c.revisado_por, c.revisado_en = g.user.username, utcnow()
    db.session.commit()
    if accion == 'aprobar':
        disparar_evento('comprobante.approved', {
            "id": c.id, "codigo": c.codigo, "dni": c.dni_estudiante, "telefono": c.telefono, "revisado_por": g.user.username,
        })
    resp = enviar_texto(c.telefono, aviso)
    if resp is not None and resp.status_code == 200:
        flash(f'{c.codigo} {c.estado}. Se notificó al usuario por WhatsApp.', 'success')
    else:
        flash(f'{c.codigo} {c.estado}, pero NO se pudo avisar por WhatsApp (¿pasaron más de 24 h desde su último mensaje?).', 'warning')
    return redirect(destino)


@main.route('/admin/leads')
@agente_required
def leads():
    from sqlalchemy import case
    estado = request.args.get('estado', 'todos')
    cola = request.args.get('cola', 'todos')
    q = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    query = Lead.query
    # Aislamiento por empresa: un asesor callcenter asignado a 1 empresa solo ve los leads de su institución
    if g.user.rol == 'callcenter' and getattr(g.user, 'empresa_id', None):
        query = query.filter(Lead.empresa_id == g.user.empresa_id)
    if estado in Lead.ESTADOS:
        query = query.filter_by(estado=estado)
    if cola == 'llamar':
        query = query.filter(Lead.resultado_llamada.in_(('pendiente', 'no_contesta', 'volver_llamar')) | Lead.resultado_llamada.is_(None))
        query = query.filter(Lead.estado.notin_(('matriculado', 'descartado')))
    elif cola == 'mios':
        query = query.filter_by(asignado_a=g.user.username)
    elif cola == 'sin_asignar':
        query = query.filter((Lead.asignado_a.is_(None)) | (Lead.asignado_a == ''))
    if q:
        patron = f"%{q}%"
        query = query.filter(
            Lead.nombre.ilike(patron) | Lead.telefono.ilike(patron)
            | Lead.sede_nombre.ilike(patron) | Lead.resumen_ia.ilike(patron)
            | Lead.tags.ilike(patron)
        )
    prio_ord = case((Lead.prioridad == 'alta', 0), (Lead.prioridad == 'media', 1), else_=2)
    pag = query.order_by(prio_ord.asc(), Lead.creado_en.desc()).paginate(page=page, per_page=15, error_out=False)
    conteos = dict(db.session.query(Lead.estado, func.count(Lead.id)).group_by(Lead.estado).all())
    por_llamar = Lead.query.filter(
        (Lead.resultado_llamada.in_(('pendiente', 'no_contesta', 'volver_llamar')) | Lead.resultado_llamada.is_(None)),
        Lead.estado.notin_(('matriculado', 'descartado')),
    ).count()
    sin_asignar = Lead.query.filter((Lead.asignado_a.is_(None)) | (Lead.asignado_a == '')).count()
    mios = Lead.query.filter_by(asignado_a=g.user.username).count()
    agentes = [u.username for u in UsuarioSistema.query.filter(
        UsuarioSistema.activo.is_(True), UsuarioSistema.rol.in_(('admin', 'callcenter'))
    ).order_by(UsuarioSistema.username.asc()).all()]
    empresas_lista = Empresa.query.filter_by(activo=True).order_by(Empresa.orden.asc(), Empresa.id.asc()).all()
    tels = [l.telefono for l in pag.items]
    lids = [l.id for l in pag.items]
    pausados = {u.telefono: True for u in Usuario.query.filter(Usuario.telefono.in_(tels), Usuario.bot_pausado.is_(True)).all()} if tels else {}
    eventos_por_lead = {}
    if lids:
        evs = LeadEvento.query.filter(LeadEvento.lead_id.in_(lids)).order_by(LeadEvento.creado_en.desc()).all()
        for ev in evs:
            arr = eventos_por_lead.setdefault(ev.lead_id, [])
            if len(arr) < 5:
                arr.append(ev)
    return render_template(
        'leads.html', pag=pag, estado=estado, cola=cola, q=q, estados=Lead.ESTADOS,
        prioridades=Lead.PRIORIDADES, scores=Lead.SCORES, resultados_llamada=Lead.RESULTADOS_LLAMADA,
        conteos=conteos, total=sum(conteos.values()), por_llamar=por_llamar,
        sin_asignar=sin_asignar, mios=mios, agentes=agentes, empresas=empresas_lista,
        bot_pausado=pausados, eventos_por_lead=eventos_por_lead,
    )


@main.route('/admin/leads/<int:lid>/tomar', methods=['POST'])
@agente_required
def lead_tomar(lid):
    l = db.get_or_404(Lead, lid)
    l.asignado_a = g.user.username
    l.atendido_por = g.user.username
    if l.estado == 'nuevo':
        l.estado = 'contactado'
    db.session.commit()
    flash(f'Interesado #{l.id} asignado a {g.user.username}.', 'success')
    return redirect(_destino_leads(request.form.get('next', '')))


@main.route('/admin/leads/<int:lid>/actualizar', methods=['POST'])
@agente_required
def lead_actualizar(lid):
    l = db.get_or_404(Lead, lid)
    cambios = []
    nuevo = request.form.get('estado')
    if nuevo in Lead.ESTADOS and nuevo != l.estado:
        cambios.append(f"estado: {l.estado}→{nuevo}")
        l.estado = nuevo
    prio = request.form.get('prioridad')
    if prio in Lead.PRIORIDADES:
        l.prioridad = prio
    res_ll = request.form.get('resultado_llamada')
    if res_ll in Lead.RESULTADOS_LLAMADA and res_ll != l.resultado_llamada:
        cambios.append(f"llamada: {res_ll}")
        l.resultado_llamada = res_ll
    asig = request.form.get('asignado_a')
    if asig is not None:
        nuevo_asig = asig.strip()[:60] or None
        if nuevo_asig != l.asignado_a:
            cambios.append(f"asignado: {nuevo_asig or 'nadie'}")
        l.asignado_a = nuevo_asig
    tel_alt = request.form.get('telefono_contacto')
    if tel_alt is not None:
        l.telefono_contacto = tel_alt.strip()[:30] or None
    tags_raw = request.form.get('tags')
    if tags_raw is not None:
        l.tags = ','.join(t.strip() for t in tags_raw.split(',') if t.strip())[:255]
    motivo_desc = (request.form.get('motivo_descarte') or '').strip()[:60]
    if motivo_desc in Lead.MOTIVOS_DESCARTE or motivo_desc == '':
        l.motivo_descarte = motivo_desc
    l.notas_internas = (request.form.get('notas') or '').strip()[:500]
    l.atendido_por = g.user.username
    if cambios:
        db.session.add(LeadEvento(lead_id=l.id, tipo='estado', detalle=' · '.join(cambios)[:300], usuario=g.user.username))
    db.session.commit()
    if nuevo == 'matriculado':
        disparar_evento('lead.won', {
            "lead_id": l.id, "nombre": l.nombre, "telefono": l.telefono, "empresa_id": l.empresa_id,
            "sede": l.sede_nombre, "nivel": l.nivel, "origen": l.origen, "campana": l.campana_nombre,
        })
    flash(f'Interesado #{l.id} actualizado.', 'success')
    return redirect(_destino_leads(request.form.get('next', '')))


@main.route('/admin/leads/exportar.csv')
@agente_required
def leads_exportar_csv():
    """Exporta Leads a CSV delimitado por ';' con BOM UTF-8 (Excel), respetando aislamiento por empresa."""
    import csv
    from io import StringIO
    q = Lead.query
    if g.user.rol == 'callcenter' and getattr(g.user, 'empresa_id', None):
        q = q.filter(Lead.empresa_id == g.user.empresa_id)
    filas = q.order_by(Lead.creado_en.desc()).limit(5000).all()
    buf = StringIO()
    buf.write('\ufeff')
    w = csv.writer(buf, delimiter=';')
    w.writerow(['ID', 'Fecha', 'DNI', 'Nombre', 'WhatsApp', 'Tel Alternativo', 'Sede/Empresa', 'Programa', 'Origen',
                'Campana', 'Score', 'Estado', 'Motivo Descarte', 'Prioridad', 'Asignado', 'Tags', 'Resumen'])
    for l in filas:
        w.writerow([
            l.id, to_local(l.creado_en), getattr(l, 'dni', '') or '', l.nombre or '', l.telefono or '', l.telefono_contacto or '',
            l.sede_nombre or '', l.nivel or '', l.origen or 'organico', l.campana_nombre or '',
            l.score or 'frio', l.estado or '', getattr(l, 'motivo_descarte', '') or '',
            l.prioridad or '', l.asignado_a or '', l.tags or '', (l.resumen_ia or l.comentario or '').replace('\n', ' '),
        ])
    return Response(
        buf.getvalue().encode('utf-8'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=leads_evo.csv'},
    )


@main.route('/admin/leads/<int:lid>/reactivar_bot', methods=['POST'])
@agente_required
def lead_reactivar_bot(lid):
    l = db.get_or_404(Lead, lid)
    usu = Usuario.query.filter_by(telefono=l.telefono).first()
    if usu:
        usu.bot_pausado = False
        db.session.commit()
    flash('Bot reactivado para este contacto.', 'success')
    return redirect(_destino_leads(request.form.get('next', '')))


def _destino_leads(valor):
    return valor if valor and valor.startswith('/admin/leads') else url_for('main.leads')


@main.route('/admin/ia')
@admin_required
def ia_panel():
    from datetime import date
    from . import ia as ia_router
    hoy = db.session.query(func.coalesce(func.sum(IaUso.mensajes), 0)).filter(IaUso.dia == date.today()).scalar()
    aj = db.session.get(Ajuste, 'ia_conocimiento')
    empresas = Empresa.query.order_by(Empresa.orden.asc(), Empresa.id.asc()).all()
    empresas_map = {e.id: e.nombre_comercial for e in empresas}
    campanas = ReglaCampana.query.order_by(ReglaCampana.id.desc()).all()
    materiales = MaterialComercial.query.order_by(MaterialComercial.id.desc()).all()
    webhooks = WebhookSaliente.query.order_by(WebhookSaliente.id.desc()).all()
    PlantillaRapida.seed()
    plantillas = PlantillaRapida.query.order_by(PlantillaRapida.id.asc()).all()
    preguntas_pendientes = (
        PreguntaPendiente.query.filter_by(estado='pendiente')
        .order_by(PreguntaPendiente.veces.desc(), PreguntaPendiente.creado_en.desc())
        .limit(50).all()
    )
    catalogo = 'MODO DEMO' if Config.CAT_API_MOCK else (Config.CAT_API_URL or 'Texto oficial (/admin/ia)')
    saludo_preview = (Config.BOT_DEFAULT_GREETING or '').replace('{{BOT_NAME}}', Config.BOT_NAME)
    return render_template('ia.html', activa=Config.IA_ENABLED, proveedor=Config.IA_PROVIDER, respaldo=Config.IA_PROVIDER_RESPALDO,
                           gemini_modelo=Config.GEMINI_MODEL, openrouter_modelos=', '.join(Config.OPENROUTER_MODELS),
                           hoy=hoy, tope=Config.IA_MAX_LLAMADAS_DIA, catalogo=catalogo, aviso=Config.IA_AVISO,
                           ultimo_error=ia_router.ULTIMO_ERROR,
                           empresas=empresas, empresas_map=empresas_map, campanas=campanas,
                           materiales=materiales, plantillas=plantillas,
                           webhooks=webhooks, webhook_eventos=WebhookSaliente.EVENTOS,
                           preguntas_pendientes=preguntas_pendientes,
                           saludo_preview=saludo_preview,
                           conocimiento=aj.valor if aj else '',
                           actualizado=(f"{to_local(aj.actualizado_en)} por {aj.actualizado_por}" if aj and aj.actualizado_en else ''))


@main.route('/admin/ia/webhooks/guardar', methods=['POST'])
@admin_required
def ia_webhook_guardar():
    from .crypto import cifrar_token
    nombre = (request.form.get('nombre') or '').strip()[:120]
    evento = (request.form.get('evento') or '*').strip()[:60]
    url = (request.form.get('url') or '').strip()[:500]
    sec = (request.form.get('secret') or '').strip()
    if not nombre or not url.startswith(('http://', 'https://')):
        flash('Indica un nombre y una URL HTTPS válida para el webhook.', 'danger')
        return redirect(url_for('main.ia_panel'))
    if evento not in WebhookSaliente.EVENTOS:
        evento = '*'
    h = WebhookSaliente(nombre=nombre, evento=evento, url=url, secret=cifrar_token(sec) if sec else '', activo=True)
    db.session.add(h)
    db.session.commit()
    AuditoriaAdmin.registrar(g.user.username, 'webhook_creado', f'{nombre} ({evento}) -> {url}', get_remote_address())
    flash(f'Webhook saliente «{h.nombre}» guardado.', 'success')
    return redirect(url_for('main.ia_panel'))


@main.route('/admin/ia/webhooks/<int:wid>/eliminar', methods=['POST'])
@admin_required
def ia_webhook_eliminar(wid):
    h = db.session.get(WebhookSaliente, wid)
    if h:
        db.session.delete(h)
        db.session.commit()
        flash('Webhook eliminado.', 'info')
    return redirect(url_for('main.ia_panel'))


@main.route('/admin/ia/materiales/guardar', methods=['POST'])
@admin_required
def ia_material_guardar():
    titulo = (request.form.get('titulo') or '').strip()[:150]
    tipo = (request.form.get('tipo') or 'image').strip().lower()
    url = (request.form.get('url') or '').strip()[:500]
    emp_id = (request.form.get('empresa_id') or '').strip()
    if tipo not in ('image', 'video', 'document'):
        tipo = 'image'
    if not titulo or not url.startswith(('http://', 'https://')):
        flash('Indica un título y una URL HTTPS válida para el material.', 'danger')
        return redirect(url_for('main.ia_panel'))
    m = MaterialComercial(
        empresa_id=int(emp_id) if emp_id.isdigit() else None,
        titulo=titulo,
        tipo=tipo,
        url=url,
        filename=(request.form.get('filename') or '').strip()[:120],
        caption=(request.form.get('caption') or '').strip()[:500],
        activo=True,
    )
    db.session.add(m)
    db.session.commit()
    flash(f'Material «{m.titulo}» agregado a la biblioteca.', 'success')
    return redirect(url_for('main.ia_panel'))


@main.route('/admin/ia/materiales/<int:mid>/eliminar', methods=['POST'])
@admin_required
def ia_material_eliminar(mid):
    m = db.session.get(MaterialComercial, mid)
    if m:
        db.session.delete(m)
        db.session.commit()
        flash('Material eliminado de la biblioteca.', 'info')
    return redirect(url_for('main.ia_panel'))


@main.route('/admin/ia/plantillas/guardar', methods=['POST'])
@admin_required
def ia_plantilla_guardar():
    pid = (request.form.get('id') or '').strip()
    atajo = (request.form.get('atajo') or '').strip().lower()
    if atajo and not atajo.startswith('/'):
        atajo = '/' + atajo
    atajo = re.sub(r'[^a-z0-9_/-]+', '', atajo)[:40]
    titulo = (request.form.get('titulo') or '').strip()[:100]
    contenido = (request.form.get('contenido') or '').strip()[:2000]
    if not atajo or len(atajo) < 2 or not contenido:
        flash('Indica el atajo (ej. /brochure) y el texto del mensaje.', 'danger')
        return redirect(url_for('main.ia_panel'))

    p = db.session.get(PlantillaRapida, int(pid)) if pid.isdigit() else PlantillaRapida.query.filter_by(atajo=atajo).first()
    if not p:
        p = PlantillaRapida(atajo=atajo)
        db.session.add(p)
    p.atajo = atajo
    p.titulo = titulo or atajo
    p.contenido = contenido
    p.activo = True
    db.session.commit()
    flash(f'Plantilla rápida «{p.atajo}» guardada.', 'success')
    return redirect(url_for('main.ia_panel'))


@main.route('/admin/ia/plantillas/<int:pid>/eliminar', methods=['POST'])
@admin_required
def ia_plantilla_eliminar(pid):
    p = db.session.get(PlantillaRapida, pid)
    if p:
        db.session.delete(p)
        db.session.commit()
        flash(f'Plantilla «{p.atajo}» eliminada.', 'info')
    return redirect(url_for('main.ia_panel'))


@main.route('/admin/citas')
@agente_required
def admin_citas():
    estado = request.args.get('estado', 'pendiente')
    query = Cita.query
    if g.user.rol == 'callcenter' and getattr(g.user, 'empresa_id', None):
        query = query.filter(Cita.empresa_id == g.user.empresa_id)
    if estado in Cita.ESTADOS:
        query = query.filter_by(estado=estado)
    citas = query.order_by(Cita.creado_en.desc()).limit(100).all()
    conteos = dict(db.session.query(Cita.estado, func.count(Cita.id)).group_by(Cita.estado).all())
    empresas_map = {e.id: e.nombre_comercial for e in Empresa.query.all()}
    agentes = [u.username for u in UsuarioSistema.query.filter(
        UsuarioSistema.activo.is_(True), UsuarioSistema.rol.in_(('admin', 'callcenter'))
    ).order_by(UsuarioSistema.username.asc()).all()]
    return render_template('citas.html', citas=citas, estado=estado, estados=Cita.ESTADOS,
                           conteos=conteos, total=sum(conteos.values()),
                           empresas_map=empresas_map, agentes=agentes)


@main.route('/admin/citas/<int:cid>/actualizar', methods=['POST'])
@agente_required
def cita_actualizar(cid):
    c = db.get_or_404(Cita, cid)
    est = request.form.get('estado')
    if est in Cita.ESTADOS:
        c.estado = est
    asig = request.form.get('asignado_a')
    if asig is not None:
        c.asignado_a = asig.strip()[:60] or None
    franja = (request.form.get('franja') or '').strip()
    if franja:
        c.franja = franja[:80]
    db.session.commit()
    flash(f'Cita #{c.id} actualizada ({c.estado}).', 'success')
    return redirect(url_for('main.admin_citas', estado=request.form.get('filtro_estado', 'pendiente')))


@main.route('/admin/citas/<int:cid>/recordar', methods=['POST'])
@agente_required
def cita_recordar(cid):
    c = db.get_or_404(Cita, cid)
    texto = (
        f"📅 ¡Hola *{c.nombre or 'estimado(a)'}*! Te recordamos tu llamada/cita programada "
        f"(*{c.franja or c.fecha_preferida or 'pronto'}*).\n\n"
        f"Nuestro equipo se comunicará contigo según lo coordinado. ¡Gracias!"
    )
    resp = enviar_texto(c.telefono, texto)
    if resp is not None and resp.status_code == 200:
        c.recordatorio_enviado = True
        if c.estado == 'pendiente':
            c.estado = 'confirmada'
        db.session.commit()
        flash(f'Recordatorio enviado por WhatsApp para la cita #{c.id}.', 'success')
    else:
        flash('No se pudo enviar el recordatorio por WhatsApp (¿fuera de ventana 24h?).', 'warning')
    return redirect(url_for('main.admin_citas', estado=request.form.get('filtro_estado', 'pendiente')))


@main.route('/api/v1/crm/citas', methods=['POST'])
@limiter.exempt
@agente_required
def api_crm_citas():
    data = request.get_json(silent=True) or {}
    tel = str(data.get('telefono') or '').strip()[:20]
    lead_id = data.get('lead_id')
    if not tel and lead_id:
        lead = db.session.get(Lead, int(lead_id))
        if lead:
            tel = lead.telefono
    if not tel:
        return jsonify({"ok": False, "error": "Falta telefono o lead_id válido"}), 400
    emp_id = data.get('empresa_id')
    c = Cita(
        lead_id=int(lead_id) if str(lead_id or '').isdigit() else None,
        empresa_id=int(emp_id) if str(emp_id or '').isdigit() else None,
        telefono=tel,
        nombre=str(data.get('nombre') or '')[:120],
        fecha_preferida=str(data.get('fecha_preferida') or '')[:60],
        franja=str(data.get('franja') or '')[:80],
        motivo=str(data.get('motivo') or '')[:500],
        asignado_a=str(data.get('asignado_a') or g.user.username)[:60],
        estado='pendiente',
    )
    db.session.add(c)
    db.session.commit()
    return jsonify({"ok": True, "id": c.id, "estado": c.estado}), 201


@main.route('/admin/ia/campanas/guardar', methods=['POST'])
@admin_required
def ia_campana_guardar():
    nombre = (request.form.get('nombre') or '').strip()[:150]
    if not nombre:
        flash('El nombre de la campaña es obligatorio.', 'danger')
        return redirect(url_for('main.ia_panel'))
    emp_id = (request.form.get('empresa_id') or '').strip()
    c = ReglaCampana(
        nombre=nombre,
        source_id=(request.form.get('source_id') or '').strip()[:100] or None,
        palabras_clave=(request.form.get('palabras_clave') or '').strip()[:300],
        empresa_id=int(emp_id) if emp_id.isdigit() else None,
        programa=(request.form.get('programa') or '').strip()[:150],
        mensaje_bienvenida=(request.form.get('mensaje_bienvenida') or '').strip()[:2000],
        brochure_url=(request.form.get('brochure_url') or '').strip()[:500],
        brochure_nombre=(request.form.get('brochure_nombre') or 'brochure.pdf').strip()[:120],
        nodo_destino=(request.form.get('nodo_destino') or '').strip()[:50],
        activo=True,
    )
    db.session.add(c)
    db.session.commit()
    flash(f'Regla de campaña «{c.nombre}» guardada.', 'success')
    return redirect(url_for('main.ia_panel'))


@main.route('/admin/ia/campanas/<int:cid>/eliminar', methods=['POST'])
@admin_required
def ia_campana_eliminar(cid):
    c = db.session.get(ReglaCampana, cid)
    if c:
        db.session.delete(c)
        db.session.commit()
        flash('Regla de campaña eliminada.', 'info')
    return redirect(url_for('main.ia_panel'))


@main.route('/admin/ia/preguntas/<int:pid>/aprobar', methods=['POST'])
@admin_required
def ia_pregunta_aprobar(pid):
    preg = db.session.get(PreguntaPendiente, pid)
    if not preg:
        flash('Pregunta no encontrada.', 'danger')
        return redirect(url_for('main.ia_panel'))
    resp_oficial = (request.form.get('respuesta_oficial') or '').strip()[:3000]
    emp_id_raw = (request.form.get('empresa_id') or '').strip()
    if not resp_oficial:
        flash('Escribe la respuesta oficial antes de aprobar.', 'danger')
        return redirect(url_for('main.ia_panel'))

    target_emp_id = int(emp_id_raw) if emp_id_raw.isdigit() else preg.empresa_id
    bloque = f"\n\nP: {preg.pregunta}\nR: {resp_oficial}"

    emp = db.session.get(Empresa, target_emp_id) if target_emp_id else None
    if emp:
        emp.base_conocimiento = ((emp.base_conocimiento or '').rstrip() + bloque)[:15000]
        emp.actualizado_por = g.user.username
        destino = emp.nombre_comercial
    else:
        actual = Ajuste.obtener('ia_conocimiento', '')
        Ajuste.guardar('ia_conocimiento', (actual.rstrip() + bloque)[:15000], g.user.username)
        destino = 'bloque global'

    preg.empresa_id = emp.id if emp else None
    preg.respuesta_oficial = resp_oficial
    preg.estado = 'aprobada'
    preg.revisado_por = g.user.username
    preg.revisado_en = utcnow()
    db.session.commit()
    flash(f'Pregunta aprobada e integrada automáticamente a la memoria IA ({destino}).', 'success')
    return redirect(url_for('main.ia_panel'))


@main.route('/admin/ia/preguntas/<int:pid>/descartar', methods=['POST'])
@admin_required
def ia_pregunta_descartar(pid):
    preg = db.session.get(PreguntaPendiente, pid)
    if preg:
        preg.estado = 'descartada'
        preg.revisado_por = g.user.username
        preg.revisado_en = utcnow()
        db.session.commit()
        flash('Pregunta descartada.', 'info')
    return redirect(url_for('main.ia_panel'))


@main.route('/api/v1/ia/knowledge-gaps', methods=['POST'])
@limiter.exempt
@admin_required
def api_ia_knowledge_gaps():
    data = request.get_json(silent=True) or {}
    pregunta = (data.get('pregunta') or '').strip()
    if not pregunta:
        return jsonify({"ok": False, "error": "Falta el campo pregunta"}), 400
    emp_id = data.get('empresa_id')
    reg = PreguntaPendiente.registrar(
        pregunta=pregunta,
        empresa_id=int(emp_id) if str(emp_id or '').isdigit() else None,
        telefono=str(data.get('telefono') or ''),
        contexto_previo=str(data.get('contexto_previo') or ''),
    )
    return jsonify({"ok": True, "id": reg.id, "veces": reg.veces}), 201


@main.route('/admin/ia/empresas/guardar', methods=['POST'])
@admin_required
def ia_empresa_guardar():
    eid = request.form.get('id', '').strip()
    nombre = (request.form.get('nombre_comercial') or '').strip()[:150]
    clave_raw = (request.form.get('clave') or '').strip().lower()
    if not nombre:
        flash('El nombre comercial de la empresa es obligatorio.', 'danger')
        return redirect(url_for('main.ia_panel'))
    if not clave_raw:
        clave_raw = re.sub(r'[^a-z0-9]+', '_', nombre.lower()).strip('_')[:60] or 'empresa'
    else:
        clave_raw = re.sub(r'[^a-z0-9_-]+', '_', clave_raw).strip('_')[:60]

    emp = db.session.get(Empresa, int(eid)) if eid.isdigit() else None
    dup = Empresa.query.filter_by(clave=clave_raw).first()
    if dup and (not emp or dup.id != emp.id):
        flash(f'Ya existe otra empresa con la clave «{clave_raw}».', 'danger')
        return redirect(url_for('main.ia_panel'))

    if not emp:
        emp = Empresa(clave=clave_raw)
        db.session.add(emp)
    else:
        emp.clave = clave_raw

    emp.nombre_comercial = nombre
    emp.dominio_web = (request.form.get('dominio_web') or '').strip()[:255]
    emp.horario_atencion = (request.form.get('horario_atencion') or '').strip()[:255]
    emp.telefono_asesor = (request.form.get('telefono_asesor') or '').strip()[:30]
    emp.cola_asesores = (request.form.get('cola_asesores') or 'general').strip()[:80]
    emp.prompt_sistema = (request.form.get('prompt_sistema') or '').strip()[:6000]
    emp.base_conocimiento = (request.form.get('base_conocimiento') or '').strip()[:15000]
    emp.activo = request.form.get('activo') == '1'
    try:
        emp.orden = int(request.form.get('orden') or 0)
    except ValueError:
        emp.orden = 0
    emp.actualizado_por = g.user.username
    db.session.commit()
    flash(f'Empresa «{emp.nombre_comercial}» guardada correctamente.', 'success')
    return redirect(url_for('main.ia_panel'))


@main.route('/admin/ia/empresas/<int:eid>/eliminar', methods=['POST'])
@admin_required
def ia_empresa_eliminar(eid):
    emp = db.session.get(Empresa, eid)
    if not emp:
        flash('Empresa no encontrada.', 'danger')
        return redirect(url_for('main.ia_panel'))
    Usuario.query.filter_by(empresa_id=emp.id).update({'empresa_id': None})
    nombre = emp.nombre_comercial
    db.session.delete(emp)
    db.session.commit()
    flash(f'Empresa «{nombre}» eliminada.', 'success')
    return redirect(url_for('main.ia_panel'))


@main.route('/admin/ia/ajustes', methods=['POST'])
@admin_required
def ia_ajustes():
    Ajuste.guardar('ia_conocimiento', (request.form.get('conocimiento') or '').strip()[:15000], g.user.username)
    flash('Información guardada. La IA ya la usa.', 'success')
    return redirect(url_for('main.ia_panel'))


@main.route('/admin/ia/probar', methods=['POST'])
@admin_required
def ia_probar():
    from . import ia as ia_router
    if not Config.IA_ENABLED:
        flash('La IA está apagada: pon IA_ENABLED=true en el .env y recrea el contenedor.', 'danger')
        return redirect(url_for('main.ia_panel'))
    try:
        contenido, tokens = ia_router.generar("Responde solo con la palabra OK.", [{"role": "user", "parts": [{"text": "Prueba de conexión"}]}])
        texto = ''.join(p.get('text', '') for p in contenido.get('parts', []))[:80]
        flash(f'Conexión correcta con la IA (respuesta: «{texto}», {tokens} tokens).', 'success')
    except Exception as e:
        flash(f'No se pudo conectar con la IA: {str(e)[:250]}', 'danger')
    return redirect(url_for('main.ia_panel'))


@main.route('/api/ia/simular', methods=['POST'])
@limiter.exempt
@admin_required
def api_ia_simular():
    from . import asistente
    data = request.get_json(silent=True) or {}
    emp_id = data.get('empresa_id')
    res = asistente.simular(data.get('pregunta', ''), empresa_id=emp_id or None)
    return jsonify(res), 200


# --- 5.2 CONFIGURACIÓN DE ENDPOINTS API (URL, TOKEN Y CUERPO DINÁMICO) ---
def _enmascarar_token(tok):
    if not tok:
        return ''
    if len(tok) <= 8:
        return '••••'
    return f"{tok[:4]}••••{tok[-4:]}"


@main.route('/admin/endpoints')
@admin_required
def admin_endpoints():
    if ApiEndpoint.query.count() == 0:
        ApiEndpoint.seed()
    endpoints = ApiEndpoint.query.order_by(ApiEndpoint.id.asc()).all()
    env_tok = Config.ACAD_API_TOKEN or ''
    ep_data = []
    for e in endpoints:
        vars_usadas = ep_cli.extraer_variables(e.url, e.body_template)
        if e.token:
            tok_estado, tok_mask = 'propio', _enmascarar_token(e.token)
        elif env_tok:
            tok_estado, tok_mask = 'env', _enmascarar_token(env_tok)
        else:
            tok_estado, tok_mask = 'ninguno', ''
        ep_data.append({
            'id': e.id,
            'clave': e.clave,
            'nombre': e.nombre,
            'descripcion': e.descripcion or '',
            'metodo': e.metodo,
            'url': e.url,
            'auth_header': e.auth_header or 'Authorization',
            'auth_scheme': e.auth_scheme or '',
            'token_estado': tok_estado,
            'token_mask': tok_mask,
            'headers_json': e.headers_json or '{"Content-Type": "application/json"}',
            'body_template': e.body_template or '{}',
            'timeout': e.timeout,
            'activo': bool(e.activo),
            'variables': vars_usadas,
            'actualizado': (f"{to_local(e.actualizado_en)} por {e.actualizado_por}" if e.actualizado_en and e.actualizado_por else (to_local(e.actualizado_en) if e.actualizado_en else '')),
        })
    return render_template(
        'endpoints.html',
        endpoints=ep_data,
        variables_catalogo=ep_cli.VARIABLES_ALUMNO,
        total_activos=sum(1 for x in ep_data if x['activo']),
        env_token_set=bool(env_tok),
    )


@main.route('/admin/endpoints/guardar', methods=['POST'])
@admin_required
def admin_endpoints_guardar():
    eid = (request.form.get('id') or '').strip()
    clave = (request.form.get('clave') or '').strip().lower()
    nombre = (request.form.get('nombre') or '').strip()[:120]
    descripcion = (request.form.get('descripcion') or '').strip()[:300]
    metodo = (request.form.get('metodo') or 'POST').strip().upper()
    url = (request.form.get('url') or '').strip()[:500]
    auth_header = (request.form.get('auth_header') or 'Authorization').strip()[:60]
    auth_scheme = (request.form.get('auth_scheme') or '').strip()[:30]
    token_nuevo = (request.form.get('token') or '').strip()
    limpiar_token = request.form.get('limpiar_token') == '1'
    headers_json = (request.form.get('headers_json') or '').strip() or '{"Content-Type": "application/json"}'
    body_template = (request.form.get('body_template') or '').strip() or '{}'
    activo = request.form.get('activo') == '1'
    try:
        timeout = max(2, min(60, int(request.form.get('timeout') or 10)))
    except ValueError:
        timeout = 10

    if not re.fullmatch(r'[a-z0-9_]{2,60}', clave):
        flash('La clave debe tener entre 2 y 60 caracteres (solo minúsculas, números y _).', 'danger')
        return redirect(url_for('main.admin_endpoints'))
    if not nombre:
        flash('El nombre del endpoint es obligatorio.', 'danger')
        return redirect(url_for('main.admin_endpoints'))
    if metodo not in ('POST', 'GET', 'PUT', 'PATCH', 'DELETE'):
        flash('Método HTTP inválido.', 'danger')
        return redirect(url_for('main.admin_endpoints'))
    if not re.match(r'^https?://', url, re.I):
        flash('La URL debe empezar con http:// o https://.', 'danger')
        return redirect(url_for('main.admin_endpoints'))
    try:
        parsed_h = json.loads(headers_json)
        if not isinstance(parsed_h, dict):
            raise ValueError('debe ser un objeto JSON')
    except ValueError as e:
        flash(f'Headers JSON inválido: {e}', 'danger')
        return redirect(url_for('main.admin_endpoints'))

    ok_body, err_body = ep_cli.validar_body_template(body_template)
    if not ok_body:
        flash(f'Cuerpo JSON inválido: {err_body}', 'danger')
        return redirect(url_for('main.admin_endpoints'))

    if eid:
        ep = db.session.get(ApiEndpoint, int(eid))
        if not ep:
            flash('Endpoint no encontrado.', 'danger')
            return redirect(url_for('main.admin_endpoints'))
        otro = ApiEndpoint.query.filter_by(clave=clave).first()
        if otro and otro.id != ep.id:
            flash(f'Ya existe otro endpoint con la clave "{clave}".', 'danger')
            return redirect(url_for('main.admin_endpoints'))
    else:
        if ApiEndpoint.query.filter_by(clave=clave).first():
            flash(f'Ya existe un endpoint con la clave "{clave}".', 'danger')
            return redirect(url_for('main.admin_endpoints'))
        ep = ApiEndpoint(clave=clave)
        db.session.add(ep)

    ep.clave = clave
    ep.nombre = nombre
    ep.descripcion = descripcion
    ep.metodo = metodo
    ep.url = url
    ep.auth_header = auth_header
    ep.auth_scheme = auth_scheme
    if limpiar_token:
        ep.token = ''
    elif token_nuevo:
        from .crypto import cifrar_token
        ep.token = cifrar_token(token_nuevo)
    ep.headers_json = headers_json
    ep.body_template = body_template
    ep.timeout = timeout
    ep.activo = activo
    ep.actualizado_por = g.user.username
    db.session.commit()
    flash(f'Endpoint "{ep.nombre}" ({ep.clave}) guardado correctamente.', 'success')
    return redirect(url_for('main.admin_endpoints'))


@main.route('/admin/endpoints/<int:eid>/toggle', methods=['POST'])
@admin_required
def admin_endpoints_toggle(eid):
    ep = db.get_or_404(ApiEndpoint, eid)
    ep.activo = not bool(ep.activo)
    ep.actualizado_por = g.user.username
    db.session.commit()
    estado_txt = 'activado' if ep.activo else 'desactivado'
    flash(f'Endpoint "{ep.nombre}" {estado_txt}.', 'info')
    return redirect(url_for('main.admin_endpoints'))


@main.route('/admin/endpoints/<int:eid>/eliminar', methods=['POST'])
@admin_required
def admin_endpoints_eliminar(eid):
    ep = db.get_or_404(ApiEndpoint, eid)
    nombre = ep.nombre
    db.session.delete(ep)
    db.session.commit()
    flash(f'Endpoint "{nombre}" eliminado.', 'info')
    return redirect(url_for('main.admin_endpoints'))


@main.route('/api/endpoints/<int:eid>/probar', methods=['POST'])
@limiter.exempt
@admin_required
def api_endpoints_probar(eid):
    ep = db.get_or_404(ApiEndpoint, eid)
    payload = request.get_json(silent=True) or {}
    variables = payload.get('variables') if isinstance(payload.get('variables'), dict) else {}
    res = ep_cli.ejecutar(ep, variables)
    return jsonify(res), 200


@main.route('/admin/editor', methods=['GET', 'POST'])
@admin_required
def editor():
    NOMBRES_MAP = {
        'bienvenida': '👋 Bienvenida (tras validar ID)',
        'inicio': '🏠 Menú principal',
        'm_gest': '📋 Menú Servicios',
        'm_segu': 'ℹ️ Menú Información',
        'm_bene': '🛟 Menú Soporte',
    }

    if request.method == 'POST':
        action = request.form.get('action', 'save')
        clave = request.form.get('clave', '').strip()
        titulo = request.form.get('titulo', '').strip()[:150]
        texto = request.form.get('texto', '').strip()
        link = request.form.get('link', '').strip()
        botones_json = request.form.get('botones_json', '').strip()

        if not re.fullmatch(r'[a-z0-9_]{1,50}', clave):
            return redirect(url_for('main.editor', error='clave'))

        if action == 'delete':
            res = RespuestaBot.query.filter_by(clave=clave).first()
            if res:
                db.session.delete(res)
                db.session.commit()
            return redirect(url_for('main.editor', deleted='1'))

        if link and not re.match(r'^https?://', link, re.I):
            return redirect(url_for('main.editor', error='link', active=clave))
        tipo_nodo = (request.form.get('tipo_nodo') or 'botones').strip().lower()
        if tipo_nodo not in ('botones', 'lista', 'documento', 'accion'):
            tipo_nodo = 'botones'
        texto_boton_lista = (request.form.get('texto_boton_lista') or 'Ver opciones').strip()[:30]
        nodo_padre = (request.form.get('nodo_padre') or '').strip()[:50] or None
        documento_url = (request.form.get('documento_url') or '').strip()[:500] or None
        documento_nombre = (request.form.get('documento_nombre') or '').strip()[:120] or None
        emp_id_raw = (request.form.get('empresa_id') or '').strip()
        if documento_url and not re.match(r'^https?://', documento_url, re.I):
            return redirect(url_for('main.editor', error='link', active=clave))
        if botones_json:
            try:
                items = json.loads(botones_json)
                max_items = 10 if tipo_nodo == 'lista' else 3
                max_title = 24 if tipo_nodo == 'lista' else 20
                assert isinstance(items, list) and len(items) <= max_items
                assert all(isinstance(b, dict) and b.get('id') and b.get('title') for b in items)
                assert all(len(str(b['title'])) <= max_title and len(str(b['id'])) <= 256 for b in items)
            except (ValueError, AssertionError):
                return redirect(url_for('main.editor', error='botones', active=clave))

        res = RespuestaBot.query.filter_by(clave=clave).first()
        if not res:
            res = RespuestaBot(clave=clave)
            db.session.add(res)
        if titulo:
            res.titulo = titulo
        elif not res.titulo:
            res.titulo = NOMBRES_MAP.get(clave, clave.replace('f_', '').replace('_', ' ').capitalize())
        res.texto = texto
        res.link = link[:255]
        res.botones_json = botones_json or None
        res.tipo_nodo = tipo_nodo
        res.texto_boton_lista = texto_boton_lista
        res.nodo_padre = nodo_padre
        res.documento_url = documento_url
        res.documento_nombre = documento_nombre
        res.empresa_id = int(emp_id_raw) if emp_id_raw.isdigit() else None
        db.session.commit()
        return redirect(url_for('main.editor', saved='1', active=clave))

    respuestas = RespuestaBot.query.order_by(RespuestaBot.id.asc()).all()
    respuestas_data = [
        {
            "id": r.id,
            "clave": r.clave,
            "title": r.titulo or NOMBRES_MAP.get(r.clave, ""),
            "texto": r.texto or "",
            "link": r.link or "",
            "botones_json": r.botones_json or "",
            "tipo_nodo": r.tipo_nodo or "botones",
            "texto_boton_lista": r.texto_boton_lista or "Ver opciones",
            "nodo_padre": r.nodo_padre or "",
            "documento_url": r.documento_url or "",
            "documento_nombre": r.documento_nombre or "",
            "empresa_id": r.empresa_id or "",
        }
        for r in respuestas
    ]
    active_clave = request.args.get('active', respuestas[0].clave if respuestas else '')
    return render_template('editor.html', respuestas=respuestas, respuestas_json=respuestas_data,
                           active_clave=active_clave, nombres_map=NOMBRES_MAP)
