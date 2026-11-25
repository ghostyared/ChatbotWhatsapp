# Registro de cambios y plan de mejoras — QA_CHATBOT

> Documento vivo. Si se corta la sesión, retomar desde la sección **"Estado actual"**.
> Respaldo del código ANTES de los cambios (sin `.env`):
> `~/.gemini/antigravity-ide/brain/50bdc5ae-be27-415e-bd7b-3f22aeb02424/scratch/backup_original_*.tgz`

## Reglas acordadas
- **Toda variable importante va en `.env`** (con valor por defecto seguro en `config.py` y documentada en `.env.example`).
- **NO se modifica** por ahora la consulta de notas/pagos con solo DNI (el cliente trabaja internamente en fecha de nacimiento / teléfono vinculado).
- Repositorio: `github.com/ghostyared/ChatbotWhatsapp` (`.env` nunca se sube).

---

## Estado actual

| # | Tarea | Estado |
|---|---|---|
| 1 | XSS en Monitor de chats (escapar HTML) | ✅ Hecho |
| 2 | XSS menor en modal de Colaboradores | ✅ Hecho |
| 3 | Token de `.env` solo hacia hosts permitidos (`ACAD_TOKEN_HOSTS`) | ✅ Hecho |
| 4 | Bloqueo por teléfono (mensajes del mismo usuario en serie) | ✅ Hecho |
| 5 | Valores fijos del código → variables `.env` | ✅ Hecho |
| 6 | No mostrar errores internos en la URL (colaboradores) | ✅ Hecho |
| 7 | Cabecera CSP opcional (`CONTENT_SECURITY_POLICY`) | ✅ Hecho (desactivada por defecto) |
| 8 | Apagado ordenado de Gunicorn (`GUNICORN_GRACEFUL_TIMEOUT`) | ✅ Hecho |
| 9 | Script `scripts/check-env.sh` (detecta variables faltantes en `.env`) | ✅ Hecho |
| 10 | Vincular repositorio GitHub | ✅ Hecho (`origin/main`) |
| 11 | Reconstruir y probar en Docker | ⏳ Pendiente (usuario) |
| 12 | **Plan EVO — Fase 1**: Multi-Empresa (`/admin/ia`), variables `.env` (`BOT_NAME=EVO`, SLAs), reglas EVO, navegación global (`home`, `volver`, `0`, `CMD_MAIN_MENU`) | ✅ Hecho |
| 13 | **Plan EVO — Fase 2**: Cola de aprendizaje IA (`ia_preguntas_pendientes`), soporte de nodos `lista` (≤10) y `documento` PDF, citas fuera de horario y contratos de API en `/admin/endpoints` | ✅ Hecho |
| 14 | **Plan EVO — Fase 3**: Atribución Facebook/IG Ads (`referral` + `ReglaCampana`), envío HSM en `/admin/leads`, `/atajos` formales con variables y worker dedicado de timers SLA/inactividad (`scripts/worker_timers.py` + `docker-compose.yml`) | ✅ Hecho |
| 15 | **Plan EVO — Fase 4**: Torre de Supervisión (`handoff_brief`, Modo Susurro `/api/whisper`, Takeover `/api/takeover`), KPIs (Contención/FRT/ART/Embudo/Brechas IA) y exportación CSV `;` UTF-8 (`/admin/reportes/kpis.csv`) | ✅ Hecho |

### Pendientes para próximas fases
- [ ] Segundo factor en Portal del Alumno (cuando exista fecha de nacimiento / teléfono vinculado).
- [x] No reiniciar `intentos_acad` en cada DNI válido + tope de consultas por teléfono/día (`ACAD_MAX_CONSULTAS_DIA`).
- [ ] Cifrar tokens de `api_endpoints` en la BD.
- [ ] Redis para rate-limit compartido entre workers (`RATELIMIT_STORAGE_URI`).
- [ ] `/logout` solo por POST.
- [x] Optimizar consultas N+1 del dashboard (y mostrar KPIs EVO en vivo).
- [ ] Dividir `routes.py` en blueprints; unificar `consulta.py` y `alumno.py`.
- [ ] Migraciones con Flask-Migrate/Alembic.
- [ ] Activar CSP una vez probada en el navegador.

---

## Detalle de cambios (Fase 1 — 2026-11-25)

### 1. XSS en el Monitor (`app/templates/monitor.html`)
- Se añadió `escapeHtml()` y se aplica a nombre, teléfono, contenido, fecha y al mensaje del agente.
- Motivo: un usuario de WhatsApp podía enviar HTML/JS que se ejecutaba en el navegador del agente/admin.

### 2. XSS en Colaboradores (`app/templates/colaboradores.html`)
- El modal de eliminar escapa nombre y DNI.

### 3. Token de `.env` restringido (`app/endpoints_client.py`, `config.py`)
- `ACAD_API_TOKEN` solo se envía si el host del endpoint está en `ACAD_TOKEN_HOSTS`
  (por defecto: el host de `ACAD_API_URL`). Los endpoints con token propio no cambian.
- **Acción:** si algún endpoint usa otro dominio y depende del token de `.env`, añádelo a `ACAD_TOKEN_HOSTS`.

### 4. Bloqueo por teléfono (`app/routes.py`)
- En PostgreSQL se usa `pg_advisory_lock` por teléfono (conexión dedicada): dos mensajes seguidos del mismo
  número ya no se procesan a la vez ni se pisan el estado de la conversación. Funciona entre workers.
- Controlado por `WEBHOOK_LOCK_POR_TELEFONO=true`. En SQLite (tests) se omite.
- Pool de BD configurable (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`): cada mensaje en curso usa hasta 2 conexiones.

### 5. Nuevas variables `.env` (todas con valor por defecto = comportamiento anterior)
| Variable | Defecto | Uso |
|---|---|---|
| `ACAD_TOKEN_HOSTS` | host de `ACAD_API_URL` | hosts que pueden recibir `ACAD_API_TOKEN` |
| `CALLCENTER_WEBHOOK_URL` | *(vacío)* | URL inicial del endpoint `notificar_callcenter` (antes fija en el código) |
| `WA_API_TIMEOUT` | 10 | timeout envío a WhatsApp (s) |
| `WA_MEDIA_TIMEOUT` | 30 | timeout descarga de archivos (s) |
| `WEBHOOK_WORKERS` | 8 | hilos que procesan mensajes por worker |
| `WEBHOOK_LOCK_POR_TELEFONO` | true | serializa mensajes por número |
| `LOGIN_RATE_LIMIT` | `5 per minute; 15 per hour` | límite de /login por IP |
| `UPLOAD_MAX_MB` | 5 | tamaño máx. de subida (Excel) |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_RECYCLE` | 10 / 10 / 300 | pool de conexiones |
| `CONTENT_SECURITY_POLICY` | *(vacío = desactivada)* | cabecera CSP |
| `APP_PORT` | 5005 | puerto interno de Gunicorn (nginx apunta aquí) |
| `GUNICORN_TIMEOUT` | 60 | timeout de petición |
| `GUNICORN_GRACEFUL_TIMEOUT` | 30 | espera al apagar para terminar mensajes en curso |
| `APP_MEM_LIMIT` / `APP_CPUS` | 512m / 1.5 | límites del contenedor (docker-compose) |

- Se quitó el dominio fijo `api.mariategui.edu.pe` del código: ahora sale solo de `ACAD_API_URL`.
  *(Las BD existentes no se ven afectadas: el seed nunca pisa endpoints ya creados.)*
- `docker-compose.yml`: `TZ` toma `APP_TIMEZONE`.

### 6. Errores internos
- `admin_colaboradores` ya no pone el texto de la excepción en la URL; se registra en el log.

### 7. CSP opcional
- Si `CONTENT_SECURITY_POLICY` tiene valor, se envía como cabecera. Ver valor sugerido en `.env.example`.

### 8. Apagado ordenado
- `docker-entrypoint.sh` usa `GUNICORN_TIMEOUT`, `GUNICORN_GRACEFUL_TIMEOUT` y `APP_PORT`.

### 9. `scripts/check-env.sh`
- Lista variables de `.env.example` que faltan en `.env` (no muestra valores). Uso: `sudo ./scripts/check-env.sh`.

---

## Cómo desplegar estos cambios
```bash
cd /Proyectos_Dockers/deployments/QA_CHATBOT
sudo ./scripts/check-env.sh                 # ver qué variables nuevas faltan (opcionales: todas tienen defecto)
sudo docker compose build
sudo docker compose up -d
sudo docker compose logs -f --tail=100 app  # verificar arranque
# Tests dentro del contenedor:
sudo docker compose run --rm --no-deps --entrypoint sh app -c "pip install -q pytest && python -m pytest -q"
```
> Nota: `.dockerignore` excluye `tests/`; para correr tests en contenedor monta la carpeta:
> `sudo docker run --rm --entrypoint sh -v "$PWD":/src -w /src qa-chatbot:latest -c "pip install -q --target /tmp/pydeps pytest && PYTHONPATH=/tmp/pydeps python -m pytest -q -p no:cacheprovider"`

## Rollback
```bash
git checkout <commit-anterior> -- .          # si ya está en git
# o restaurar el respaldo tgz indicado arriba
```
