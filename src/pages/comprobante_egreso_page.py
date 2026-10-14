import logging
import time
import re
from datetime import datetime
from playwright.sync_api import Page, TimeoutError as PWTimeout

from src.config import Config
from playwright.sync_api import Locator, Page, expect, TimeoutError as PWTimeout
from playwright.sync_api import Error as PWError, Locator, Page, expect, TimeoutError as PWTimeout


from decimal import Decimal

from src.validacion import parsear_fecha, parsear_monto

class PaginacionError(Exception):
    """La lectura paginada de la grilla no es confiable."""
    
class NavegacionError(Exception):
    """No fue posible llegar a la pantalla esperada."""


class ComprobanteEgresoPage:
    COLUMNAS_GRILLA = 9  # Número, Suc, Prefijo, NIT, Nombre, Fecha, Valor, Usuario, Anulado
    FILAS_POR_PAGINA = 20
    ESPERA_VACIO_S = 4  # segundos de grilla vacía antes de dar el NIT por "sin comprobantes"
    RE_MOSTRANDO = re.compile(r"Mostrando\s+(\d+)\s*-\s*(\d+)\s+de\s+(\d+)")
    def __init__(self, page: Page, config: Config, logger: logging.Logger):
        self.page = page
        self.config = config
        self.logger = logger

    def abrir(self) -> None:
        """Navega: Carteras CCxCP -> Principal -> Comprobante Egreso."""
        self.logger.info("Navegando a Comprobante Egreso")

        
        self._clic_menu("Carteras CCxCP")
        self._clic_menu("Principal")
        self._clic_menu("Comprobante Egreso")

        try:
            self.page.get_by_text("Generar comprobantes de egreso por pagos").wait_for(
                timeout=self.config.timeout_ms
            )
        except PWTimeout:
            ruta = self.config.dir_output / "error_navegacion.png"
            self.config.dir_output.mkdir(exist_ok=True)
            self.page.screenshot(path=str(ruta))
            raise NavegacionError(f"No cargó la pantalla de consulta. Pantallazo en {ruta}")

        self.logger.info("Pantalla de Comprobante Egreso lista")
        self.cerrar_formularios_abiertos()

    def _clic_menu(self, texto: str) -> None:
        """Espera la primera coincidencia VISIBLE del texto y le hace clic."""
        opcion = (
            self.page.get_by_text(texto, exact=True)
            .locator("visible=true")
            .first
        )
        opcion.wait_for(state="visible", timeout=self.config.timeout_ms)
        opcion.click()
        self.logger.info(f"Clic en menú: {texto}")

    def _ventana_consulta(self) -> Locator:
        """Ventana 'Comprobante Egreso [Consulta]' (hay otras ventanas detrás)."""
        return self.page.get_by_role("dialog").filter(
            has_text="Comprobante Egreso [Consulta]"
        )

    def _esperar_consulta_cargada(self, timeout_s: int = 6) -> None:
        """Espera a que la consulta muestre sus datos iniciales antes de tocar los filtros."""
        limite = time.monotonic() + timeout_s
        while time.monotonic() < limite:
            hay_filas = self._ventana_consulta().locator("tr.x-grid-row").count() > 0
            if hay_filas or self._leer_paginacion() is not None:
                self.page.wait_for_timeout(500)
                return
            self.page.wait_for_timeout(300)
        self.logger.warning("La consulta no mostró datos iniciales; se continúa")

    def fijar_filtros(self, intentos: int = 3) -> None:
        """Deja fecha en 'Sin filtro por fecha' y usuarios en 'Todos los usuarios'."""
        ventana = self._ventana_consulta()
        self.page.wait_for_timeout(2000)
        self.cerrar_formularios_abiertos()
        timeout = self.config.timeout_ms

        campo = ventana.locator('[title="Filtrar por fecha"] input')
        campo.wait_for(state="visible", timeout=timeout)
        self._esperar_consulta_cargada()

        for intento in range(1, intentos + 1):
            if campo.input_value() == "Sin filtro por fecha":
                break
            self.logger.info(
                f"Fijando filtro: Sin filtro por fecha (intento {intento}/{intentos})"
            )
            try:
                campo.click()
                opcion = self.page.locator(
                    "li.x-boundlist-item:visible", has_text="Sin filtro por fecha"
                ).first
                opcion.wait_for(state="visible", timeout=5000)
                opcion.click(timeout=5000)
                expect(campo).to_have_value("Sin filtro por fecha", timeout=5000)
            except (PWTimeout, AssertionError) as e:
                self.logger.warning(
                    f"Intento {intento} falló: {' '.join(str(e).split())[:200]}"
                )
                try:
                    ventana.get_by_text("Generar comprobantes de egreso por pagos").first.click(
                        timeout=3000
                    )
                except PWTimeout:
                    pass
                self.page.wait_for_timeout(1000)

        if campo.input_value() != "Sin filtro por fecha":
            self.config.dir_output.mkdir(exist_ok=True)
            self.page.screenshot(path=str(self.config.dir_output / "error_filtro_fecha.png"))
            raise NavegacionError(
                "No se pudo fijar 'Sin filtro por fecha'. Ver output/error_filtro_fecha.png"
            )

        campo_usuario = ventana.locator('[title="Filtrar usuario"] input')
        expect(campo_usuario).to_have_value("Todos los usuarios", timeout=timeout)
        self.logger.info("Filtros verificados: Sin filtro por fecha / Todos los usuarios")
    
    @staticmethod
    def _solo_digitos(texto: str) -> str:
        """'98,546,647' -> '98546647'."""
        return "".join(c for c in str(texto) if c.isdigit())

    def buscar_nit(self, nit: str, timeout_s: int = 10) -> list[list[str]]:
        """Busca un NIT y devuelve todas sus filas (todas las páginas).

        Devuelve [] solo si la grilla queda vacía de forma sostenida (NIT sin
        comprobantes). Si hay filas que no se pueden leer, lanza PaginacionError.
        """
        ventana = self._ventana_consulta()
        caja = ventana.get_by_placeholder("Ingrese dato a consultar...")
        nit_buscado = self._solo_digitos(nit)

        self.logger.info(f"Buscando NIT {nit_buscado}")
        caja.click()
        caja.press("Control+A")
        caja.press("Backspace")
        caja.press_sequentially(nit_buscado, delay=80)
        caja.press("Enter")

        inicio = time.monotonic()
        limite = inicio + timeout_s
        anterior = None
        filas: list[list[str]] = []
        vacio_desde = None
        estable = False
        while time.monotonic() < limite:
            if ventana.locator("tr.x-grid-row").count() == 0:
                vacio_desde = vacio_desde or time.monotonic()
                if time.monotonic() - vacio_desde >= self.ESPERA_VACIO_S:
                    self.logger.info(f"NIT {nit_buscado}: grilla vacía, sin comprobantes")
                    return []
                self.page.wait_for_timeout(400)
                continue

            vacio_desde = None
            filas = self.leer_filas()
            del_nit = bool(filas) and all(
                self._solo_digitos(f[3]) == nit_buscado for f in filas
            )
            if del_nit and filas == anterior:
                estable = True
                break
            anterior = filas
            self.page.wait_for_timeout(400)

        if not estable:
            self._diagnostico_fallo(nit_buscado)
            raise PaginacionError(
                f"La grilla no se estabilizó para {nit_buscado} en {timeout_s}s "
                f"({len(filas)} filas leídas)"
            )

        todas = self.leer_todas_las_paginas(filas)
        resultado = [f for f in todas if self._solo_digitos(f[3]) == nit_buscado]
        self.logger.info(
            f"Búsqueda lista en {time.monotonic() - inicio:.1f}s | "
            f"{len(todas)} filas leídas, {len(resultado)} del NIT"
        )
        return resultado

    def leer_filas(self) -> list[list[str]]:
        """Lee la grilla en una sola llamada al navegador.

        La última celda indica si el comprobante está anulado (SI/NO).
        """
        filas = self._ventana_consulta().locator("tr.x-grid-row")
        try:
            datos = filas.evaluate_all(
                """filas => filas.map(tr => {
                    const tds = Array.from(tr.querySelectorAll('td'));
                    const celdas = tds.map(td => td.innerText.trim());
                    const casilla = tds.length
                        ? tds[tds.length - 1].querySelector('.x-grid-checkcolumn')
                        : null;
                    if (celdas.length) {
                        celdas[celdas.length - 1] =
                            (casilla && casilla.className.includes('checked')) ? 'SI' : 'NO';
                    }
                    return celdas;
                })"""
            )
        except PWError as e:
            self.logger.warning(
                f"leer_filas: error al leer la grilla: {' '.join(str(e).split())[:300]}"
            )
            return []

        validas = [f for f in datos if len(f) == self.COLUMNAS_GRILLA]
        if len(validas) != len(datos):
            self.logger.warning(
                f"leer_filas: {len(datos) - len(validas)} de {len(datos)} filas "
                f"descartadas (celdas por fila: {sorted({len(f) for f in datos})}, "
                f"esperadas: {self.COLUMNAS_GRILLA})"
            )
        return validas


    def _barra_paginacion(self) -> Locator:
        return (
            self._ventana_consulta()
            .locator(".x-tbar-page-number")
            .first.locator("xpath=ancestor::div[contains(@class,'x-toolbar')][1]")
        )

    def _leer_paginacion(self) -> tuple[int, int, int] | None:
        """Devuelve (desde, hasta, total) de 'Mostrando X - Y de Z', o None."""
        try:
            textos = self._barra_paginacion().locator(".x-toolbar-text").all_inner_texts()
        except PWError:
            return None
        for texto in textos:
            m = self.RE_MOSTRANDO.search(texto)
            if m:
                return int(m.group(1)), int(m.group(2)), int(m.group(3))
        return None

    def _leer_estable(self, timeout_s: int = 10) -> list[list[str]]:
        """Lee la grilla hasta que dos lecturas seguidas coinciden."""
        limite = time.monotonic() + timeout_s
        anterior = None
        while time.monotonic() < limite:
            filas = self.leer_filas()
            if filas and filas == anterior:
                return filas
            anterior = filas
            self.page.wait_for_timeout(300)
        raise PaginacionError("La grilla no se estabilizó al cambiar de página")

    def _siguiente_pagina(self, desde_esperado: int, timeout_s: int = 10) -> None:
        boton = self._ventana_consulta().locator("a.x-btn:has(.x-tbar-page-next)")
        if boton.count() == 0:
            raise PaginacionError("No se encontró el botón de página siguiente")
        boton.first.click()

        limite = time.monotonic() + timeout_s
        while time.monotonic() < limite:
            pag = self._leer_paginacion()
            if pag and pag[0] == desde_esperado:
                return
            self.page.wait_for_timeout(300)
        raise PaginacionError(f"La grilla no pasó a mostrar desde el registro {desde_esperado}")

    def leer_todas_las_paginas(self, primera: list[list[str]]) -> list[list[str]]:
        """Completa la lectura con las páginas siguientes y verifica el total."""
        pag = self._leer_paginacion()
        if pag is None:
            if len(primera) >= self.FILAS_POR_PAGINA:
                raise PaginacionError("No se pudo leer el total de registros de la grilla")
            self.logger.warning(
                f"Sin texto 'Mostrando'; se asume una sola página ({len(primera)} filas)"
            )
            return primera

        _, hasta, total = pag
        todas = list(primera)
        max_paginas = total // self.FILAS_POR_PAGINA + 2
        for _ in range(max_paginas):
            if hasta >= total:
                break
            self._siguiente_pagina(hasta + 1)
            todas.extend(self._leer_estable())
            pag = self._leer_paginacion()
            if pag is None:
                raise PaginacionError("Se perdió el indicador de paginación")
            hasta = pag[1]

        claves = [(f[0], f[1], f[2]) for f in todas]
        if len(todas) != total or len(set(claves)) != len(claves):
            raise PaginacionError(
                f"Lectura inconsistente: {len(todas)} filas leídas "
                f"({len(set(claves))} únicas) y la plataforma indica {total}"
            )
        return todas
    
    def _diagnostico_fallo(self, nit: str) -> None:
        """Temporal: captura el estado de la pantalla cuando la búsqueda no se estabiliza."""
        try:
            self.config.dir_output.mkdir(exist_ok=True)
            self.page.screenshot(
                path=str(self.config.dir_output / f"fallo_busqueda_{nit}.png")
            )
            ventana = self._ventana_consulta()
            caja = ventana.get_by_placeholder("Ingrese dato a consultar...")
            self.logger.info(f"Valor en la caja de consulta: '{caja.input_value()}'")
            self.logger.info(
                f"tr.x-grid-row en la ventana: {ventana.locator('tr.x-grid-row').count()}"
            )
            self.logger.info(f"Paginación: {self._leer_paginacion()}")
            celdas = ventana.locator("tr.x-grid-row").evaluate_all(
                "filas => filas.slice(0, 3).map(tr => tr.querySelectorAll('td').length)"
            )
            self.logger.info(f"leer_filas ahora devuelve: {len(self.leer_filas())} filas")
        except PWError as e:
            self.logger.warning(f"El diagnóstico falló: {e}")


    def _formulario(self) -> Locator:
        """El formulario más reciente (el de delante si hubiera varios)."""
        return (
            self.page.get_by_role("dialog")
            .filter(has_text="Comprobante Egreso [Formulario]")
            .last
        )

    def _sin_formulario_estable(self, segundos: float = 2.5) -> bool:
        """True si no aparece ningún formulario durante 'segundos' seguidos."""
        formulario = self._formulario()
        limite = time.monotonic() + segundos
        while time.monotonic() < limite:
            if formulario.count():
                return False
            self.page.wait_for_timeout(250)
        return True

    def abrir_formulario(self) -> None:
        self.cerrar_formularios_abiertos()
        """Clic en 'Adicionar' y espera el formulario. NO graba nada."""
        self._ventana_consulta().get_by_text("Adicionar", exact=True).locator(
            "visible=true"
        ).first.click()
        self._formulario().wait_for(state="visible", timeout=self.config.timeout_ms)
        self.page.wait_for_timeout(1500)
        consecutivo = self._formulario().last.locator(
            '[title="Consecutivo del documento"] input'
        ).input_value()
        self.logger.info(f"Formulario de comprobante abierto | consecutivo={consecutivo}")

    def _caja_cliente(self) -> Locator:
        """Input que queda justo debajo de la etiqueta 'Cliente', en su misma columna."""
        ident = self._formulario().evaluate(
            """el => {
                const etiqueta = Array.from(el.querySelectorAll('*')).find(
                    e => e.children.length === 0 &&
                         e.textContent.trim() === 'Cliente' && e.offsetParent !== null);
                if (!etiqueta) return null;
                const r = etiqueta.getBoundingClientRect();
                let mejor = null, distancia = 1e9;
                for (const i of el.querySelectorAll('input')) {
                    if (i.offsetParent === null || i.type === 'hidden') continue;
                    const b = i.getBoundingClientRect();
                    const dy = b.top - r.bottom;
                    const dx = Math.abs(b.left - r.left);
                    if (dy >= -2 && dx < 60 && dy < distancia) { mejor = i; distancia = dy; }
                }
                return mejor ? mejor.id : null;
            }"""
        )
        if not ident:
            raise NavegacionError("No se encontró la caja Cliente en el formulario")
        return self.page.locator(f'[id="{ident}"]')

    def _estado_formulario(self, etiqueta: str) -> None:
        """Temporal: pantallazo, totales y filas del detalle."""
        form = self._formulario()
        self.config.dir_output.mkdir(exist_ok=True)
        self.page.screenshot(path=str(self.config.dir_output / f"formulario_{etiqueta}.png"))
        for titulo in (
            "Total pago del comprobante",
            "Total de cartera del tercero (sumas totales de CC y CP)",
            "Valor por las formas de pago",
            "Abonos realizados a documentos referencia",
        ):
            campo = form.locator(f'[title="{titulo}"] input')
            valor = campo.first.input_value() if campo.count() else "(no encontrado)"
            self.logger.info(f"[{etiqueta}] {titulo}: {valor}")
        self.logger.info(f"[{etiqueta}] filas en el detalle: {form.locator('tr.x-grid-row').count()}")

    def _diagnostico_cliente(self, nit: str) -> None:
        """Temporal: NIT en Cliente + Enter, luego 'Cargar cuentas CC-CP'. NO graba."""
        form = self._formulario()
        caja = self._caja_cliente()
        self.logger.info(f"Caja Cliente: id={caja.get_attribute('id')} caja={caja.bounding_box()}")
        self._estado_formulario("0_antes")

        caja.click()
        caja.press_sequentially(self._solo_digitos(nit), delay=80)
        caja.press("Enter")
        self.page.wait_for_timeout(4000)
        self.logger.info(f"Valor en Cliente: '{caja.input_value()}'")
        self._estado_formulario("1_despues_de_enter")

        form.get_by_text("Cargar cuentas CC-CP", exact=True).locator(
            "visible=true"
        ).first.click()
        self.page.wait_for_timeout(4000)
        self._estado_formulario("2_despues_de_cargar_cuentas")
        self._diagnostico_ventana_nueva("2_despues_de_cargar_cuentas")


    def _diagnostico_ventana_nueva(self, etiqueta: str) -> None:
        """Temporal: describe la última ventana abierta. NO hace clic en nada."""
        ventanas = self.page.get_by_role("dialog")
        self.logger.info(f"[{etiqueta}] ventanas abiertas: {ventanas.count()}")
        for i in range(ventanas.count()):
            titulo = ventanas.nth(i).evaluate(
                "el => (document.getElementById(el.getAttribute('aria-labelledby')) || {}).textContent || ''"
            )
            self.logger.info(f"[{etiqueta}] ventana {i}: {titulo.strip()!r}")

        self.page.screenshot(
            path=str(self.config.dir_output / f"ventana_nueva_{etiqueta}.png")
        )
        info = ventanas.last.evaluate(
            """el => ({
                columnas: Array.from(el.querySelectorAll('.x-column-header'))
                    .filter(c => c.offsetParent !== null)
                    .map(c => c.innerText.trim()),
                filas: el.querySelectorAll('tr.x-grid-row').length,
                campos: Array.from(el.querySelectorAll('input, textarea'))
                    .filter(i => i.offsetParent !== null)
                    .map(i => {
                        const item = i.closest('.x-form-item');
                        return {
                            tipo: i.type, valor: i.value, placeholder: i.placeholder,
                            titulo: item ? item.getAttribute('title') : null,
                            solo_lectura: i.readOnly
                        };
                    }),
                botones: Array.from(el.querySelectorAll('a.x-btn'))
                    .filter(b => b.offsetParent !== null)
                    .map(b => {
                        const tip = b.querySelector('.x-btn-tooltip');
                        return {
                            texto: b.innerText.trim(),
                            tooltip: tip ? tip.getAttribute('data-qtip') : null,
                            deshabilitado: b.classList.contains('x-btn-disabled')
                        };
                    })
            })"""
        )
        self.logger.info(f"[{etiqueta}] columnas: {info['columnas']}")
        self.logger.info(f"[{etiqueta}] filas: {info['filas']}")
        for campo in info["campos"]:
            self.logger.info(f"[{etiqueta}] campo: {campo}")
        for boton in info["botones"]:
            self.logger.info(f"[{etiqueta}] botón: {boton}")



    def _cuadro_cartera(self) -> Locator:
        return self.page.get_by_role("dialog").filter(
            has_text="Cartera asociada a tercero"
        )

    def _diagnostico_cuadro(self) -> None:
        """Temporal: describe el cuadro de cartera. SOLO LEE, no hace clic en nada."""
        cuadro = self._cuadro_cartera()
        cuadro.wait_for(state="visible", timeout=self.config.timeout_ms)
        self.page.wait_for_timeout(1500)
        self.config.dir_output.mkdir(exist_ok=True)
        self.page.screenshot(path=str(self.config.dir_output / "cuadro_cartera.png"))

        info = cuadro.evaluate(
            """el => {
                const visible = e => e.offsetParent !== null;
                const encabezados = Array.from(el.querySelectorAll('.x-column-header'))
                    .filter(h => visible(h) && !h.querySelector('.x-column-header'))
                    .map(h => ({id: h.id, texto: h.innerText.trim()}));
                const filas = Array.from(el.querySelectorAll('tr.x-grid-row')).map(tr => {
                    const celdas = {};
                    tr.querySelectorAll('td').forEach(td => {
                        const clase = Array.from(td.classList)
                            .find(c => c.startsWith('x-grid-cell-gridcolumn')) || '?';
                        celdas[clase] = td.innerText.trim();
                    });
                    return celdas;
                });
                const campos = Array.from(el.querySelectorAll('input')).filter(visible).map(i => {
                    const r = i.getBoundingClientRect();
                    return {valor: i.value, solo_lectura: i.readOnly,
                            x: Math.round(r.x), y: Math.round(r.y), ancho: Math.round(r.width)};
                });
                const botones = Array.from(el.querySelectorAll('a.x-btn'))
                    .filter(visible).map(b => b.innerText.trim());
                return {encabezados, primeras: filas.slice(0, 2), total_filas: filas.length,
                        campos, botones};
            }"""
        )
        self.logger.info(f"Cuadro encabezados: {info['encabezados']}")
        self.logger.info(f"Cuadro filas renderizadas: {info['total_filas']}")
        for fila in info["primeras"]:
            self.logger.info(f"Cuadro fila: {fila}")
        for campo in info["campos"]:
            self.logger.info(f"Cuadro campo: {campo}")
        self.logger.info(f"Cuadro botones: {info['botones']}")

    def _responder_confirmacion_cancelar(self, timeout_ms: int = 4000) -> bool:
        """Responde las preguntas de la plataforma al cancelar o cerrar un formulario.

        - '¿Desea cancelar los datos actuales?'  -> 'Si, cancelar edición del documento'
        - 'Cerrar formulario ... modo edición'   -> 'Si, cerrar el formulario'
        Devuelve True si respondió alguna.
        """
        preguntas = [
            (
                "cancelar datos actuales",
                re.compile(r"Desea cancelar los datos actuales", re.I),
                re.compile(r"cancelar edici[oó]n del documento", re.I),
            ),
            (
                "cerrar formulario en modo edición",
                re.compile(r"formulario\s+est[aá]\s+en\s+modo\s+edici[oó]n", re.I),
                re.compile(r"cerrar el formulario", re.I),
            ),
        ]
        limite = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < limite:
            for nombre, pregunta, boton in preguntas:
                if self.page.get_by_text(pregunta).locator("visible=true").count() == 0:
                    continue
                destino = self.page.get_by_text(boton).locator("visible=true").first
                try:
                    destino.click(timeout=5000)
                except PWTimeout:
                    self._captura_segura("error_confirmacion")
                    raise NavegacionError(
                        f"No se encontró el botón para responder la pregunta '{nombre}'. "
                        "Ver output/"
                    )
                self.page.wait_for_timeout(1500)
                self.logger.info(f"Pregunta respondida: {nombre}")
                return True
            self.page.wait_for_timeout(250)
        return False
    
    def cerrar_formularios_abiertos(self, max_rondas: int = 6) -> None:
        """Cierra el cuadro de cartera y los formularios 'Insertando' que haya abiertos.

        Tras cerrar un formulario puede aparecer otro, por eso se repite hasta que
        no aparezca ninguno durante unos segundos. Nunca presiona Aceptar ni Grabar.
        """
        cuadro = self.page.get_by_role("dialog").filter(has_text="Cartera asociada a tercero")
        formulario = self._formulario()

        for ronda in range(1, max_rondas + 1):
            self._cancelar_ventanas_de_pago()
            if cuadro.count():
                self.logger.warning("Cuadro de cartera abierto; se cancela")
                cuadro.get_by_text("Cancelar", exact=True).locator(
                    "visible=true"
                ).first.click(timeout=5000)
                self._responder_confirmacion_cancelar()
                self.page.wait_for_timeout(1000)
                if cuadro.count():
                    self._captura_segura("cuadro_sigue_abierto")
                    raise NavegacionError("El cuadro de cartera sigue abierto tras cancelar.")

            if self._sin_formulario_estable():
                if ronda > 1:
                    self.logger.info(f"Sin formularios abiertos tras {ronda - 1} cierre(s)")
                return

            insertando = formulario.get_by_text("Insertando", exact=True).locator(
                "visible=true"
            ).count() > 0
            con_grabar = formulario.get_by_text("Grabar", exact=True).locator(
                "visible=true"
            ).count() > 0
            if con_grabar and not insertando:
                self._captura_segura("error_formulario_en_edicion")
                raise NavegacionError(
                    "Hay un formulario con 'Grabar' que no está en estado 'Insertando' "
                    "(¿editando un documento?). Revísalo y ciérralo a mano."
                )

            self.logger.info(f"Ronda {ronda}: se cierra el formulario abierto")
            self._cerrar_ventana_formulario()

        self._captura_segura("error_cerrar_formulario")
        raise NavegacionError(
            f"Siguen apareciendo formularios tras {max_rondas} rondas. Ver output/ y el log."
        )


    def _captura_segura(self, nombre: str) -> None:
        """Guarda un pantallazo solo si PANTALLAZOS=true; nunca lanza error."""
        if not self.config.pantallazos:
            return
        try:
            self.config.dir_output.mkdir(exist_ok=True)
            ruta = self.config.dir_output / f"{nombre}_{datetime.now():%H%M%S}.png"
            self.page.screenshot(path=str(ruta))
            self.logger.info(f"Pantallazo guardado: {ruta.name}")
        except Exception as e:
            self.logger.warning(f"No se pudo guardar el pantallazo {nombre}: {e}")

    def _describir_formulario(self) -> str:
        """Resumen del formulario abierto, para el log."""
        formulario = self._formulario()
        if formulario.count() == 0:
            return "sin formulario"
        info = formulario.last.evaluate(
            """el => {
                const campo = el.querySelector('[title="Consecutivo del documento"] input');
                const herramientas = Array.from(el.querySelectorAll('.x-tool-after-title'))
                    .filter(t => t.offsetParent !== null)
                    .map(t => {
                        const interior = t.firstElementChild;
                        return (t.id + ' | ' + (t.getAttribute('data-qtip') || '-') + ' | ' +
                                (interior ? interior.className : '-')).slice(0, 120);
                    });
                return {consecutivo: campo ? campo.value : null, herramientas};
            }"""
        )
        return f"consecutivo={info['consecutivo']} | herramientas={info['herramientas']}"


    def _cerrar_ventana_formulario(self, max_clics: int = 3) -> None:
        """Cierra el formulario con la X de su cabecera, respondiendo las preguntas que salgan."""
        formulario = self._formulario()
        for n in range(1, max_clics + 1):
            boton = formulario.locator(".x-tool-after-title:has(.x-tool-close)")
            if boton.count() == 0:
                return  # ya no hay formulario
            self.logger.info(f"Cerrando formulario: clic {n}/{max_clics} en la X")
            boton.first.click(timeout=5000)

            limite = time.monotonic() + 8
            while time.monotonic() < limite:
                if formulario.count() == 0:
                    return
                if self._responder_confirmacion_cancelar(timeout_ms=500):
                    limite = time.monotonic() + 5  # tras responder, espera un poco más
                self.page.wait_for_timeout(250)
            self.logger.info(f"Cierre del formulario: el clic {n} no lo cerró; se repite")

    def _describir_ventanas(self) -> str:
        """Título y texto inicial de cada ventana (dialog) visible, para el log."""
        ventanas = self.page.get_by_role("dialog")
        partes = []
        for i in range(ventanas.count()):
            try:
                partes.append(
                    ventanas.nth(i).evaluate(
                        """el => {
                            const t = document.getElementById(el.getAttribute('aria-labelledby'));
                            return (t ? t.textContent.trim() : '?') + ' :: ' +
                                   el.innerText.replace(/\\s+/g, ' ').slice(0, 160);
                        }"""
                    )
                )
            except PWError:
                continue
        mascaras = self.page.locator(".x-mask:visible").count()
        return f"mascaras={mascaras} || " + " || ".join(partes)

    def _cancelar_ventanas_de_pago(self) -> None:
        """Cancela 'Formas de pago' y 'Pagos del documento' si quedaron abiertas."""
        for _ in range(2):
            ventana = self.page.get_by_role("dialog").filter(
                has_text=re.compile(r"Seleccione la forma de pago|Pagos del documento")
            )
            if ventana.count() == 0:
                return
            self.logger.warning("Ventana de pago abierta; se cancela")
            ventana.last.get_by_text("Cancelar", exact=True).locator(
                "visible=true"
            ).first.click(timeout=5000)
            self._responder_confirmacion_cancelar(timeout_ms=1000)
            self.page.wait_for_timeout(1500)