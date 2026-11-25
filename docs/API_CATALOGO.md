# Contrato de la API de catálogo (sedes, precios, vacantes, aulas)

Base: `https://api.mariategui.edu.pe` (`CAT_API_URL`). Las rutas (`CAT_PATH_*`) se ajustan a tus endpoints reales. El bot consulta **tu API en vivo** (cada pregunta de precio o vacantes sale de ahí, nunca de la memoria de la IA).
Si tus nombres de campo son otros, solo se ajusta `_norm_sede` / `_norm_oferta` en `app/catalogo_api.py`.

Autenticación: `CAT_API_TOKEN` + `CAT_AUTH_HEADER` + `CAT_AUTH_SCHEME` (en `.env`). Solo lectura (GET).

## 1. Sedes — `GET {CAT_PATH_SEDES}`

```json
[
  {"id": "1", "nombre": "Sede Norte", "zona": "Los Olivos", "direccion": "Av. Universitaria 1234",
   "telefono_asesor": "51999111222", "horario_atencion": "Lun-Vie 8:00-17:00", "tipo": "colegio"},
  {"id": "3", "nombre": "Instituto Pedagógico", "zona": "Cercado", "direccion": "Jr. Lampa 789",
   "telefono_asesor": "51999333444", "tipo": "instituto"}
]
```
`telefono_asesor`: WhatsApp del asesor de esa sede (con código de país). Si falta, se usa `ASESOR_TELEFONO`.

## 2. Oferta de una sede — `GET {CAT_PATH_OFERTA}?sede=<id>`

```json
{
  "sede": {"id": "1", "nombre": "Sede Norte", "zona": "Los Olivos", "telefono_asesor": "51999111222"},
  "ofertas": [
    {"nivel": "Inicial", "grado": "5 años", "turno": "Mañana", "matricula": 350, "pension": 420, "moneda": "S/",
     "vacantes": 6, "aulas": [{"nombre": "Aula Girasoles", "capacidad": 20, "disponibles": 6}],
     "beneficios": "Inglés, psicología"}
  ]
}
```
Alias aceptados: `cupos`/`vacantes_disponibles`, `mensualidad`/`pensión`, `codigo`/`sede_id`, `whatsapp`, etc.
Errores: cualquier HTTP ≥ 400 → el bot dice que no puede ver el dato y ofrece un asesor (no inventa).

## 3. Planes de pago y requisitos (productos: colegio / instituto)

Pueden venir **dentro de cada oferta** (`planes_pago`, `requisitos`) o en dos endpoints opcionales:

`GET {CAT_PATH_PLANES}?tipo=colegio&sede=1&nivel=Inicial`
```json
[{"nombre": "Pago mensual", "descripcion": "10 cuotas de pensión", "cuotas": 10, "monto_cuota": 420, "vigente_hasta": "2027-03-31"},
 {"nombre": "Pago anual adelantado", "descripcion": "Paga todo en marzo", "condiciones": "10% de descuento"}]
```
`GET {CAT_PATH_REQUISITOS}?tipo=instituto&sede=3&nivel=Educación Inicial`
```json
["Partida de nacimiento", "Certificado de estudios secundarios", {"requisito": "DNI", "detalle": "Del postulante y del apoderado"}]
```
`tipo` en las sedes (`"tipo": "colegio"` o `"instituto"`) es lo que separa los productos.
**Regla de oro:** si un dato (monto, porcentaje, fecha, requisito) no está en la API ni en *Panel → Asistente IA*, la IA no puede decirlo.

## Probar sin tu API
`CAT_API_MOCK=true` carga 3 sedes de demostración (**solo para pruebas**, nunca en producción).

## Qué hace la IA con esto
Herramientas disponibles: `listar_sedes`, `consultar_oferta`, `consultar_planes_pago`, `consultar_requisitos`, `registrar_interesado`, `mostrar_menu_alumno`.
Un interesado queda en **Panel → Interesados**, se avisa por WhatsApp al asesor de la sede y, si pidió hablar con una
persona, el bot se pausa para ese contacto (se reactiva desde el mismo panel).

**Aviso de WhatsApp:** un mensaje de texto libre al asesor solo llega si el asesor escribió al número del bot en las
últimas 24 h. Para garantizarlo, crea una plantilla aprobada por Meta (o pídele a cada asesor que escriba "hola" al bot
al iniciar su día). Si el aviso falla, el interesado igual queda en el panel marcado «sin aviso».
