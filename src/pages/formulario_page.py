import re
import time
from decimal import Decimal
from datetime import date
from playwright.sync_api import Locator, TimeoutError as PWTimeout

from src.pages.comprobante_egreso_page import ComprobanteEgresoPage, NavegacionError
from src.validacion import parsear_fecha, parsear_monto

class CarteraIncompleta(NavegacionError):
    """El cuadro de cartera no carga en pantalla todos sus documentos: se revisa a mano."""


class CargaIncierta(Exception):
    """No se puede asegurar si el comprobante quedó grabado. Detiene toda la corrida."""

class FormularioComprobante:
    """Pasos dentro del formulario 'Comprobante Egreso [Formulario]' y su cuadro de cartera.

    Reutiliza los ayudantes de ComprobanteEgresoPage. Nunca presiona Aceptar ni Grabar.
    """

    def __init__(self, consulta: ComprobanteEgresoPage):
        self.c = consulta
        self.page = consulta.page
        self.config = consulta.config
        self.logger = consulta.logger
        self.resumen_marcados = ""  # desglose de la última marcación, para explicar diferencias
        self._ids_columnas: tuple[str, str, str] | None = None

    def cargar_cliente(self, nit: str) -> None:
        """Escribe el NIT en 'Cliente', da Enter y confirma que cargó ese cliente."""
        digitos = self.c._solo_digitos(nit)
        caja = self.c._caja_cliente()
        caja.click()
        caja.press_sequentially(digitos, delay=80)
        caja.press("Enter")

        limite = time.monotonic() + 15
        while time.monotonic() < limite:
            valor = caja.input_value().strip()
            if valor and self.c._solo_digitos(valor) != digitos:
                break
            self.page.wait_for_timeout(300)
        else:
            self.c._captura_segura("error_cliente")
            raise NavegacionError(f"El cliente del NIT {digitos} no cargó")

        # El formulario muestra el NIT con comas sobre la caja (p. ej. 71,745,301)
        formateado = f"{int(digitos):,}"
        visible = (
            self.c._formulario().get_by_text(formateado, exact=True).locator("visible=true")
        )
        if visible.count() == 0:
            self.c._captura_segura("error_cliente_no_coincide")
            raise NavegacionError(
                f"No se pudo confirmar que el cliente cargado corresponde al NIT {digitos}"
            )
        self.logger.info(f"Cliente cargado para el NIT {digitos}")

    def abrir_cuadro_cartera(self) -> None:
        """Clic en 'Cargar cuentas CC-CP' y espera el cuadro de cartera."""
        self.c._formulario().get_by_text("Cargar cuentas CC-CP", exact=True).locator(
            "visible=true"
        ).first.click()
        self.c._cuadro_cartera().wait_for(state="visible", timeout=self.config.timeout_ms)
        self.page.wait_for_timeout(1500)
        self.logger.info("Cuadro de cartera abierto")

    def _leer_cuadro(self) -> dict:
        """Encabezados (con su id) y, por fila, el texto de cada celda indexado por id de columna."""
        return self.c._cuadro_cartera().evaluate(
            """el => {
                const encabezados = Array.from(el.querySelectorAll('.x-column-header'))
                    .filter(h => h.offsetParent !== null && !h.querySelector('.x-column-header'))
                    .map(h => ({id: h.id, texto: h.innerText.trim()}));
                const filas = Array.from(el.querySelectorAll('tr.x-grid-row')).map(tr => {
                    const celdas = {};
                    const tabla = tr.closest('table');
                    celdas['__id'] = tabla ? tabla.id : '';
                    tr.querySelectorAll('td').forEach(td => {
                        const clase = Array.from(td.classList)
                            .find(c => c.startsWith('x-grid-cell-gridcolumn'));
                        if (clase) celdas[clase.replace('x-grid-cell-', '')] = td.innerText.trim();
                    });
                    return celdas;
                });
                return {encabezados, filas};
            }"""
        )

    def _total_filas_cuadro(self) -> int | None:
        textos = (
            self.c._cuadro_cartera()
            .get_by_text(ComprobanteEgresoPage.RE_MOSTRANDO)
            .all_inner_texts()
        )
        for texto in textos:
            m = ComprobanteEgresoPage.RE_MOSTRANDO.search(texto)
            if m:
                return int(m.group(3))
        return None

    def _leer_mostrando(self) -> tuple[int, int, int] | None:
        """(desde, hasta, total) de 'Mostrando X - Y de Z' del cuadro de cartera, o None."""
        textos = (
            self.c._cuadro_cartera()
            .get_by_text(ComprobanteEgresoPage.RE_MOSTRANDO)
            .all_inner_texts()
        )
        for texto in textos:
            m = ComprobanteEgresoPage.RE_MOSTRANDO.search(texto)
            if m:
                return int(m.group(1)), int(m.group(2)), int(m.group(3))
        return None

    def _mover_cuadro(self, y: int | None = None) -> dict | None:
        """Mueve (o solo lee) el desplazamiento de la grilla del cuadro de cartera."""
        return self.c._cuadro_cartera().evaluate(
            """(el, y) => {
                const v = Array.from(el.querySelectorAll('.x-grid-view'))
                    .find(x => x.offsetParent !== null);
                if (!v) return null;
                if (y !== null) v.scrollTop = y;
                return {arriba: v.scrollTop, total: v.scrollHeight, alto: v.clientHeight};
            }""",
            y,
        )

    def _esperar_render_estable(self, timeout_s: float = 3.0) -> None:
        """Espera a que la grilla termine de dibujar las filas tras moverla o cambiar de página."""
        cuadro = self.c._cuadro_cartera()
        js = "el => Array.from(el.querySelectorAll('tr.x-grid-row')).map(tr => (tr.closest('table') || {}).id).join(',')"
        limite = time.monotonic() + timeout_s
        anterior = None
        self.page.wait_for_timeout(250)
        while time.monotonic() < limite:
            actual = cuadro.evaluate(js)
            if actual == anterior:
                return
            anterior = actual
            self.page.wait_for_timeout(250)

    def marcar_documentos(self, anio: int) -> tuple[int, Decimal]:
        """Da Enter en la celda 'Fecha' de cada documento del año indicado.

        El cuadro divide la cartera en páginas de hasta 200 documentos y cada página solo
        dibuja unas 49 filas a la vez (el resto aparece al desplazarse). Por eso se recorre
        página por página y, dentro de cada una, por tandas; cada documento se identifica por
        el id de su fila. Las marcas se conservan al cambiar de página (comprobado). Al final
        de cada página se exige haber visto TODOS sus documentos y al final de todas, el total
        de 'Mostrando'; si no, se lanza CarteraIncompleta (nada se acepta).

        Devuelve (filas marcadas, suma de sus saldos). NO presiona Aceptar.
        """
        est = {
            "marcadas": 0, "suma": Decimal(0), "n_pos": 0, "n_neg": 0,
            "suma_pos": Decimal(0), "suma_neg": Decimal(0), "abonos": Decimal(0),
        }
        self._ids_columnas = None
        pag = self._leer_mostrando()
        total = pag[2] if pag else None
        recorridos = 0
        for _pagina in range(1, 51):
            esperado = (pag[1] - pag[0] + 1) if pag else None
            self._mover_cuadro(0)
            self._esperar_render_estable()
            recorridos += self._marcar_pagina(anio, esperado, est)
            if pag is None or pag[1] >= pag[2]:
                break
            self._pagina_siguiente(pag[1] + 1)
            pag = self._leer_mostrando()
            if pag is None:
                raise NavegacionError("Se perdió el texto 'Mostrando' al cambiar de página")
        else:
            raise CarteraIncompleta("El cuadro tiene más de 50 páginas")

        if total is not None and recorridos != total:
            raise CarteraIncompleta(
                f"El cuadro indica {total} documentos pero solo se pudieron recorrer {recorridos}"
            )
        self.logger.info(
            f"Documentos de {anio} marcados: {est['marcadas']} de {recorridos} recorridos"
        )
        self.resumen_marcados = (
            f"marcados: {est['n_pos']} con saldo positivo (suma {est['suma_pos']}) y "
            f"{est['n_neg']} con saldo negativo (suma {est['suma_neg']}); "
            f"abonos marcados suman {est['abonos']}"
        )
        self.logger.info(f"Desglose de lo marcado: {self.resumen_marcados}")
        return est["marcadas"], est["suma"]

    def _marcar_pagina(self, anio: int, esperado: int | None, est: dict) -> int:
        """Marca los documentos del año de la página actual; devuelve cuántos documentos vio."""
        vistos: set[str] = set()
        # Tres barridos como máximo: bajando de a 300 px, subiendo de a 150 y bajando de a 100.
        # El primero basta casi siempre; los otros cubren filas que la grilla no dibujó a tiempo.
        for paso in (300, -150, 100):
            self._barrer(anio, esperado, vistos, est, paso)
            if esperado is None or len(vistos) >= esperado:
                break
        if esperado is not None and len(vistos) != esperado:
            raise CarteraIncompleta(
                f"La página indica {esperado} documentos pero solo se pudieron recorrer {len(vistos)}"
            )
        return len(vistos)

    def _barrer(
        self, anio: int, esperado: int | None, vistos: set, est: dict, paso: int
    ) -> None:
        for _ in range(200):
            self._procesar_filas_dibujadas(anio, vistos, est)
            if esperado is not None and len(vistos) >= esperado:
                return
            pos = self._mover_cuadro()
            if pos is None:
                return
            if paso > 0 and pos["arriba"] + pos["alto"] >= pos["total"] - 2:
                return
            if paso < 0 and pos["arriba"] <= 0:
                return
            self._mover_cuadro(max(pos["arriba"] + paso, 0))
            self._esperar_render_estable()

    def _procesar_filas_dibujadas(self, anio: int, vistos: set, est: dict) -> None:
        cuadro = self.c._cuadro_cartera()
        datos = self._leer_cuadro()
        if self._ids_columnas is None:
            def id_columna(nombre: str) -> str:
                hallados = [h["id"] for h in datos["encabezados"] if h["texto"] == nombre]
                if len(hallados) != 1:
                    raise NavegacionError(
                        f"Columna '{nombre}' no encontrada de forma única: "
                        f"{[h['texto'] for h in datos['encabezados']]}"
                    )
                return hallados[0]

            self._ids_columnas = (id_columna("Fecha"), id_columna("Saldo"), id_columna("Abono"))
        ids = self._ids_columnas
        id_fecha, id_saldo, id_abono = ids

        if not datos["filas"] and not vistos:
            raise NavegacionError("El cuadro de cartera no tiene filas")

        for fila in datos["filas"]:
            rid = fila.get("__id")
            if not rid:
                raise NavegacionError("Las filas del cuadro no traen un id estable")
            if rid in vistos:
                continue
            if not all(i in fila for i in ids):
                raise NavegacionError(
                    "No se pudieron asociar las celdas de la fila con sus columnas "
                    "(las celdas no traen el id de la columna)"
                )
            if fila[id_abono]:
                raise NavegacionError("El cuadro ya tenía abonos antes de empezar")
            if parsear_fecha(fila[id_fecha]).year != anio:
                vistos.add(rid)
                continue
            # Si la grilla ya no dibuja esta fila (se movió sola), se deja para otro barrido.
            if cuadro.locator(f'table[id="{rid}"] tr.x-grid-row').count() == 0:
                continue
            abono_txt = self._marcar_fila(rid, id_fecha, id_abono)
            vistos.add(rid)
            saldo = parsear_monto(fila[id_saldo])
            est["marcadas"] += 1
            est["suma"] += abs(saldo)
            est["abonos"] += parsear_monto(abono_txt)
            if saldo >= 0:
                est["n_pos"] += 1
                est["suma_pos"] += saldo
            else:
                est["n_neg"] += 1
                est["suma_neg"] += saldo

    def _pagina_siguiente(self, desde_esperado: int, timeout_s: int = 20) -> None:
        """Pasa a la página siguiente del cuadro y espera a que muestre desde ese registro."""
        boton = self.c._cuadro_cartera().locator("a.x-btn:has(.x-tbar-page-next)")
        if boton.count() == 0:
            raise NavegacionError("No se encontró el botón de página siguiente del cuadro")
        boton.first.click()
        limite = time.monotonic() + timeout_s
        while time.monotonic() < limite:
            pag = self._leer_mostrando()
            if pag and pag[0] == desde_esperado:
                self.page.wait_for_timeout(1000)
                return
            self.page.wait_for_timeout(400)
        raise NavegacionError(f"El cuadro no pasó a mostrar desde el registro {desde_esperado}")

    def _marcar_fila(self, rid: str, id_fecha: str, id_abono: str) -> str:
        """Clic en la celda Fecha de la fila (por su id) + Enter; devuelve el texto de su Abono."""
        fila = self.c._cuadro_cartera().locator(f'table[id="{rid}"] tr.x-grid-row')
        fila.locator(f"td.x-grid-cell-{id_fecha}").click(timeout=10000)
        self.page.keyboard.press("Enter")

        limite = time.monotonic() + 5
        while time.monotonic() < limite:
            abono = fila.locator(f"td.x-grid-cell-{id_abono}").inner_text().strip()
            if abono:
                return abono
            self.page.wait_for_timeout(200)
        self.c._captura_segura("error_enter_fecha")
        raise NavegacionError(f"El documento {rid} no se abonó al dar Enter en la fecha")

    def leer_abonado(self) -> Decimal:
        """Valor del total 'ABONADO' (la caja que queda bajo esa etiqueta)."""
        ident = self.c._cuadro_cartera().evaluate(
            """el => {
                const etiqueta = Array.from(el.querySelectorAll('*')).find(
                    e => e.children.length === 0 &&
                         e.textContent.trim() === 'ABONADO' && e.offsetParent !== null);
                if (!etiqueta) return null;
                const r = etiqueta.getBoundingClientRect();
                let mejor = null, distancia = 1e9;
                for (const i of el.querySelectorAll('input')) {
                    if (i.offsetParent === null || i.type === 'hidden') continue;
                    const b = i.getBoundingClientRect();
                    const dy = b.top - r.bottom;
                    if (b.right > r.left && b.left < r.right && dy >= -2 && dy < distancia) {
                        mejor = i; distancia = dy;
                    }
                }
                return mejor ? mejor.id : null;
            }"""
        )
        if not ident:
            self.c._captura_segura("error_abonado")
            raise NavegacionError("No se encontró la caja del total ABONADO")
        return parsear_monto(self.page.locator(f'[id="{ident}"]').input_value())

    def cancelar_cuadro(self) -> None:
        """Cierra el cuadro de cartera con Cancelar (descarta lo marcado)."""
        cuadro = self.c._cuadro_cartera()
        cuadro.get_by_text("Cancelar", exact=True).locator("visible=true").first.click(
            timeout=5000
        )
        self.c._responder_confirmacion_cancelar(timeout_ms=2000)
        try:
            cuadro.wait_for(state="hidden", timeout=5000)
        except PWTimeout:
            self.c._captura_segura("cuadro_sigue_abierto")
            raise NavegacionError("El cuadro de cartera sigue abierto tras Cancelar")
        self.logger.info("Cuadro de cartera cancelado")


    def aceptar_cuadro(self) -> None:
        """Presiona 'Aceptar' en el cuadro de cartera y espera a que se cierre. NO graba."""
        cuadro = self.c._cuadro_cartera()
        cuadro.get_by_text("Aceptar", exact=True).locator("visible=true").first.click(
            timeout=5000
        )
        try:
            cuadro.wait_for(state="hidden", timeout=15000)
        except PWTimeout:
            self.c._captura_segura("cuadro_sigue_tras_aceptar")
            raise NavegacionError(
                "El cuadro de cartera sigue abierto tras Aceptar. Ver output/ y el log."
            )
        self.page.wait_for_timeout(2000)
        self.logger.info("Cuadro de cartera aceptado")

    def abrir_forma_de_pago(self) -> None:
        """Clic en 'Sin forma de pago' y espera la ventana nueva. NO graba."""
        ventanas = self.page.get_by_role("dialog")
        antes = ventanas.count()
        self.c._formulario().get_by_text("Sin forma de pago", exact=True).locator(
            "visible=true"
        ).first.click(timeout=5000)

        limite = time.monotonic() + 15
        while ventanas.count() <= antes and time.monotonic() < limite:
            self.page.wait_for_timeout(300)
        if ventanas.count() <= antes:
            self.c._captura_segura("forma_pago_no_abrio")
            raise NavegacionError(
                "No se abrió una ventana nueva tras 'Sin forma de pago'. Ver output/"
            )
        self.page.wait_for_timeout(1500)
        self.logger.info(f"Ventana de forma de pago abierta (ventanas: {antes} -> {ventanas.count()})")

    def _cuadro_pagos(self) -> Locator:
        return self.page.get_by_role("dialog").filter(has_text="Pagos del documento")

    def abrir_lista_forma_pago(self) -> None:
        """Clic en la caja 'Forma de pago' del cuadro 'Pagos del documento'. NO elige nada."""
        cuadro = self._cuadro_pagos()
        ident = cuadro.evaluate(
            """el => {
                const etiqueta = Array.from(el.querySelectorAll('*')).find(
                    e => e.children.length === 0 &&
                         e.textContent.trim() === 'Forma de pago' && e.offsetParent !== null);
                if (!etiqueta) return null;
                const r = etiqueta.getBoundingClientRect();
                let mejor = null, distancia = 1e9;
                for (const i of el.querySelectorAll('input')) {
                    if (i.offsetParent === null || i.type === 'hidden') continue;
                    const b = i.getBoundingClientRect();
                    const dx = b.left - r.right;
                    const dy = Math.abs((b.top + b.height / 2) - (r.top + r.height / 2));
                    if (dx >= -4 && dy < 14 && dx < distancia) { mejor = i; distancia = dx; }
                }
                return mejor ? mejor.id : null;
            }"""
        )
        if not ident:
            self.c._captura_segura("error_caja_forma_pago")
            raise NavegacionError("No se encontró la caja 'Forma de pago' en el cuadro de pagos")

        ventanas = self.page.get_by_role("dialog")
        antes = ventanas.count()
        self.page.locator(f'[id="{ident}"]').click(timeout=5000)
        self.page.wait_for_timeout(3000)

        opciones = self.page.locator("li.x-boundlist-item:visible").all_inner_texts()
        self.logger.info(
            f"Tras clic en Forma de pago: ventanas {antes} -> {ventanas.count()} | "
            f"opciones desplegadas: {opciones}"
        )

    @staticmethod
    def _patron_forma(nombre: str) -> "re.Pattern":
        """Coincidencia exacta del nombre de la forma de pago (sin importar mayúsculas)."""
        partes = r"\s+".join(re.escape(p) for p in nombre.split())
        return re.compile(r"^\s*" + partes + r"\s*$", re.I)

    def elegir_forma_pago(self, nombre: str) -> None:
        """Abre la ventana 'Formas de pago' y selecciona la fila con ese nombre. NO acepta."""
        self.abrir_lista_forma_pago()
        lista = self.page.get_by_role("dialog").filter(has_text="Seleccione la forma de pago")
        try:
            lista.wait_for(state="visible", timeout=10000)
        except PWTimeout:
            self.c._captura_segura("error_lista_formas_pago")
            raise NavegacionError("No apareció la ventana 'Formas de pago'")

        celdas = lista.get_by_text(self._patron_forma(nombre)).locator("visible=true")
        if celdas.count() != 1:
            filas = lista.locator("tr.x-grid-row").all_inner_texts()
            self.c._captura_segura("error_forma_pago")
            raise NavegacionError(
                f"No se pudo elegir '{nombre}' de forma única. Filas de la lista: {filas}"
            )
        celdas.first.click(timeout=5000)
        self.page.wait_for_timeout(1000)
        self.logger.info(f"Fila elegida: {nombre}")

    def aceptar_ventanas_de_pago(self, forma: str, max_clics: int = 3) -> None:
        """Aceptar en 'Formas de pago' y luego en 'Pagos del documento'. NO presiona Grabar.

        Antes de aceptar 'Pagos del documento' verifica que muestre la forma de pago pedida.
        Solo toca esas dos ventanas.
        """
        ventanas_pago = re.compile(r"Seleccione la forma de pago|Pagos del documento")
        patron = re.compile(r"^\s*Aceptar\s*$")
        con_aceptar = self.page.locator("a.x-btn:visible", has_text=patron)
        patron_forma = self._patron_forma(forma)

        def pendientes() -> Locator:
            return (
                self.page.get_by_role("dialog")
                .filter(has_text=ventanas_pago)
                .filter(has=con_aceptar)
            )

        for n in range(1, max_clics + 1):
            ventanas = pendientes()
            if ventanas.count() == 0:
                self.logger.info("Ventanas de forma de pago aceptadas")
                return
            ventana = ventanas.last
            texto = " ".join(ventana.inner_text().split())
            if "Pagos del documento" in texto:
                if ventana.get_by_text(patron_forma).locator("visible=true").count() == 0:
                    self.c._captura_segura("error_forma_no_aparece")
                    raise NavegacionError(
                        f"El cuadro 'Pagos del documento' no muestra la forma de pago '{forma}'"
                    )
            self.logger.info(f"Aceptar {n}: {texto[:70]}")
            ventana.locator("a.x-btn:visible", has_text=patron).first.click(timeout=5000)
            self.page.wait_for_timeout(2500)

        if pendientes().count():
            self.c._captura_segura("quedan_ventanas_de_pago")
            raise NavegacionError(
                "Siguen abiertas las ventanas de pago tras varios clics. Ver el log."
            )
        self.logger.info("Ventanas de forma de pago aceptadas")

    def leer_totales(self) -> dict:
        """Lee Débitos, Total Pagos y Diferencia del formulario (None si no se encuentran)."""
        form = self.c._formulario()
        totales = {}
        for nombre in ("Debitos", "Total Pagos", "Diferencia"):
            texto = form.evaluate(
                r"""(el, nombre) => {
                    const norm = s => s.normalize('NFD').replace(/[\u0300-\u036f]/g, '')
                                       .trim().toLowerCase();
                    const objetivo = norm(nombre);
                    const etiqueta = Array.from(el.querySelectorAll('*')).find(
                        e => e.children.length === 0 && e.offsetParent !== null &&
                             norm(e.textContent) === objetivo);
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
                }""",
                nombre,
            )
            totales[nombre] = parsear_monto(texto) if texto is not None else None
        return totales

    def verificar_pago(self, esperado: Decimal) -> list:
        """Lista de problemas; vacía si Débitos y Total Pagos igualan al Excel y la Diferencia es 0."""
        t = self.leer_totales()
        problemas = []
        if t["Debitos"] != esperado:
            problemas.append(f"Débitos {t['Debitos']} distinto del Excel {esperado}")
        if t["Total Pagos"] != esperado:
            problemas.append(f"Total Pagos {t['Total Pagos']} distinto del Excel {esperado}")
        if t["Diferencia"] != 0:
            problemas.append(f"Diferencia {t['Diferencia']} distinta de 0")
        return problemas

    def _caja_fecha(self) -> Locator:
        """Caja 'Fecha' del encabezado del comprobante (a la derecha de esa etiqueta)."""
        ident = self.c._formulario().evaluate(
            r"""el => {
                const norm = s => s.normalize('NFD').replace(/[\u0300-\u036f]/g, '')
                                   .trim().toLowerCase();
                const etiqueta = Array.from(el.querySelectorAll('*')).find(
                    e => e.children.length === 0 && e.offsetParent !== null &&
                         norm(e.textContent) === 'fecha');
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
                return mejor ? mejor.id : null;
            }"""
        )
        if not ident:
            self.c._captura_segura("error_caja_fecha")
            raise NavegacionError("No se encontró la caja 'Fecha' del formulario")
        return self.page.locator(f'[id="{ident}"]')

    def leer_fecha(self) -> str:
        return self._caja_fecha().input_value().strip()

    def fijar_fecha(self, fecha: date) -> None:
        """Escribe la fecha del comprobante (dd/mm/aaaa) y verifica que quedó."""
        texto = fecha.strftime("%d/%m/%Y")
        caja = self._caja_fecha()
        caja.click()
        caja.press("Control+A")
        caja.press_sequentially(texto, delay=60)
        caja.press("Tab")
        self.page.wait_for_timeout(1000)

        valor = self.leer_fecha()
        if valor != texto:
            self.c._captura_segura("error_fecha_comprobante")
            raise NavegacionError(
                f"La fecha del comprobante quedó '{valor}' y se esperaba '{texto}'. Ver output/"
            )
        self.logger.info(f"Fecha del comprobante fijada: {texto}")

    def grabar(self) -> str:
        """Presiona 'Grabar' UNA sola vez y devuelve el consecutivo propuesto antes de grabar.

        No reintenta nunca. Si no se puede confirmar que el formulario salió del modo de
        inserción, lanza CargaIncierta (no se sabe si el comprobante quedó creado).
        """
        form = self.c._formulario()
        consecutivo = (
            form.locator('[title="Consecutivo del documento"] input').first.input_value().strip()
        )
        boton = form.locator("a.x-btn:visible", has_text=re.compile(r"^\s*Grabar\s*$"))
        if boton.count() != 1:
            self.c._captura_segura("error_boton_grabar")
            raise NavegacionError("No hay un único botón 'Grabar' visible; no se grabó nada")
        try:
            boton.first.click(timeout=5000)
        except PWTimeout:
            self.c._captura_segura("error_clic_grabar")
            raise NavegacionError("No se pudo presionar 'Grabar'; no se grabó nada")
        self.logger.info(f"Grabar presionado (consecutivo propuesto {consecutivo})")

        limite = time.monotonic() + 20
        while time.monotonic() < limite:
            en_insercion = form.count() and form.get_by_text("Insertando", exact=True).locator(
                "visible=true"
            ).count()
            if not en_insercion:
                self.page.wait_for_timeout(1500)
                self.logger.info("El formulario salió del modo de inserción")
                return consecutivo
            self.page.wait_for_timeout(300)

        self.c._captura_segura("grabar_sin_confirmar")
        self.logger.error(f"Ventanas tras Grabar: {self.c._describir_ventanas()}")
        raise CargaIncierta(
            f"Se presionó Grabar (consecutivo propuesto {consecutivo}) pero no se pudo "
            "confirmar que se guardó"
        )