"""Exploración de Recibos Caja. NO graba nada.

Uso:
    python explorar_recibo.py <NIT>              # busca el NIT y muestra sus recibos
    python explorar_recibo.py <NIT> formulario   # además abre Adicionar, lo describe y lo cierra
    python explorar_recibo.py <NIT> pagos        # recorre el proceso hasta 'Pagos del documento'
    python explorar_recibo.py <NIT> cartera      # solo estudia el cuadro de cartera (NIT con 50+ docs)
    python explorar_recibo.py <NIT> paginas      # NIT con más de 200 docs: ¿se conservan las marcas al cambiar de página?

Modo 'pagos': abre el formulario, fija la fecha de HOY, carga el cliente, marca los
documentos del año de hoy en el cuadro de cartera, acepta el cuadro, elige la forma de pago y
describe el cuadro 'Pagos del documento' (campo Respaldo incluido) y los totales.
Nunca presiona Grabar ni acepta 'Pagos del documento'; al final cierra con Cancelar.
Usa un NIT que tenga documentos del año de hoy por cobrar en su cartera.
No se imprimen nombres de terceros, solo número, fecha y valor de cada recibo.
"""
import re
import sys
from dataclasses import replace
from datetime import date

from playwright.sync_api import sync_playwright

from src.config import cargar_config
from src.logger import crear_logger
from src.pages.formulario_page import FormularioComprobante
from src.pages.login_page import LoginPage
from src.pages.pago_recibo import escribir_respaldo
from src.pages.recibo_caja_page import ReciboCajaPage
from src.validacion import (
    COL_ANULADO,
    COL_FECHA,
    COL_NUMERO,
    COL_VALOR,
    siguiente_consecutivo,
)

FORMA_PAGO_RECIBO = "Recaudo en Banco Caja Social Ahorro"
ETIQUETAS_TOTALES = ("Debitos", "Creditos", "Total Pagos", "Diferencia", "Abonos", "Otros", "Deuda")

JS_DESCRIBIR = """el => {
    const visible = e => e.offsetParent !== null;
    const cabeceras = Array.from(el.querySelectorAll('.x-column-header, .x-grid-header-ct *'))
        .filter(h => visible(h) && h.children.length === 0 && h.textContent.trim())
        .map(h => { const r = h.getBoundingClientRect();
            return {t: h.textContent.trim(), x1: r.left, x2: r.right, y: r.top}; });
    const cabeceraDe = (r) => {
        const cx = r.left + r.width / 2;
        const c = cabeceras.filter(h => h.x1 - 2 <= cx && cx <= h.x2 + 2 && h.y < r.top)
            .sort((a, b) => b.y - a.y)[0];
        return c ? c.t : null;
    };
    const campos = Array.from(el.querySelectorAll('input')).filter(visible).map(i => {
        const item = i.closest('.x-form-item');
        const etiqueta = item ? item.querySelector('.x-form-item-label') : null;
        const r = i.getBoundingClientRect();
        return {
            titulo: item ? item.getAttribute('title') : null,
            etiqueta: etiqueta ? etiqueta.innerText.trim() : null,
            valor: i.value,
            solo_lectura: i.readOnly,
            deshabilitado: i.disabled || (item ? item.className.includes('x-item-disabled') : false),
            x: Math.round(r.x), y: Math.round(r.y), ancho: Math.round(r.width),
            columna: cabeceraDe(r),
        };
    });
    const botones = Array.from(el.querySelectorAll('a.x-btn')).filter(visible)
        .map(b => {
            const r = b.getBoundingClientRect();
            const icono = b.querySelector('.x-btn-icon-el, [class*=icon]');
            return {
                texto: b.innerText.trim(),
                clase_icono: icono ? icono.className : null,
                deshabilitado: b.className.includes('disabled'),
                x: Math.round(r.x), y: Math.round(r.y),
            };
        });
    const textos = Array.from(el.querySelectorAll('*'))
        .filter(e => e.children.length === 0 && visible(e))
        .map(e => e.textContent.trim())
        .filter(t => t && t.length <= 40);
    const filas = Array.from(el.querySelectorAll(
            'tr.x-grid-row, .x-grid-item, .x-grid-row, [role=row]'))
        .filter(visible).slice(0, 8)
        .map(tr => tr.innerText.replace(/\\s+/g, ' ').trim()).filter(t => t);
    const celdas = Array.from(el.querySelectorAll('.x-grid-cell-inner'))
        .filter(visible).map(c => c.innerText.trim()).filter(t => t).slice(0, 40);
    return {campos, botones, filas, celdas, textos: Array.from(new Set(textos)).slice(0, 80)};
}"""

JS_ID_RESPALDO = '''el => {
    const c = Array.from(el.querySelectorAll('input')).filter(i => {
        if (i.offsetParent === null || i.type === 'hidden' || i.readOnly || i.disabled) return false;
        const item = i.closest('.x-form-item');
        return item && item.getAttribute('title') === 'Documento respaldo';
    }).sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x);
    return c.length ? c[0].id : null;
}'''

JS_TOTAL = r"""(el, nombre) => {
    const norm = s => s.normalize('NFD').replace(/[̀-ͯ]/g, '').trim().toLowerCase();
    const objetivo = norm(nombre);
    const etiqueta = Array.from(el.querySelectorAll('*')).find(
        e => e.children.length === 0 && e.offsetParent !== null && norm(e.textContent) === objetivo);
    if (!etiqueta) return null;
    const r = etiqueta.getBoundingClientRect();
    let mejor = null, distancia = 1e9;
    for (const i of el.querySelectorAll('input')) {
        if (i.offsetParent === null || i.type === 'hidden') continue;
        const b = i.getBoundingClientRect();
        const dx = b.left - r.right;
        const dy = Math.abs((b.top + b.height / 2) - (r.top + r.height / 2));
        if (dx >= -4 && dy < 12 && dx < distancia) { mejor = i; distancia = dx; }
    }
    return mejor ? mejor.value : null;
}"""


def mostrar(logger, texto: str) -> None:
    print(texto)
    logger.info(texto)


def describir_ventana(logger, ventana, titulo: str) -> None:
    info = ventana.evaluate(JS_DESCRIBIR)
    for campo in info["campos"]:
        mostrar(logger, f"[{titulo}] campo: {campo}")
    mostrar(logger, f"[{titulo}] botones: {info['botones']}")
    mostrar(logger, f"[{titulo}] filas: {info['filas']}")
    mostrar(logger, f"[{titulo}] celdas de grilla: {info['celdas']}")
    mostrar(logger, f"[{titulo}] textos visibles: {info['textos']}")


def mostrar_totales(pagina: ReciboCajaPage, logger, etiqueta: str) -> None:
    formulario = pagina._formulario()
    totales = {n: formulario.evaluate(JS_TOTAL, n) for n in ETIQUETAS_TOTALES}
    mostrar(logger, f"[{etiqueta}] totales del formulario: {totales}")


def explorar_formulario(pagina: ReciboCajaPage, logger) -> None:
    page = pagina.page
    pagina._ventana_consulta().get_by_text("Adicionar", exact=True).locator(
        "visible=true"
    ).first.click()
    page.wait_for_timeout(4000)
    mostrar(logger, f"Ventanas: {pagina._describir_ventanas()}")
    describir_ventana(logger, page.get_by_role("dialog").last, "formulario")


JS_VISTAS = """el => Array.from(el.querySelectorAll('.x-grid-view'))
    .filter(v => v.offsetParent !== null)
    .map(v => ({clase: String(v.className).slice(0, 60), alto: v.clientHeight,
                alto_total: v.scrollHeight, desplazado: v.scrollTop}))"""

JS_BAJAR = """el => {
    const vistas = Array.from(el.querySelectorAll('.x-grid-view')).filter(v => v.offsetParent !== null);
    vistas.forEach(v => { v.scrollTop = v.scrollHeight; });
    return vistas.length;
}"""

JS_FILAS_IDX = """el => Array.from(el.querySelectorAll('tr.x-grid-row')).map(tr => ({
    attrs: Object.fromEntries(Array.from(tr.attributes).map(a => [a.name, String(a.value).slice(0, 40)])),
    padre: (tr.closest('table') || {}).id || '',
    vista: (tr.closest('.x-grid-view') || {}).id || '',
    texto: tr.innerText.replace(/\\s+/g, ' ').trim().slice(0, 140)}))"""

JS_MOVER = """(el, y) => {
    const v = Array.from(el.querySelectorAll('.x-grid-view')).find(x => x.offsetParent !== null);
    v.scrollTop = y;
    return {arriba: v.scrollTop, total: v.scrollHeight, alto: v.clientHeight};
}"""

JS_SUBIR = """el => {
    Array.from(el.querySelectorAll('.x-grid-view')).forEach(v => { v.scrollTop = 0; });
}"""


def resumen_filas_cartera(formulario, logger, etiqueta: str) -> None:
    datos = formulario._leer_cuadro()
    ids = {h["texto"]: h["id"] for h in datos["encabezados"]}
    filas = datos["filas"]
    id_fecha, id_saldo = ids.get("Fecha"), ids.get("Saldo")
    muestra = lambda f: (f.get(id_fecha), f.get(id_saldo))  # noqa: E731
    mostrar(logger, f"[{etiqueta}] filas dibujadas: {len(filas)}")
    if filas:
        mostrar(logger, f"[{etiqueta}] primera (Fecha, Saldo): {muestra(filas[0])}")
        mostrar(logger, f"[{etiqueta}] última   (Fecha, Saldo): {muestra(filas[-1])}")
        fechas = [f.get(id_fecha) for f in filas]
        mostrar(logger, f"[{etiqueta}] fechas distintas dibujadas: {sorted(set(fechas))[:12]}")
    mostrar(logger, f"[{etiqueta}] total según 'Mostrando': {formulario._total_filas_cuadro()}")


def explorar_cartera(pagina: ReciboCajaPage, nit: str, logger) -> None:
    """Estudia el cuadro de cartera de un NIT con muchos documentos. Solo lee y cancela."""
    formulario = FormularioComprobante(pagina)
    pagina.abrir_formulario()
    formulario.fijar_fecha(pagina.config.fecha_pago)
    formulario.cargar_cliente(nit)
    formulario.abrir_cuadro_cartera()
    cuadro = formulario.c._cuadro_cartera()

    resumen_filas_cartera(formulario, logger, "cartera inicial")
    describir_ventana(logger, cuadro, "cartera inicial")
    mostrar(logger, f"[cartera inicial] vistas de grilla: {cuadro.evaluate(JS_VISTAS)}")

    mostrar(logger, f"Se baja el desplazamiento en {cuadro.evaluate(JS_BAJAR)} vista(s)")
    pagina.page.wait_for_timeout(2500)
    resumen_filas_cartera(formulario, logger, "tras bajar")
    mostrar(logger, f"[tras bajar] vistas de grilla: {cuadro.evaluate(JS_VISTAS)}")
    cuadro.evaluate(JS_SUBIR)
    pagina.page.wait_for_timeout(1500)

    # Recorrido por tandas: la clave de cada documento es el texto de su fila.
    total = formulario._total_filas_cuadro()
    primeras = cuadro.evaluate(JS_FILAS_IDX)[:3]
    for f in primeras:
        mostrar(logger, f"[recorrido] fila de ejemplo: atributos={f['attrs']} tabla={f['padre']} vista={f['vista']}")
        mostrar(logger, f"[recorrido]   texto={f['texto']}")
    vistos = {}
    y, paso, pasos_hechos = 0, 300, 0
    while pasos_hechos < 60:
        pos = cuadro.evaluate(JS_MOVER, y)
        pagina.page.wait_for_timeout(700)
        filas = cuadro.evaluate(JS_FILAS_IDX)
        conteo = {}
        for f in filas:
            conteo[f["texto"]] = conteo.get(f["texto"], 0) + 1
        nuevas = sum(1 for t in conteo if t not in vistos)
        for t, n in conteo.items():
            vistos[t] = max(vistos.get(t, 0), n)
        tablas = sorted({f["padre"] for f in filas})
        mostrar(
            logger,
            f"[recorrido] y={pos['arriba']} de {pos['total']} | tr dibujados {len(filas)} "
            f"| textos distintos {len(conteo)} | veces que se repite cada uno {sorted(set(conteo.values()))} "
            f"| nuevos {nuevas} | acumulados {len(vistos)} | tablas {len(tablas)}",
        )
        pasos_hechos += 1
        if (total and len(vistos) >= total) or (pos["arriba"] + pos["alto"] >= pos["total"] - 2):
            break
        y = pos["arriba"] + paso
    mostrar(logger, f"[recorrido] total esperado {total} | documentos distintos vistos: {len(vistos)}")
    mostrar(logger, f"[recorrido] máxima repetición de un mismo texto en una pantalla: {max(vistos.values()) if vistos else 0}")
    cuadro.evaluate(JS_SUBIR)


def _mostrando_cuadro(formulario):
    """(desde, hasta, total) de 'Mostrando X - Y de Z' del cuadro de cartera, o None."""
    from src.pages.comprobante_egreso_page import ComprobanteEgresoPage

    textos = formulario.c._cuadro_cartera().get_by_text(ComprobanteEgresoPage.RE_MOSTRANDO).all_inner_texts()
    for t in textos:
        m = ComprobanteEgresoPage.RE_MOSTRANDO.search(t)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def _estado_pagina(formulario, logger, etiqueta: str, anio: int) -> dict:
    datos = formulario._leer_cuadro()
    ids = {h["texto"]: h["id"] for h in datos["encabezados"]}
    id_fecha, id_abono = ids["Fecha"], ids["Abono"]
    filas = datos["filas"]
    con_abono = [f for f in filas if f.get(id_abono)]
    del_anio = [f for f in filas if f.get(id_fecha, "").endswith(str(anio)[-2:])]
    mostrar(
        logger,
        f"[{etiqueta}] Mostrando {_mostrando_cuadro(formulario)} | filas dibujadas {len(filas)} "
        f"| con Abono {len(con_abono)} | de {anio}: {len(del_anio)} "
        f"| ABONADO {formulario.leer_abonado()}",
    )
    if filas:
        mostrar(logger, f"[{etiqueta}] primera fecha {filas[0].get(id_fecha)} | última {filas[-1].get(id_fecha)}")
    return {"datos": datos, "ids": ids}


def _marcar_una(formulario, logger, etiqueta: str, anio: int):
    """Marca UN documento del año (clic en Fecha + Enter) en la página actual."""
    from src.validacion import parsear_fecha

    datos = formulario._leer_cuadro()
    ids = {h["texto"]: h["id"] for h in datos["encabezados"]}
    for f in datos["filas"]:
        if not f.get(ids["Abono"]) and parsear_fecha(f[ids["Fecha"]]).year == anio:
            formulario._marcar_fila(f["__id"], ids["Fecha"], ids["Abono"])
            mostrar(logger, f"[{etiqueta}] marcado {f['__id']} (Fecha {f[ids['Fecha']]}, Saldo {f[ids['Saldo']]})")
            return f["__id"], ids
    mostrar(logger, f"[{etiqueta}] no hay documentos de {anio} sin marcar en esta página")
    return None, ids


def _cambiar_pagina(formulario, logger, clase: str, desde_esperado: int) -> bool:
    cuadro = formulario.c._cuadro_cartera()
    boton = cuadro.locator(f"a.x-btn:has({clase})")
    mostrar(logger, f"[paginas] botones '{clase}': {boton.count()}")
    if boton.count() == 0:
        return False
    boton.first.click()
    import time

    limite = time.monotonic() + 20
    while time.monotonic() < limite:
        pag = _mostrando_cuadro(formulario)
        if pag and pag[0] == desde_esperado:
            formulario.page.wait_for_timeout(1500)
            return True
        formulario.page.wait_for_timeout(400)
    mostrar(logger, f"[paginas] no se llegó a 'desde {desde_esperado}'; último: {_mostrando_cuadro(formulario)}")
    return False


def explorar_paginas(pagina: ReciboCajaPage, nit: str, logger) -> None:
    """NIT con más de 200 documentos: ¿las marcas y el ABONADO sobreviven al cambio de página?
    Solo marca en el cuadro y al final cancela. Nunca presiona Aceptar ni Grabar."""
    formulario = FormularioComprobante(pagina)
    pagina.abrir_formulario()
    formulario.fijar_fecha(pagina.config.fecha_pago)
    formulario.cargar_cliente(nit)
    formulario.abrir_cuadro_cartera()
    anio = pagina.config.fecha_pago.year

    _estado_pagina(formulario, logger, "p1 inicial", anio)
    marcada1, ids = _marcar_una(formulario, logger, "p1", anio)
    _estado_pagina(formulario, logger, "p1 tras marcar", anio)

    pag = _mostrando_cuadro(formulario)
    if not pag or pag[1] >= pag[2]:
        mostrar(logger, "[paginas] el cuadro no tiene más páginas: usa un NIT con más de 200 documentos")
        return
    siguiente = pag[1] + 1
    if not _cambiar_pagina(formulario, logger, ".x-tbar-page-next", siguiente):
        mostrar(logger, "[paginas] no se pudo pasar a la página siguiente")
        return
    _estado_pagina(formulario, logger, "p2 al llegar", anio)
    marcada2, _ = _marcar_una(formulario, logger, "p2", anio)
    _estado_pagina(formulario, logger, "p2 tras marcar", anio)

    if not _cambiar_pagina(formulario, logger, ".x-tbar-page-prev", 1):
        mostrar(logger, "[paginas] no se pudo volver a la página 1")
        return
    estado = _estado_pagina(formulario, logger, "p1 al volver", anio)
    if marcada1:
        fila = formulario.c._cuadro_cartera().locator(f'table[id="{marcada1}"] tr.x-grid-row')
        if fila.count():
            abono = fila.locator(f"td.x-grid-cell-{ids['Abono']}").inner_text().strip()
            mostrar(logger, f"[paginas] documento marcado en p1: abono al volver = '{abono}' (vacío = se perdió)")
        else:
            mostrar(logger, "[paginas] el documento marcado en p1 ya no está dibujado (id distinto al volver)")
    mostrar(logger, "[paginas] fin: se cancela el cuadro sin aceptar")


def explorar_pagos(pagina: ReciboCajaPage, nit: str, logger, respaldo) -> None:
    formulario = FormularioComprobante(pagina)
    anio = pagina.config.fecha_pago.year

    pagina.abrir_formulario()
    formulario.fijar_fecha(pagina.config.fecha_pago)
    formulario.cargar_cliente(nit)
    mostrar_totales(pagina, logger, "cliente cargado")

    formulario.abrir_cuadro_cartera()
    marcadas, suma = formulario.marcar_documentos(anio)
    abonado = formulario.leer_abonado()
    mostrar(
        logger,
        f"Documentos de {anio} marcados: {marcadas} | suma de saldos: {suma} | "
        f"ABONADO: {abonado}",
    )
    if marcadas == 0:
        mostrar(logger, f"Sin documentos de {anio} por cobrar: usa otro NIT para explorar los pagos")
        return

    formulario.aceptar_cuadro()
    mostrar_totales(pagina, logger, "cartera aceptada")

    formulario.abrir_forma_de_pago()
    describir_ventana(logger, formulario._cuadro_pagos(), "pagos antes de elegir")
    formulario.elegir_forma_pago(FORMA_PAGO_RECIBO)
    pagina.page.wait_for_timeout(2000)
    describir_ventana(logger, formulario._cuadro_pagos(), "pagos despues de elegir")
    mostrar_totales(pagina, logger, "forma de pago elegida")

    # Aceptar SOLO la ventana de lista 'Seleccione la forma de pago' (como en egresos).
    # No se acepta 'Pagos del documento' ni se graba.
    lista = pagina.page.get_by_role("dialog").filter(has_text="Seleccione la forma de pago")
    mostrar(logger, f"Ventanas de lista abiertas: {lista.count()}")
    if lista.count():
        lista.last.locator("a.x-btn:visible", has_text=re.compile(r"^\s*Aceptar\s*$")).first.click(
            timeout=5000
        )
        pagina.page.wait_for_timeout(2500)
    mostrar(logger, f"Ventanas de lista tras aceptar: {lista.count()}")
    describir_ventana(logger, formulario._cuadro_pagos(), "pagos tras aceptar la lista")
    mostrar_totales(pagina, logger, "tras aceptar la lista")

    # Prueba: escribir el Respaldo y ver si la línea de la grilla lo toma. Sin Aceptar ni Grabar.
    if respaldo is None:
        mostrar(logger, "Sin consecutivo calculable: no se prueba el Respaldo")
        return
    escribir_respaldo(formulario, respaldo)
    describir_ventana(logger, formulario._cuadro_pagos(), "pagos tras escribir Respaldo")
    mostrar_totales(pagina, logger, "tras escribir Respaldo")

    # Sondeo 1: Enter dentro de la caja Respaldo
    cuadro = formulario._cuadro_pagos()
    ident = cuadro.evaluate(JS_ID_RESPALDO)
    pagina.page.locator(f'[id="{ident}"]').press("Enter")
    pagina.page.wait_for_timeout(1500)
    info = cuadro.evaluate(JS_DESCRIBIR)
    mostrar(logger, f"[sondeo Enter] celdas: {info['celdas']}")

    # Sondeo 2: clic sobre la línea de la grilla (selecciona la fila)
    cuadro.locator(".x-grid-cell-inner", has_text="Recaudo en Banco").first.click(timeout=5000)
    pagina.page.wait_for_timeout(1500)
    describir_ventana(logger, cuadro, "sondeo clic en la línea")

    # Sondeo 3: botones de ícono del cuadro (posiciones 4, 5 y 2 de los botones sin texto).
    # Todo ocurre dentro del cuadro: no se acepta ni se graba; al final se cancela.
    iconos = cuadro.locator("a.x-btn:visible").filter(has_not_text=re.compile(r"\S"))
    mostrar(logger, f"Botones de ícono: {iconos.count()}")
    for indice in (4, 5, 2):
        if iconos.count() <= indice:
            continue
        ident_r = cuadro.evaluate(JS_ID_RESPALDO)
        if ident_r:
            caja = pagina.page.locator(f'[id="{ident_r}"]')
            caja.click()
            caja.press("Control+A")
            caja.press_sequentially(str(respaldo), delay=60)
            caja.press("Tab")
        boton = iconos.nth(indice)
        habilitado = "disabled" not in (boton.get_attribute("class") or "")
        mostrar(logger, f"[sondeo botón {indice}] habilitado={habilitado}")
        if not habilitado:
            continue
        boton.click(timeout=5000)
        pagina.page.wait_for_timeout(2000)
        info = cuadro.evaluate(JS_DESCRIBIR)
        mostrar(logger, f"[sondeo botón {indice}] celdas: {info['celdas']}")
        mostrar(logger, f"[sondeo botón {indice}] filas: {info['filas']}")
        mostrar_totales(pagina, logger, f"sondeo botón {indice}")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Uso: python explorar_recibo.py <NIT> [formulario|pagos]")
    nit = sys.argv[1]
    modo = sys.argv[2] if len(sys.argv) > 2 else ""
    if modo not in ("", "formulario", "pagos", "cartera", "paginas"):
        sys.exit("El segundo argumento solo puede ser 'formulario', 'pagos', 'cartera' o 'paginas'")

    # Recibos de caja: se graban con la fecha del día de ejecución y se cobran los
    # documentos de ese mismo año (p. ej. 2026).
    config = replace(cargar_config(), fecha_pago=date.today())
    logger = crear_logger(config.dir_logs)

    with sync_playwright() as p:
        navegador = p.chromium.launch(
            channel=config.canal_navegador, headless=config.headless, args=["--start-maximized"]
        )
        page = navegador.new_context(no_viewport=True).new_page()
        page.screenshot = lambda *args, **kwargs: None  # sin pantallazos
        pagina = None
        try:
            LoginPage(page, config, logger).iniciar_sesion()
            pagina = ReciboCajaPage(page, config, logger)
            pagina.abrir()
            pagina.fijar_filtros()

            filas = pagina.buscar_nit(nit)
            mostrar(logger, f"Recibos del NIT {nit}: {len(filas)}")
            for f in filas[:8]:
                mostrar(
                    logger,
                    f"  Número {f[COL_NUMERO]} | Fecha {f[COL_FECHA]} | "
                    f"Valor {f[COL_VALOR]} | Anulado {f[COL_ANULADO]}",
                )
            if len(filas) > 8:
                mostrar(logger, f"  ... y {len(filas) - 8} más")
            respaldo = siguiente_consecutivo(filas)
            mostrar(logger, f"Siguiente consecutivo (Respaldo): {respaldo}")

            if modo == "formulario":
                explorar_formulario(pagina, logger)
            elif modo == "pagos":
                explorar_pagos(pagina, nit, logger, respaldo)
            elif modo == "cartera":
                explorar_cartera(pagina, nit, logger)
            elif modo == "paginas":
                explorar_paginas(pagina, nit, logger)
        finally:
            if pagina is not None:
                try:
                    pagina.cerrar_formularios_abiertos()
                except Exception:
                    logger.exception("No se pudo cerrar el formulario; ciérralo a mano")
            navegador.close()


if __name__ == "__main__":
    main()