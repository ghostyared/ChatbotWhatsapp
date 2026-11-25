# Contrato de la API académica (deudas y notas)

El bot consulta tu API con peticiones **GET** y espera JSON. Todo se configura en `.env` (sección 5b).
Si tu API usa otros nombres de campo, **solo hay que ajustar `app/api_academica.py`** (funciones `_normalizar_*`
y `consultar_*`); el resto del bot no cambia.

Autenticación: `Authorization: Bearer <ACAD_API_TOKEN>` (o el header que definas en `ACAD_AUTH_HEADER`).
Usa HTTPS si la API sale de tu red interna.

## 1. Años (opcional si usas `ACAD_ANIOS_FIJOS`)
`GET {ACAD_PATH_ANIOS}` →
```json
["2026", "2025", "2024"]
```
(también acepta `{"items":[...]}` o una lista de objetos con `codigo` / `anio`). WhatsApp muestra máx. 10.

## 2. Periodos (opcional si usas `ACAD_PERIODOS_FIJOS`)
`GET {ACAD_PATH_PERIODOS}?anio=2026` →
```json
["2026-I", "2026-II"]
```

## 3. Deudas
`GET {ACAD_PATH_DEUDAS}?anio=2026&periodo=2026-I&dni=12345678` →
```json
{
  "estudiante": {
    "nombre": "Ana Pérez Quispe",
    "fecha_nacimiento": "2010-05-20",
    "telefonos": ["51900000001"]
  },
  "moneda": "S/",
  "total_pendiente": 350.00,
  "deudas": [
    {"concepto": "Pensión marzo", "monto": 350.00, "vencimiento": "2026-03-15", "estado": "Pendiente"},
    {"concepto": "Matrícula",     "monto": 400.00, "vencimiento": "2026-02-01", "estado": "Pagado"}
  ]
}
```
- `total_pendiente` es opcional: si falta, se suma lo que esté `Pendiente`/`Vencido`.
- `estudiante.fecha_nacimiento` es **obligatorio** si `ACAD_VERIFY_MODE=fecha_nacimiento`;
  `estudiante.telefonos` si `ACAD_VERIFY_MODE=telefono` (se comparan los últimos 9 dígitos del WhatsApp).
  **Si falta el dato, el bot deniega la consulta** (falla cerrado) y deja el error en el log.

## 4. Notas
`GET {ACAD_PATH_NOTAS}?anio=2026&periodo=2026-I&dni=12345678` →
```json
{
  "estudiante": {"nombre": "Ana Pérez Quispe", "fecha_nacimiento": "2010-05-20"},
  "promedio": 15.5,
  "cursos": [
    {"curso": "Matemática",   "nota": 16, "observacion": ""},
    {"curso": "Comunicación", "nota": 15, "observacion": "Muy bien"}
  ]
}
```

## Códigos de respuesta
| Código | Significado para el bot |
|---|---|
| 200 | Datos OK |
| 404 | El estudiante no existe / sin datos → mensaje genérico "No encontré un estudiante con esos datos" |
| 401 / 403 | El bot no está autorizado (error de configuración): el usuario ve "servicio no disponible" y queda en el log |
| 5xx, timeout, JSON inválido | "Servicio no disponible" |

## Regla "al día en pagos" para ver notas
Con `ACAD_NOTAS_REQUIERE_PAGOS_AL_DIA=true`, antes de mostrar notas el bot consulta **deudas del mismo año y periodo**
y solo las muestra si `total_pendiente` es 0. Si tu regla es otra (p. ej. cualquier deuda de cualquier periodo),
pídele a tu API un endpoint/parámetro que lo calcule y ajusta `consultar_deudas`.

## Probar sin la API real
`ACAD_API_MOCK=true` activa datos de ejemplo: DNI `12345678` (nac. 20/05/2010, con deuda) y `11111111`
(nac. 02/03/2011, al día). **Nunca lo dejes activo en producción.**
