from src.pages.comprobante_egreso_page import ComprobanteEgresoPage


class ReciboCajaPage(ComprobanteEgresoPage):
    """Consulta de 'Recibos Caja' (Carteras CCxCP -> Principal -> Recibos Caja).

    Reutiliza toda la lectura de la Consulta de ComprobanteEgresoPage (filtros, búsqueda
    por NIT, paginación, lectura de filas): la grilla tiene las mismas 9 columnas.
    El formulario de recibos tiene los mismos botones y campos clave que el de comprobantes
    (consecutivo, fecha, cliente, 'Cargar cuentas CC-CP', 'Sin forma de pago'), por eso
    también se reutiliza abrir_formulario(). Solo cambian los textos propios del módulo.
    """

    NOMBRE_MENU = "Recibos Caja"
    TEXTO_PANTALLA = "Recaudo de Cartera"
    TITULO_CONSULTA = "Recibos Caja [Consulta]"
    TITULO_FORMULARIO = "Recibos Caja [Formulario]"