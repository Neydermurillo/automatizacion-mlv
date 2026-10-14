from datetime import date
from decimal import Decimal

from src.validacion import (
    Decision,
    contar_coincidencias,
    decidir,
    evaluar_cuadro,
    evaluar_valor,
    parsear_fecha,
    parsear_monto,
    parsear_valor,
)

PAGO = date(2025, 12, 31)


def fila(num, fecha, valor, anulado="NO"):
    return [num, "1", "CE", "1,000", "TERCERO", fecha, valor, "ERP1", anulado]


def test_parsear_fecha():
    assert parsear_fecha("07/oct./26") == date(2026, 10, 7)
    assert parsear_fecha("03/sept./26") == date(2026, 9, 3)
    assert parsear_fecha("31/dic./25") == date(2025, 12, 31)


def test_parsear_valor():
    assert parsear_valor("1,732,500") == Decimal("1732500")


def test_omite_si_fecha_de_pago_y_mismo_valor():
    filas = [fila("11044", "31/dic./25", "2,987,994")]
    r = decidir(filas, Decimal("-2987994.00"), PAGO)
    assert r.decision == Decision.OMITIR_YA_INGRESADO


def test_carga_si_fecha_de_pago_con_otro_valor():
    filas = [fila("11044", "31/dic./25", "2,987,994")]
    r = decidir(filas, Decimal("-1859928"), PAGO)
    assert r.decision == Decision.CARGAR


def test_carga_si_mismo_valor_en_otra_fecha():
    filas = [fila("8953", "31/jul./25", "1,493,952"), fila("9460", "29/ago./25", "1,493,952")]
    r = decidir(filas, Decimal("-1493952"), PAGO)
    assert r.decision == Decision.CARGAR


def test_registro_reciente_ya_no_cuenta_como_ingresado():
    filas = [fila("65", "07/oct./26", "1,000,000")]
    r = decidir(filas, Decimal("-1000000"), PAGO)
    assert r.decision == Decision.CARGAR


def test_ignora_anulados():
    del_pago = [fila("11044", "31/dic./25", "2,987,994", anulado="SI")]
    assert decidir(del_pago, Decimal("-2987994"), PAGO).decision == Decision.CARGAR


def test_sin_filas_se_carga():
    r = decidir([], Decimal("-500"), PAGO)
    assert r.decision == Decision.CARGAR


def test_valor_no_negativo_se_revisa_a_mano():
    assert evaluar_valor(Decimal("200000")).decision == Decision.REVISAR_VALOR_NO_NEGATIVO
    assert evaluar_valor(Decimal("0")).decision == Decision.REVISAR_VALOR_NO_NEGATIVO


def test_valor_negativo_se_procesa():
    assert evaluar_valor(Decimal("-5975935")) is None
    assert evaluar_valor(Decimal("-5975935.0")) is None


def test_valor_con_centavos_se_revisa_a_mano():
    r = evaluar_valor(Decimal("-1560671.14"))
    assert r.decision == Decision.REVISAR_VALOR_CON_CENTAVOS


def test_parsear_monto():
    assert parsear_monto("-$ 497,979.00") == Decimal("-497979.00")
    assert parsear_monto("$ 497,979.00") == Decimal("497979.00")
    assert parsear_monto("0.00") == Decimal("0")
    assert parsear_monto("") == Decimal("0")


def test_cuadro_sin_documentos_se_revisa():
    r = evaluar_cuadro(0, Decimal("0"), Decimal("-5975935"))
    assert r.decision == Decision.REVISAR_SIN_DOCUMENTOS_PENDIENTES


def test_cuadro_abonado_diferente_se_revisa():
    r = evaluar_cuadro(12, Decimal("5000000"), Decimal("-5975935"))
    assert r.decision == Decision.REVISAR_ABONADO_DIFERENTE


def test_cuadro_que_coincide_continua():
    assert evaluar_cuadro(12, Decimal("5975935.00"), Decimal("-5975935")) is None


def test_contar_coincidencias():
    filas = [
        fila("1", "31/dic./25", "5,975,935"),
        fila("2", "31/dic./25", "5,975,935"),
        fila("3", "31/dic./25", "5,975,935", anulado="SI"),
        fila("4", "30/dic./25", "5,975,935"),
    ]
    assert contar_coincidencias(filas, Decimal("-5975935"), PAGO) == 2