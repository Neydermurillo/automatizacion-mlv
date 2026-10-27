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


class TipoDocumento(Enum):
    COMPROBANTE_EGRESO = "Comprobante de egreso"
    RECIBO_CAJA = "Recibo de caja"


class Decision(Enum):
    CARGAR = "cargar"
    OMITIR_YA_INGRESADO = "omitir: ya ingresado (mismo valor)"
    REVISAR_SIN_COMPROBANTES = "revisar a mano: el NIT no tiene comprobantes"
    REVISAR_VALOR_NO_NEGATIVO = "revisar a mano: el valor del Excel no es negativo"
    REVISAR_VALOR_NO_POSITIVO = "revisar a mano: el valor del Excel no es positivo"
    REVISAR_VALOR_CON_CENTAVOS = "revisar a mano: el valor del Excel tiene más de 2 decimales"
    REVISAR_SIN_RESPALDO = "revisar a mano: no se pudo calcular el consecutivo de Respaldo"
    ERROR_LECTURA = "error: revisar a mano"
    REVISAR_SIN_DOCUMENTOS_PENDIENTES = "revisar a mano: la cartera no tiene documentos del año por pagar"
    REVISAR_ABONADO_DIFERENTE = "revisar a mano: el total abonado no coincide con el Excel"
    LISTO_PARA_CARGAR = "listo para cargar (simulación: no se grabó nada)"
    REVISAR_CARTERA_INCOMPLETA = "revisar a mano: la cartera tiene más documentos de los que caben en pantalla"
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


def evaluar_valor(
    valor_excel: Decimal, tipo: TipoDocumento = TipoDocumento.COMPROBANTE_EGRESO
) -> Resultado | None:
    """Filtro previo según el tipo de documento, con a lo sumo 2 decimales.

    - Comprobante de egreso: el valor del Excel debe ser negativo.
    - Recibo de caja: el valor del Excel debe ser positivo.

    Los centavos son válidos: el cuadro de cartera los muestra y el ABONADO debe coincidir
    exactamente. Más de 2 decimales no es un valor monetario normal: se deja para revisión.
    """
    if tipo == TipoDocumento.RECIBO_CAJA:
        if valor_excel <= 0:
            return Resultado(
                Decision.REVISAR_VALOR_NO_POSITIVO,
                f"Valor del Excel no positivo: {valor_excel}",
            )
    elif valor_excel >= 0:
        return Resultado(
            Decision.REVISAR_VALOR_NO_NEGATIVO,
            f"Valor del Excel no negativo: {valor_excel}",
        )
    if valor_excel != valor_excel.quantize(Decimal("0.01")):
        return Resultado(
            Decision.REVISAR_VALOR_CON_CENTAVOS,
            f"El valor del Excel tiene más de 2 decimales ({valor_excel}); revisar el Excel",
        )
    return None


def _mismo_periodo(fecha: date, fecha_pago: date, todo_el_anio: bool) -> bool:
    return fecha.year == fecha_pago.year if todo_el_anio else fecha == fecha_pago


def buscar_coincidencias(
    filas: list[list[str]],
    valor_excel: Decimal,
    fecha_pago: date,
    todo_el_anio: bool = False,
) -> list[list[str]]:
    """Comprobantes no anulados con la fecha de pago y exactamente el valor del Excel.

    Con todo_el_anio=True basta que sean del mismo año de fecha_pago (recibos de caja: la
    fecha cambia cada día de ejecución, así una segunda corrida detecta lo ya grabado).

    La Consulta muestra los centavos cuando los hay ('1,560,671.14'), así que la
    comparación es exacta, sin tolerancia.
    """
    esperado = abs(valor_excel)
    return [
        fila
        for fila in filas
        if fila[COL_ANULADO] != "SI"
        and _mismo_periodo(parsear_fecha(fila[COL_FECHA]), fecha_pago, todo_el_anio)
        and parsear_valor(fila[COL_VALOR]) == esperado
    ]


def contar_coincidencias(filas: list[list[str]], valor_excel: Decimal, fecha_pago: date) -> int:
    return len(buscar_coincidencias(filas, valor_excel, fecha_pago))


def siguiente_consecutivo(filas: list[list[str]]) -> int | None:
    """Número que sigue al más alto de la Consulta (237 -> 238).

    NIT sin recibos previos (Consulta vacía): empieza en 1. Si hay filas pero ninguna con
    número legible devuelve None (se revisa a mano).

    Se usa para el campo Respaldo de los recibos de caja. Cuenta todas las filas, también
    las anuladas, porque su número ya fue usado.
    """
    if not filas:
        return 1
    numeros = []
    for fila in filas:
        texto = fila[COL_NUMERO].replace(",", "").replace(".", "").strip()
        if texto.isdigit():
            numeros.append(int(texto))
    return max(numeros) + 1 if numeros else None


def describir_comprobante(fila: list[str]) -> str:
    return f"Comprobante {fila[COL_NUMERO]} del {fila[COL_FECHA]} por {fila[COL_VALOR]}"


def decidir(
    filas: list[list[str]],
    valor_excel: Decimal,
    fecha_pago: date,
    todo_el_anio: bool = False,
) -> Resultado:
    """Decide si un registro del Excel se carga, se omite o se revisa a mano.

    - Sin comprobantes previos: se carga (el cuadro de cartera confirma si hay algo por pagar).
    - Ignora comprobantes anulados.
    - Omite si hay uno con fecha_pago y exactamente el mismo valor (centavos incluidos).
    """
    if not filas:
        return Resultado(
            Decision.CARGAR,
            "NIT sin comprobantes previos; el cuadro de cartera confirma si hay algo por pagar",
        )

    coincidentes = buscar_coincidencias(filas, valor_excel, fecha_pago, todo_el_anio)
    if coincidentes:
        return Resultado(Decision.OMITIR_YA_INGRESADO, describir_comprobante(coincidentes[0]))

    return Resultado(Decision.CARGAR, "Sin comprobantes que coincidan")


def evaluar_cuadro(marcadas: int, abonado: Decimal, valor_excel: Decimal) -> Resultado | None:
    """Verifica el cuadro de cartera. Devuelve None si todo coincide (se puede continuar).

    La comparación es exacta, centavos incluidos: sin tolerancia.
    """
    esperado = abs(valor_excel)
    if marcadas == 0:
        return Resultado(
            Decision.REVISAR_SIN_DOCUMENTOS_PENDIENTES,
            "No hay documentos del año con saldo en la cartera del tercero "
            "(posible pago ya registrado)",
        )
    if abonado != esperado:
        return Resultado(
            Decision.REVISAR_ABONADO_DIFERENTE,
            f"ABONADO {abonado} distinto del valor del Excel {esperado} "
            f"({marcadas} documentos marcados)",
        )
    return None