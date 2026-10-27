import re
import time
from decimal import Decimal

from src.pages.comprobante_egreso_page import NavegacionError
from src.pages.formulario_page import FormularioComprobante

FORMA_PAGO_RECIBO = "Recaudo en Banco Caja Social Ahorro"

JS_CAJA_RESPALDO = """el => {
    const candidatas = Array.from(el.querySelectorAll('input')).filter(i => {
        if (i.offsetParent === null || i.type === 'hidden' || i.readOnly || i.disabled) return false;
        const item = i.closest('.x-form-item');
        return item && item.getAttribute('title') === 'Documento respaldo';
    });
    // Hay dos cajas con ese título: Respaldo (a la izquierda) y Ref Doc (a la derecha).
    candidatas.sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x);
    return candidatas.length ? candidatas[0].id : null;
}"""

JS_CELDAS = """el => Array.from(el.querySelectorAll('.x-grid-cell-inner'))
    .filter(e => e.offsetParent !== null).map(c => c.innerText.trim()).filter(t => t)"""

# Posición del botón "aplicar/modificar línea" entre los 6 botones de ícono sin texto del
# cuadro 'Pagos del documento' (comprobado en la exploración: grabó el Respaldo en la línea).
TOTAL_BOTONES_ICONO = 6
INDICE_BOTON_APLICAR = 4


def aceptar_lista_forma_pago(formulario: FormularioComprobante) -> None:
    """Acepta SOLO la ventana de lista 'Seleccione la forma de pago'. No acepta los pagos."""
    page = formulario.page
    lista = page.get_by_role("dialog").filter(has_text="Seleccione la forma de pago")
    if lista.count() == 0:
        raise NavegacionError("No está abierta la lista 'Seleccione la forma de pago'")
    lista.last.locator("a.x-btn:visible", has_text=re.compile(r"^\s*Aceptar\s*$")).first.click(
        timeout=5000
    )
    limite = time.monotonic() + 10
    while lista.count() > 0 and time.monotonic() < limite:
        page.wait_for_timeout(300)
    if lista.count() > 0:
        raise NavegacionError("La lista de formas de pago sigue abierta tras Aceptar")
    page.wait_for_timeout(1500)
    celdas = formulario._cuadro_pagos().evaluate(JS_CELDAS)
    if celdas.count(FORMA_PAGO_RECIBO) != 1:
        raise NavegacionError(
            f"El cuadro de pagos debía mostrar una sola línea '{FORMA_PAGO_RECIBO}': {celdas}"
        )
    formulario.logger.info("Lista de formas de pago aceptada; línea de pago creada")


def escribir_respaldo(formulario: FormularioComprobante, numero: int) -> None:
    """Escribe el consecutivo en la caja Respaldo de 'Pagos del documento' y lo verifica.

    Requiere que la lista de formas de pago ya se haya aceptado (la caja queda editable).
    No presiona Aceptar ni Grabar.
    """
    cuadro = formulario._cuadro_pagos()
    ident = cuadro.evaluate(JS_CAJA_RESPALDO)
    if not ident:
        formulario.c._captura_segura("error_caja_respaldo")
        raise NavegacionError("La caja Respaldo no está editable en 'Pagos del documento'")
    caja = formulario.page.locator(f'[id="{ident}"]')
    caja.click()
    caja.press("Control+A")
    caja.press_sequentially(str(numero), delay=60)
    caja.press("Tab")
    formulario.page.wait_for_timeout(1000)
    escrito = caja.input_value().strip()
    if escrito != str(numero):
        raise NavegacionError(f"Respaldo quedó '{escrito}' y se esperaba '{numero}'")
    formulario.logger.info(f"Respaldo escrito: {numero}")


def aplicar_respaldo(formulario: FormularioComprobante, numero: int) -> None:
    """Escribe el Respaldo y presiona el botón que lo aplica a la línea de pago.

    Verifica que la línea muestre el consecutivo (una celda nueva con ese valor) y que siga
    habiendo una sola línea. No presiona Aceptar ni Grabar.
    """
    cuadro = formulario._cuadro_pagos()
    antes = cuadro.evaluate(JS_CELDAS)
    escribir_respaldo(formulario, numero)

    iconos = cuadro.locator("a.x-btn:visible").filter(has_not_text=re.compile(r"\S"))
    if iconos.count() != TOTAL_BOTONES_ICONO:
        raise NavegacionError(
            f"Se esperaban {TOTAL_BOTONES_ICONO} botones de ícono y hay {iconos.count()}"
        )
    boton = iconos.nth(INDICE_BOTON_APLICAR)
    if "disabled" in (boton.get_attribute("class") or ""):
        raise NavegacionError("El botón para aplicar el Respaldo a la línea está deshabilitado")
    boton.click(timeout=5000)
    formulario.page.wait_for_timeout(2000)

    despues = cuadro.evaluate(JS_CELDAS)
    if (
        len(despues) != len(antes) + 1
        or str(numero) not in despues
        or despues.count(FORMA_PAGO_RECIBO) != 1
    ):
        formulario.c._captura_segura("error_aplicar_respaldo")
        raise NavegacionError(
            f"La línea de pago no quedó con Respaldo {numero}. Antes: {antes} | Después: {despues}"
        )
    formulario.logger.info(f"Respaldo {numero} aplicado a la línea de pago")


def verificar_pago_recibo(formulario: FormularioComprobante, esperado: Decimal) -> list:
    """Lista de problemas; vacía si Total Pagos iguala al Excel y la Diferencia es 0.

    En recibos los Débitos son los de la cartera, por eso no se comparan con el Excel.
    """
    t = formulario.leer_totales()
    problemas = []
    if t["Total Pagos"] != esperado:
        problemas.append(f"Total Pagos {t['Total Pagos']} distinto del Excel {esperado}")
    if t["Diferencia"] != 0:
        problemas.append(f"Diferencia {t['Diferencia']} distinta de 0")
    return problemas


# --- Flujo de pagos de recibos de caja (comprobado a mano en la plataforma) ---------------
# 1) Elegir la forma de pago en la lista y aceptar SOLO la lista: la línea se crea sola, con
#    el Valor igual a la Deuda del cliente.
# 2) Seleccionar la línea, escribir el consecutivo en Respaldo y presionar el botón de ícono
#    que aplica el cambio a la línea (5.º botón sin texto): la grilla muestra el Respaldo y
#    Total Pagos pasa a ser el valor del recibo.
# 3) Aceptar 'Pagos del documento'.

import re
from collections import Counter

FORMA_PAGO_RECIBO = "Recaudo en Banco Caja Social Ahorro"
TOTAL_BOTONES_ICONO = 6
INDICE_BOTON_APLICAR = 4


def aceptar_lista_formas_pago(formulario: FormularioComprobante, forma: str) -> None:
    """Acepta solo la ventana 'Seleccione la forma de pago' y verifica que la línea apareció."""
    lista = formulario.page.get_by_role("dialog").filter(has_text="Seleccione la forma de pago")
    if lista.count() != 1:
        raise NavegacionError(f"Se esperaba 1 lista de formas de pago y hay {lista.count()}")
    lista.first.locator("a.x-btn:visible", has_text=re.compile(r"^\s*Aceptar\s*$")).first.click(
        timeout=5000
    )
    for _ in range(30):
        if lista.count() == 0:
            break
        formulario.page.wait_for_timeout(300)
    else:
        raise NavegacionError("La lista de formas de pago sigue abierta tras Aceptar")
    formulario.page.wait_for_timeout(1500)
    cuadro = formulario._cuadro_pagos()
    patron = formulario._patron_forma(forma)
    if cuadro.get_by_text(patron).locator("visible=true").count() == 0:
        raise NavegacionError(f"'Pagos del documento' no muestra la línea de '{forma}'")
    formulario.logger.info("Lista de formas de pago aceptada; línea de pago creada")


def _celdas_grilla(cuadro) -> list[str]:
    textos = cuadro.locator(".x-grid-cell-inner:visible").all_inner_texts()
    return [t.strip() for t in textos if t.strip()]


def aplicar_respaldo(formulario: FormularioComprobante, numero: int, forma: str) -> None:
    """Escribe el Respaldo y lo aplica a la línea de pago. No acepta 'Pagos del documento'."""
    cuadro = formulario._cuadro_pagos()
    celda = cuadro.locator(".x-grid-cell-inner", has_text=formulario._patron_forma(forma)).first
    celda.click(timeout=5000)
    formulario.page.wait_for_timeout(800)
    escribir_respaldo(formulario, numero)

    antes = _celdas_grilla(cuadro)
    iconos = cuadro.locator("a.x-btn:visible").filter(has_not_text=re.compile(r"\S"))
    if iconos.count() != TOTAL_BOTONES_ICONO:
        raise NavegacionError(
            f"Se esperaban {TOTAL_BOTONES_ICONO} botones de ícono y hay {iconos.count()}"
        )
    boton = iconos.nth(INDICE_BOTON_APLICAR)
    if "disabled" in (boton.get_attribute("class") or ""):
        raise NavegacionError("El botón para aplicar el Respaldo a la línea está deshabilitado")
    boton.click(timeout=5000)
    formulario.page.wait_for_timeout(2000)

    despues = _celdas_grilla(cuadro)
    if Counter(despues) != Counter(antes + [str(numero)]):
        raise NavegacionError(
            f"La grilla no tomó el Respaldo {numero}. Antes: {antes} | Después: {despues}"
        )
    formulario.logger.info(f"Respaldo {numero} aplicado a la línea de pago")


def verificar_pago_recibo(formulario: FormularioComprobante, esperado) -> list:
    """Problemas del pago de un recibo; vacía si Total Pagos = Excel y Diferencia = 0.

    En recibos los Débitos no igualan al valor (incluyen otros movimientos de la cartera),
    por eso solo se exige Total Pagos y Diferencia.
    """
    t = formulario.leer_totales()
    problemas = []
    if t["Total Pagos"] != esperado:
        problemas.append(f"Total Pagos {t['Total Pagos']} distinto del Excel {esperado}")
    if t["Diferencia"] != 0:
        problemas.append(f"Diferencia {t['Diferencia']} distinta de 0")
    return problemas