from datetime import date
from decimal import Decimal

from src.validacion import (
    Decision,
    TipoDocumento,
    buscar_coincidencias,
    contar_coincidencias,
    decidir,
    evaluar_cuadro,
    evaluar_valor,
    parsear_fecha,
    parsear_monto,
    parsear_valor,
    siguiente_consecutivo,
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


def test_valor_con_centavos_se_procesa():
    assert evaluar_valor(Decimal("-1560671.14")) is None


def test_valor_con_mas_de_dos_decimales_se_revisa_a_mano():
    r = evaluar_valor(Decimal("-1560671.145"))
    assert r.decision == Decision.REVISAR_VALOR_CON_CENTAVOS


def test_centavos_con_mismo_valor_exacto_se_omite():
    # La Consulta muestra los centavos cuando los hay.
    filas = [fila("11421", "31/dic./25", "1,560,671.14"), fila("5985", "31/dic./24", "14,965,332.87")]
    r = decidir(filas, Decimal("-1560671.14"), PAGO)
    assert r.decision == Decision.OMITIR_YA_INGRESADO
    assert "11421" in r.motivo


def test_centavos_con_valor_distinto_se_carga():
    for mostrado in ("1,560,671", "1,560,672", "1,560,671.15", "1,560,671.13"):
        filas = [fila("11050", "31/dic./25", mostrado)]
        r = decidir(filas, Decimal("-1560671.14"), PAGO)
        assert r.decision == Decision.CARGAR


def test_centavos_con_mismo_valor_en_otra_fecha_se_carga():
    filas = [fila("5985", "31/dic./24", "1,560,671.14")]
    r = decidir(filas, Decimal("-1560671.14"), PAGO)
    assert r.decision == Decision.CARGAR


def test_centavos_con_comprobante_anulado_se_carga():
    filas = [fila("11421", "31/dic./25", "1,560,671.14", anulado="SI")]
    r = decidir(filas, Decimal("-1560671.14"), PAGO)
    assert r.decision == Decision.CARGAR


def test_valor_entero_no_acepta_diferencia_de_un_peso():
    filas = [fila("11050", "31/dic./25", "1,560,672")]
    r = decidir(filas, Decimal("-1560671"), PAGO)
    assert r.decision == Decision.CARGAR


def test_parsear_monto():
    assert parsear_monto("-$ 497,979.00") == Decimal("-497979.00")
    assert parsear_monto("$ 497,979.00") == Decimal("497979.00")
    assert parsear_monto("0.00") == Decimal("0")
    assert parsear_monto("") == Decimal("0")
    assert parsear_monto("1,560,671.14") == Decimal("1560671.14")


def test_cuadro_sin_documentos_se_revisa():
    r = evaluar_cuadro(0, Decimal("0"), Decimal("-5975935"))
    assert r.decision == Decision.REVISAR_SIN_DOCUMENTOS_PENDIENTES


def test_cuadro_abonado_diferente_se_revisa():
    r = evaluar_cuadro(12, Decimal("5000000"), Decimal("-5975935"))
    assert r.decision == Decision.REVISAR_ABONADO_DIFERENTE


def test_cuadro_que_coincide_continua():
    assert evaluar_cuadro(12, Decimal("5975935.00"), Decimal("-5975935")) is None


def test_cuadro_con_centavos_coincide_exacto():
    assert evaluar_cuadro(2, Decimal("1560671.14"), Decimal("-1560671.14")) is None


def test_cuadro_con_centavos_no_tolera_diferencias():
    r = evaluar_cuadro(2, Decimal("1560671.00"), Decimal("-1560671.14"))
    assert r.decision == Decision.REVISAR_ABONADO_DIFERENTE
    r = evaluar_cuadro(2, Decimal("1560671.15"), Decimal("-1560671.14"))
    assert r.decision == Decision.REVISAR_ABONADO_DIFERENTE


def test_contar_coincidencias():
    filas = [
        fila("1", "31/dic./25", "5,975,935"),
        fila("2", "31/dic./25", "5,975,935"),
        fila("3", "31/dic./25", "5,975,935", anulado="SI"),
        fila("4", "30/dic./25", "5,975,935"),
    ]
    assert contar_coincidencias(filas, Decimal("-5975935"), PAGO) == 2


def test_contar_coincidencias_con_centavos():
    filas = [
        fila("1", "31/dic./25", "1,560,671.14"),
        fila("2", "31/dic./25", "1,560,671"),
        fila("3", "31/dic./25", "1,560,671.14", anulado="SI"),
        fila("4", "31/dic./25", "1,560,671.14"),
    ]
    assert contar_coincidencias(filas, Decimal("-1560671.14"), PAGO) == 2
    assert [f[0] for f in buscar_coincidencias(filas, Decimal("-1560671.14"), PAGO)] == ["1", "4"]


def test_recibo_de_caja_exige_valor_positivo():
    recibo = TipoDocumento.RECIBO_CAJA
    assert evaluar_valor(Decimal("1315800"), recibo) is None
    assert evaluar_valor(Decimal("584999.06"), recibo) is None
    assert evaluar_valor(Decimal("-1315800"), recibo).decision == Decision.REVISAR_VALOR_NO_POSITIVO
    assert evaluar_valor(Decimal("0"), recibo).decision == Decision.REVISAR_VALOR_NO_POSITIVO


def test_comprobante_de_egreso_sigue_exigiendo_valor_negativo():
    egreso = TipoDocumento.COMPROBANTE_EGRESO
    assert evaluar_valor(Decimal("-100"), egreso) is None
    assert evaluar_valor(Decimal("100"), egreso).decision == Decision.REVISAR_VALOR_NO_NEGATIVO


def test_recibo_con_mas_de_dos_decimales_se_revisa():
    r = evaluar_valor(Decimal("100.123"), TipoDocumento.RECIBO_CAJA)
    assert r.decision == Decision.REVISAR_VALOR_CON_CENTAVOS


def test_recibo_ya_ingresado_se_omite_con_valor_positivo_del_excel():
    filas = [fila("237", "31/dic./25", "1,588,226,531")]
    r = decidir(filas, Decimal("1588226531"), PAGO)
    assert r.decision == Decision.OMITIR_YA_INGRESADO


def test_siguiente_consecutivo():
    filas = [fila("237", "31/dic./25", "1"), fila("233", "30/may./25", "1"), fila("226", "31/dic./24", "1")]
    assert siguiente_consecutivo(filas) == 238


def test_siguiente_consecutivo_toma_el_mayor_aunque_no_sea_el_primero():
    filas = [fila("226", "31/dic./24", "1"), fila("237", "31/dic./25", "1"), fila("233", "30/may./25", "1")]
    assert siguiente_consecutivo(filas) == 238


def test_siguiente_consecutivo_cuenta_anulados_y_acepta_separadores():
    filas = [fila("1,200", "31/dic./25", "1", anulado="SI"), fila("1,150", "30/may./25", "1")]
    assert siguiente_consecutivo(filas) == 1201


def test_siguiente_consecutivo_sin_recibos_previos_empieza_en_1():
    assert siguiente_consecutivo([]) == 1


def test_siguiente_consecutivo_222_da_223():
    assert siguiente_consecutivo([fila("222", "31/dic./24", "26,599,325")]) == 223


def test_siguiente_consecutivo_con_filas_ilegibles_devuelve_none():
    assert siguiente_consecutivo([fila("abc", "31/dic./25", "1")]) is None


def test_recibo_mismo_valor_en_otra_fecha_del_anio_se_omite_si_todo_el_anio():
    filas = [fila("240", "20/oct./26", "13,490,910")]
    hoy = date(2026, 10, 23)
    assert decidir(filas, Decimal("13490910"), hoy).decision == Decision.CARGAR
    r = decidir(filas, Decimal("13490910"), hoy, todo_el_anio=True)
    assert r.decision == Decision.OMITIR_YA_INGRESADO


def test_todo_el_anio_no_cuenta_otros_anios_ni_anulados():
    hoy = date(2026, 10, 23)
    filas = [
        fila("222", "31/dic./24", "13,490,910"),
        fila("240", "20/oct./26", "13,490,910", anulado="SI"),
    ]
    assert decidir(filas, Decimal("13490910"), hoy, todo_el_anio=True).decision == Decision.CARGAR