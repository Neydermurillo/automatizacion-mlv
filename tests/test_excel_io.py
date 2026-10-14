from decimal import Decimal

import pytest

from src.excel_io import parsear_nit, parsear_valor_excel


def test_nit():
    assert parsear_nit(3989120.0) == "3989120"
    assert parsear_nit("3.989.120") == "3989120"
    with pytest.raises(ValueError):
        parsear_nit("900123456-7")


def test_valor_numerico():
    assert parsear_valor_excel(-2987994.0) == Decimal("-2987994")


def test_valor_texto():
    assert parsear_valor_excel("-2.987.994,00") == Decimal("-2987994.00")
    assert parsear_valor_excel("2,987,994") == Decimal("2987994")


def test_valor_ambiguo_se_rechaza():
    with pytest.raises(ValueError):
        parsear_valor_excel("1,50")