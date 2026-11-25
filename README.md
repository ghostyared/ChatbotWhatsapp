# Chatbot WhatsApp Business (Cloud API oficial de Meta)

Chatbot para colegio e instituto con menús por botones, **consulta de pagos y notas** conectada a tu API,
**recepción de comprobantes de pago** para revisión de contabilidad, y panel web de administración (monitor de
conversaciones en vivo, modo híbrido bot/humano, editor visual de flujos).

**Stack:** Flask · Gunicorn · PostgreSQL 16 · Docker Compose · nginx y PostgreSQL en contenedores aparte (red `proxy-net`).

> **Base de datos: se queda PostgreSQL.** El código y las pruebas están validados contra PostgreSQL 16. MariaDB
> funcionaría con cambios (driver, `create_all`, tipos), pero no aporta nada aquí y añade riesgo. Para un bot con
> miles de contactos, PostgreSQL en un contenedor es más que suficiente.

---

## 1. Qué hace falta antes de desplegar

| Requisito | Detalle |
|---|---|
| Servidor | Linux con Docker Engine ≥ 24 y Docker Compose ≥ 2.24 (`docker compose version`) |
| Dominio | Un FQDN (p. ej. `chatbot.tudominio.com`) apuntando a la IP del servidor |
| HTTPS | Certificado válido (Let's Encrypt). **Meta solo envía webhooks a URLs HTTPS** |
| nginx | En Docker, en la red externa `proxy-net` (ya existente) |
| PostgreSQL | Contenedor aparte (`PROD_PostgreSQL`) en la misma red `proxy-net`; el script `scripts/desplegar-bd.sh` crea la BD y el usuario |
| Meta | App con producto WhatsApp y tu número ya registrado (ver sección 2) |

---

## 2. Datos que necesitas de Meta for Developers

1. **`PHONE_ID`** — WhatsApp → *Configuración de la API* → "Identificador de número de teléfono".
2. **`TOKEN_PERMANENTE`** — el token temporal dura 24 h, no sirve. Genera uno permanente:
   *Business Settings* → *Usuarios* → *Usuarios del sistema* → crear usuario → asignarle la app y la cuenta de WhatsApp →
   *Generar token* con permisos `whatsapp_business_messaging` y `whatsapp_business_management`.
3. **`APP_SECRET`** — *Configuración de la app* → *Básica* → "Clave secreta de la app". La app valida con ella la
   firma (`X-Hub-Signature-256`) de cada mensaje; sin ella el webhook rechaza todo.
4. **`WEBHOOK_TOKEN`** — lo inventas tú (aleatorio). Debe ser **idéntico** en tu `.env` y en Meta.
5. La app de Meta debe estar en modo **Live** para recibir mensajes de cualquier cliente (en modo Desarrollo solo
   llegan los de números con rol en la app).

---

## 3. Subir el proyecto a Git

El repositorio **no contiene secretos ni datos personales** (`.gitignore` excluye `.env`, `*.sql`, `*.xlsx`).

```bash
cd chatbot-wa
git init -b main
git add .
git status              # comprueba que NO aparece .env
git commit -m "Chatbot WhatsApp: versión inicial limpia"
git remote add origin <URL-DE-TU-REPOSITORIO>
git push -u origin main
```

Usa un repositorio **privado**. Para clonar en el servidor usa una *deploy key* o un token de solo lectura,
nunca una contraseña personal dentro de la URL.

---

## 4. Despliegue paso a paso (QA_CHATBOT)

Rutas: proyecto en `/Proyectos_Dockers/deployments/QA_CHATBOT` · nginx en
`/Proyectos_Dockers/deployments/PROD_NGINX/NginxConfig/QA_ChatBot.conf` · PostgreSQL en `/Proyectos_Dockers/deployments/PROD_PostgreSQL`.

```bash
# 1. Clonar
cd /Proyectos_Dockers/deployments && git clone <URL-DE-TU-REPOSITORIO> QA_CHATBOT && cd QA_CHATBOT

# 2. Crear el .env y proteger el archivo
cp .env.example .env && chmod 600 .env

# 3. Generar secretos aleatorios (SECRET_KEY, DB_PASS, WEBHOOK_TOKEN, ADMIN_PASS) y copiarlos al .env
./scripts/generate-secrets.sh

# 4. Editar .env: reemplaza TODOS los valores CAMBIAR...
nano .env
#    - Meta: TOKEN_PERMANENTE, PHONE_ID, APP_SECRET, WEBHOOK_TOKEN
#    - Panel: ADMIN_USER, ADMIN_PASS
#    - BD (cadena de conexión): DB_HOST (container_name de PostgreSQL, ver `docker ps`), DB_PORT, DB_NAME, DB_USER, DB_PASS
#      y para el script: PG_CONTAINER, PG_ADMIN_USER (y PG_ADMIN_PASS si el superusuario pide clave)

# 5. Crear usuario y base en el contenedor PostgreSQL existente (idempotente)
./scripts/desplegar-bd.sh

# 6. Construir y levantar la app (se une a proxy-net con el nombre qa_chatbot)
docker compose up -d --build
docker compose logs -f app                         # debe decir "Base de datos lista"
docker compose exec app python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:5005/health').read())"
```

Si el `.env` está incompleto, la app **se niega a arrancar** y el log lista exactamente qué variable falta.

### nginx (contenedor PROD_NGINX)

La app no publica puertos en el host: nginx la alcanza por `proxy-net` en `http://qa_chatbot:5005`.
Copia `docs/QA_ChatBot.conf` a `/Proyectos_Dockers/deployments/PROD_NGINX/NginxConfig/QA_ChatBot.conf`, pon tu dominio y
certificados, valida y recarga: `docker exec <contenedor_nginx> nginx -t && docker exec <contenedor_nginx> nginx -s reload`.
Mantén `TRUSTED_PROXIES=1` en el `.env` (un proxy delante).

### Conectar el webhook en Meta

*WhatsApp* → *Configuración* → *Webhook* → **Editar**:

- **URL de devolución de llamada:** `https://chatbot.tudominio.com/webhook`
- **Token de verificación:** el valor de `WEBHOOK_TOKEN`
- Pulsa *Verificar y guardar* y luego **suscríbete al campo `messages`**.

### Primeros pasos en el panel

1. Entra a `https://chatbot.tudominio.com/` con `ADMIN_USER` / `ADMIN_PASS`.
2. **Colaboradores** (solo si usas `REQUIRE_ID_VALIDATION=true`, bot interno de personal) → descarga la plantilla Excel, llénala (DNI, Nombre, Empresa, Cargo, Estado) y súbela.
   Solo las personas cargadas (y no inactivas) pasan la validación de DNI.
3. **Editor** → cambia textos, links y botones de cada paso (contenido de ejemplo neutro).
4. **Usuarios** → crea cuentas `lector` (solo ven conversaciones), `contabilidad` (solo comprobantes) o `admin`.
5. Prueba: escribe "Hola" al número desde tu WhatsApp.

---

## 5. Operación diaria

```bash
docker compose logs -f app                 # logs en vivo
docker compose restart app                 # reiniciar solo la app
docker compose ps                          # estado

# Actualizar a una nueva versión
git pull && docker compose up -d --build

# Respaldo de la base de datos (programa esto en cron, p. ej. diario)
./scripts/backup.sh                        # crea backups/db_AAAAMMDD_HHMM.sql.gz (guarda los últimos 14)

# Restaurar un respaldo
gunzip -c backups/db_AAAAMMDD_HHMM.sql.gz | docker exec -i -e PGPASSWORD="<DB_PASS>" <PG_CONTAINER> psql -h 127.0.0.1 -U <DB_USER> <DB_NAME>

# Olvidé la contraseña del admin: pon la nueva en ADMIN_PASS (.env) y ejecuta
docker compose up -d --force-recreate app
docker compose exec app python manage.py reset-admin
```

Cron de ejemplo: `0 2 * * * cd /Proyectos_Dockers/deployments/QA_CHATBOT && ./scripts/backup.sh >> backups/cron.log 2>&1`

> ⚠️ `docker compose down -v` **borra la base de datos**. Usa `docker compose down` (sin `-v`).

---

## 6. Qué se corrigió respecto al proyecto original

| Problema | Corrección |
|---|---|
| Token de Meta, contraseñas y credenciales de Git dentro del zip | Eliminados. `.env.example` solo trae marcadores `CAMBIAR...` y la app no arranca con ellos |
| Cuentas `qa_*` con contraseña fija y rol admin, recreadas en cada arranque | Eliminadas. Solo se crea el admin definido en `.env` |
| Contraseña admin, token de webhook y `PHONE_ID` con valor por defecto en el código | Quitados; todo es obligatorio vía `.env` |
| Webhook sin verificar firma de Meta | Valida `X-Hub-Signature-256` con `APP_SECRET` (comparación en tiempo constante) |
| Límite global (50/hora) aplicaba también al webhook → mensajes perdidos | Webhook y health exentos del límite |
| Meta reintenta entregas → respuestas duplicadas | Idempotencia por id de mensaje (`wa_id` único) |
| Solo se leía el primer mensaje de cada notificación | Se procesan todos los mensajes del lote |
| `/api/send_message` y `/api/toggle_bot` sin CSRF y accesibles a cualquier rol | CSRF obligatorio y solo `admin`; errores de Meta se muestran al agente |
| Cualquier texto en `?error=` se mostraba en el login | Solo mensajes fijos |
| Enumeración de DNI (el bot revelaba nombre y cargo) | Máx. de intentos por teléfono, luego bloqueo temporal; estados inactivos no validan |
| Contenedor como root y `.env` copiado a la imagen | Usuario no-root; `.env` fuera de la imagen (`.dockerignore`) |
| Carrera al crear tablas con 3 workers; `ALTER TABLE` ad hoc | `manage.py init-db` corre una sola vez en el entrypoint |
| BD sin script de preparación | `scripts/desplegar-bd.sh` crea usuario y BD en tu PostgreSQL externo |
| Rate limit / IP real incorrectos detrás de nginx | `ProxyFix` (`TRUSTED_PROXIES`) |
| Horas del panel en UTC | Se muestran en `APP_TIMEZONE` |
| Bug en el editor (variable usada antes de definirse al guardar sin título) | Corregido; validación de clave, link y botones |
| `pandas` solo para leer Excel (imagen pesada) | Reemplazado por `openpyxl` |
| Texto de negocio fijo (Wiener, Carrión, RR.HH.) | Contenido de ejemplo neutro editable; nombre/empresa/ID por `.env` |
| Dependencias sin versión | Versiones fijadas |
| Sin pruebas | 79 pruebas (`tests/`), validadas contra SQLite y PostgreSQL 16 |

---

## 7. Límites de WhatsApp que conviene conocer

- **Ventana de 24 h:** el bot y los agentes solo pueden escribir libremente dentro de las 24 h posteriores al último
  mensaje del cliente. Fuera de esa ventana Meta exige *plantillas aprobadas* (este proyecto no las envía; el panel
  mostrará el error de Meta si un agente intenta escribir fuera de ventana).
- **Botones:** máximo 3 por mensaje, título de hasta 20 caracteres, cuerpo de hasta 1024. La app recorta si te pasas;
  el editor limita a 3 botones.
- **Tipos de mensaje:** se procesan texto y botones. Imágenes/audios/documentos reciben un aviso amable.
- **Rotación de secretos:** si cambias `TOKEN_PERMANENTE`, `APP_SECRET` o `SECRET_KEY`, edita `.env` y ejecuta
  `docker compose up -d --force-recreate app` (cambiar `SECRET_KEY` cierra todas las sesiones del panel).

---

## 8. Problemas frecuentes

| Síntoma | Causa probable |
|---|---|
| La app se reinicia en bucle y el log lista variables | Falta o quedó `CAMBIAR...` en el `.env` |
| Meta: "No se pudo validar la URL de devolución" | URL sin HTTPS válido, `WEBHOOK_TOKEN` distinto, o nginx no está en `proxy-net` |
| Verifica bien pero no llegan mensajes | No suscrito al campo `messages`, app en modo Desarrollo, o `APP_SECRET` incorrecto (log: "Webhook con firma inválida") |
| El bot no responde pero se ven mensajes en el monitor | `TOKEN_PERMANENTE` vencido/incorrecto o `PHONE_ID` equivocado (log: "API WhatsApp respondió 401/400") |
| No puedo iniciar sesión por HTTP | `SESSION_COOKIE_SECURE=true` exige HTTPS; usa HTTPS (o `false` solo para pruebas locales) |
| Cuenta bloqueada | Espera `LOGIN_LOCK_MINUTES` o ejecuta `manage.py reset-admin` |
| `502` desde nginx | Contenedor caído: `docker compose ps` y `docker compose logs app` |

---

## 10. Funciones del bot (colegio / instituto)

**Menú inicial** (editable desde *Editor del Bot*): `📚 Vacantes` · `🎓 Soy alumno` · `📍 Contacto`

- **Vacantes e informes:** Colegio (Inicial · Primaria · Secundaria) e Instituto Pedagógico (Educación Inicial ·
  Educación Primaria · Educación Física), más proceso de matrícula. Los textos vienen como borrador: **edítalos**
  con requisitos, costos, horarios y fechas reales (y pon links si quieres).
- **Soy alumno:**
  - **Mis pagos / Mis notas:** el bot pregunta *año* (lista) → *periodo* (lista) → *DNI del estudiante* →
    *fecha de nacimiento* y muestra el reporte. Las **notas solo se ven si el estudiante está al día en pagos**
    (`ACAD_NOTAS_REQUIERE_PAGOS_AL_DIA`); si debe, el bot muestra el monto y ofrece enviar comprobante.
  - **Enviar comprobante:** pide el DNI del estudiante y la foto/PDF (JPG, PNG o PDF, máx. 5 MB). Se guarda y queda
    **pendiente** para contabilidad; al aprobarlo o rechazarlo (con motivo), **el bot avisa al usuario por WhatsApp**.
- **Contacto:** horarios, ubicación y asesor.

### Conectar tu API (pagos y notas)
1. Completa la sección 5b del `.env` (`ACAD_API_URL`, `ACAD_API_TOKEN`, rutas).
2. Tu API debe responder según **`docs/API_ACADEMICA.md`**. Si usa otros nombres de campo, solo se ajusta
   `app/api_academica.py`.
3. Para probar sin API real: `ACAD_API_MOCK=true` (DNI `12345678`, nacimiento 20/05/2010, con deuda; DNI `11111111`,
   nacimiento 02/03/2011, al día). **Apágalo en producción.**

### Revisión de comprobantes (contabilidad)
> Los comprobantes se guardan **en la base de datos** (`COMPROBANTE_ALMACEN=bd`, por defecto), junto con su huella, estado y quién los revisó; así entran en el respaldo de PostgreSQL. Con `COMPROBANTE_ALMACEN=disco` se guardan en el volumen `botdata`. Los registros anteriores en disco siguen visibles.

- En *Usuarios y Perfil* crea una cuenta con rol **Contabilidad**: solo ve la pantalla **Comprobantes**
  (no ve chats ni notas). Los admin también tienen acceso.
- Cada comprobante muestra remitente, DNI, nota, archivo y una marca **DUPLICADO** si el mismo archivo ya llegó antes.
- Los archivos viven en el volumen Docker `botdata`; `scripts/backup.sh` los respalda junto con la BD.
- El aviso por WhatsApp solo llega si el usuario escribió en las últimas 24 h (límite de Meta); si no, el panel te lo indica.

### Privacidad y seguridad de estos datos
- **Doble control:** además del DNI se pide la fecha de nacimiento (`ACAD_VERIFY_MODE=fecha_nacimiento`) o se compara el
  teléfono registrado (`telefono`). Si la API no entrega ese dato, **se deniega** (no se "abre" por error).
- El mensaje es el mismo si el DNI no existe o el dato no coincide (no permite descubrir qué DNI existen) y hay
  bloqueo temporal por teléfono tras `ACAD_MAX_ATTEMPTS` fallos.
- El historial del monitor **no guarda** montos, notas ni DNI completos (`****1234`, `[Reporte de notas enviado]`).
- La tabla `auditoria_consultas` registra cada consulta (teléfono, tipo, DNI enmascarado, año, periodo, resultado).
- Los comprobantes se guardan con permisos `600`, se valida el contenido real del archivo (no solo su extensión) y
  solo los ven los roles admin/contabilidad.
- Son datos personales (incluidos de menores): define quién tiene acceso, cuánto tiempo conservas los comprobantes y
  avisa el tratamiento de datos a los usuarios según la normativa que te aplique (en Perú, Ley 29733).

### Cómo añadir más opciones
- **Solo contenido** (informes, requisitos, FAQ): crea un paso en el *Editor del Bot* y conéctalo con un botón.
- **Flujos nuevos** (p. ej. solicitar constancia, reservar cita, pagar en línea): crea `app/flujos/mi_flujo.py` con
  `iniciar(ctx)` y `continuar(ctx, estado)`, regístralo en `app/flujos/__init__.py` y agrega un botón con su id en el
  menú. Ver el docstring de ese archivo.
- WhatsApp permite **3 botones por mensaje**. Para más opciones usa submenús (como el menú ya hace) o listas.

### Varias sedes y asistente de ventas con IA (Google AI y/o OpenRouter)
- **Sedes:** precios, vacantes y aulas vienen de tu API (`api.mariategui.edu.pe`, variables `CAT_*`, contrato en `docs/API_CATALOGO.md`). Añadir una sede no requiere tocar el bot.
- **Proveedores de IA (por `.env`):** `IA_PROVIDER=gemini|openrouter` y, opcional, `IA_PROVIDER_RESPALDO` (si el principal falla o agota su cupo gratuito, usa el otro).
  Claves: `GEMINI_API_KEY` (aistudio.google.com/apikey) y `OPENROUTER_API_KEY` (openrouter.ai/keys). En OpenRouter elige modelos `:free` que soporten *tools* (`OPENROUTER_MODEL`, varios separados por coma = se prueba el siguiente).
  Los planes gratuitos tienen límites bajos de solicitudes por minuto y día: por eso conviene tener respaldo y los topes `IA_MAX_*`.
- **Propuesta de venta:** *Panel → Asistente IA*. Ahí escribes la información oficial (propuesta de valor, requisitos, ofertas, respuestas a objeciones); se aplica al instante y la IA solo puede afirmar eso. Incluye el botón «Probar conexión».
- **Interesados:** *Panel → Interesados* (estado, nota interna, ver chat, reactivar el bot). Cada interesado avisa al asesor de su sede por WhatsApp; si pide hablar con una persona, el bot se pausa para ese contacto.
- **Protecciones:** la IA nunca recibe DNI, notas ni deudas; no inventa datos (precios, vacantes, promesas y competidores se verifican antes de enviar); topes por teléfono y por día; si la IA falla, el bot responde con el menú.
- **Privacidad:** el texto libre de las familias se envía al proveedor de IA (Google u OpenRouter); en planes gratuitos el proveedor puede usarlo para mejorar sus modelos. Infórmalo en tu aviso de privacidad y no uses esta etapa gratuita con datos sensibles.
- **Firewall (salida):** `api.mariategui.edu.pe`, `graph.facebook.com`, `generativelanguage.googleapis.com` y `openrouter.ai` por TCP 443.

### Si ya habías desplegado la versión anterior
`docker compose up -d --build` agrega solo las columnas y tablas nuevas. El menú viejo no se reemplaza solo
(para no pisar lo que editaste); para cargar el menú nuevo de ejemplo:
`docker compose exec app python manage.py reset-menu` (⚠️ borra los textos/botones editados).

---

## 9. Estructura

```
app/                 código (routes, bot_logic, services, models, templates)
app/flujos/          flujos guiados: consulta de pagos/notas y comprobantes (aquí se añaden los nuevos)
app/api_academica.py cliente de tu API de deudas y notas
app/catalogo_api.py     cliente del catálogo de sedes (precios, vacantes, aulas)
app/asistente.py       asistente de ventas con IA (herramientas, topes, interesados)
app/ia_gemini.py       cliente REST de Gemini
config.py            lectura y validación de variables de entorno
manage.py            init-db (con migración de columnas) / reset-admin / reset-menu
docker-compose.yml   app (qa_chatbot) en proxy-net; PostgreSQL y nginx son externos
Dockerfile · docker-entrypoint.sh
.env.example         plantilla de configuración
scripts/             generate-secrets.sh · backup.sh
docs/                QA_ChatBot.conf · API_ACADEMICA.md (contrato de tu API)
tests/               pruebas (pip install -r requirements-dev.txt && pytest)
```
