"""
Registro de flujos guiados. Para AÑADIR una opción nueva (p. ej. "solicitar constancia"):
  1. Crea app/flujos/mi_flujo.py con  iniciar(ctx)  y  continuar(ctx, estado) -> bool.
  2. Regístrala aquí en ACCIONES (id de botón -> iniciar) y CONTINUADORES (nombre de flujo -> continuar).
  3. En el editor del panel agrega un botón con ese id en el menú que quieras.
"""
from . import alumno, asesor, comprobante, consulta

ACCIONES = {
    "acc_alumno": alumno.iniciar,
    "acc_asesor": asesor.iniciar,
    "acc_deudas": consulta.iniciar_deudas,
    "acc_notas": consulta.iniciar_notas,
    "comp_enviar": comprobante.iniciar,
}

CONTINUADORES = {
    "alumno": alumno.continuar,
    "asesor": asesor.continuar,
    "consulta": consulta.continuar,
    "comprobante": comprobante.continuar,
}
