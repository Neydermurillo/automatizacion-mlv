from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum

# Posición de cada columna en las filas que devuelve leer_filas()
COL_NUMERO, COL_FECHA, COL_VALOR, COL_ANULADO = 0, 5, 6, 8

MESES = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dic": 12,
}


class Decision(Enum):
    CARGAR = "cargar"
    OMITIR_YA_INGRESADO = "omitir: ya ingresado (mismo valor)"
    REVISAR_SIN_COMPROBANTES = "revisar a mano: el NIT no tiene comprobantes"
    REVISAR_VALOR_NO_NEGATIVO = "revisar a mano: el valor del Excel no es negativo"
    REVISAR_VALOR_CON_CENTAVOS = "revisar a mano: el valor del Excel tiene centavos"
    ERROR_LECTURA = "error: revisar a mano"
    REVISAR_SIN_DOCUMENTOS_PENDIENTES = "revisar a mano: la cartera no tiene documentos de 2025 por pagar"
    REVISAR_ABONADO_DIFERENTE = "revisar a mano: el total abonado no coincide con el Excel"
    LISTO_PARA_CARGAR = "listo para cargar (simulación: no se grabó nada)"
    REVISAR_PAGO_NO_VERIFICADO = "revisar a mano: el pago no se pudo verificar"
    CARGADO = "cargado: comprobante grabado y verificado en la Consulta"
    ERROR_CARGA_INCIERTA = "ERROR: no se pudo confirmar si el comprobante quedó grabado; revisar a mano"


@dataclass(frozen=True)
class Resultado:
    decision: Decision
    motivo: str


def parsear_fecha(texto: str) -> date:
    """'07/oct./26' -> date(2026, 10, 7). Acepta 'sept.' y 'dic.'."""
    dia, mes, anio = texto.strip().split("/")
    mes_num = MESES[mes.strip(".").lower()]
    anio_num = int(anio) + (2000 if len(anio) == 2 else 0)
    return date(anio_num, mes_num, int(dia))


def parsear_valor(texto: str) -> Decimal:
    """'1,732,500' -> Decimal('1732500')."""
    return Decimal(texto.replace(",", "").strip())


def parsear_monto(texto: str) -> Decimal:
    """'-$ 497,979.00' -> Decimal('-497979.00'); '$ 497,979.00' -> 497979.00; '' -> 0."""
    limpio = texto.replace("$", "").replace(",", "").replace(" ", "").replace("\xa0", "").strip()
    return Decimal(limpio) if limpio else Decimal(0)


def evaluar_valor(valor_excel: Decimal) -> Resultado | None:
    """Filtro previo: solo se procesan valores negativos y sin centavos.

    La Consulta muestra los valores sin centavos, así que un valor con centavos no se
    puede verificar al peso: se deja para revisión manual en vez de aproximarlo.
    """
    if valor_excel >= 0:
        return Resultado(
            Decision.REVISAR_VALOR_NO_NEGATIVO,
            f"Valor del Excel no negativo: {valor_excel}",
        )
    if valor_excel != valor_excel.to_integral_value():
        return Resultado(
            Decision.REVISAR_VALOR_CON_CENTAVOS,
            f"El valor del Excel tiene centavos ({valor_excel}) y la Consulta los muestra "
            "sin centavos; no se puede verificar al peso",
        )
    return None


def decidir(
    filas: list[list[str]],
    valor_excel: Decimal,
    fecha_pago: date,
) -> Resultado:
    """Decide si un registro del Excel se carga o se omite.

    - Sin comprobantes previos: se carga (el cuadro de cartera confirma si hay algo por pagar).
    - Ignora comprobantes anulados.
    - Omite si hay uno con fecha_pago y exactamente el mismo valor (el Excel trae los
      valores en negativo, por eso se compara en absoluto).
    """
    if not filas:
        return Resultado(
            Decision.CARGAR,
            "NIT sin comprobantes previos; el cuadro de cartera confirma si hay algo por pagar",
        )

    for fila in filas:
        if fila[COL_ANULADO] == "SI":
            continue
        fecha = parsear_fecha(fila[COL_FECHA])
        valor = parsear_valor(fila[COL_VALOR])

        if fecha == fecha_pago and valor == abs(valor_excel):
            return Resultado(
                Decision.OMITIR_YA_INGRESADO,
                f"Comprobante {fila[COL_NUMERO]} del {fila[COL_FECHA]} por {fila[COL_VALOR]}",
            )

    return Resultado(Decision.CARGAR, "Sin comprobantes que coincidan")


def evaluar_cuadro(marcadas: int, abonado: Decimal, valor_excel: Decimal) -> Resultado | None:
    """Verifica el cuadro de cartera. Devuelve None si todo coincide (se puede continuar)."""
    esperado = abs(valor_excel)
    if marcadas == 0:
        return Resultado(
            Decision.REVISAR_SIN_DOCUMENTOS_PENDIENTES,
            "No hay documentos de 2025 con saldo en la cartera del tercero "
            "(posible pago ya registrado)",
        )
    if abonado != esperado:
        return Resultado(
            Decision.REVISAR_ABONADO_DIFERENTE,
            f"ABONADO {abonado} distinto del valor del Excel {esperado} "
            f"({marcadas} documentos marcados)",
        )
    return None


def contar_coincidencias(filas: list[list[str]], valor_excel: Decimal, fecha_pago: date) -> int:
    """Cuenta los comprobantes no anulados con la fecha de pago y exactamente el mismo valor."""
    return sum(
        1
        for fila in filas
        if fila[COL_ANULADO] != "SI"
        and parsear_fecha(fila[COL_FECHA]) == fecha_pago
        and parsear_valor(fila[COL_VALOR]) == abs(valor_excel)
    )