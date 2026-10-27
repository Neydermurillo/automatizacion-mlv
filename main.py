import sys
from dataclasses import replace
from datetime import date

from playwright.sync_api import sync_playwright

from src.config import Config, cargar_config
from src.excel_io import RegistroExcel, ResultadosExcel, leer_registros
from src.logger import crear_logger
from src.pages.comprobante_egreso_page import ComprobanteEgresoPage
from src.pages.formulario_page import CargaIncierta, CarteraIncompleta, FormularioComprobante
from src.pages.login_page import LoginPage
from src.pages.pago_recibo import (
    FORMA_PAGO_RECIBO,
    aceptar_lista_formas_pago,
    aplicar_respaldo,
    verificar_pago_recibo,
)
from src.pages.recibo_caja_page import ReciboCajaPage
from src.validacion import (
    Decision,
    Resultado,
    TipoDocumento,
    buscar_coincidencias,
    decidir,
    describir_comprobante,
    evaluar_cuadro,
    evaluar_valor,
    siguiente_consecutivo,
)

MAX_ERRORES_SEGUIDOS = 3

OPCIONES_PAGO = {
    "1": "PAGO EN EFECTIVO",
    "3": "Transferencia Caja social",
}


OPCIONES_DOCUMENTO = {
    "1": TipoDocumento.COMPROBANTE_EGRESO,
    "2": TipoDocumento.RECIBO_CAJA,
}


def elegir_tipo_documento_consola() -> TipoDocumento:
    print("\nQué documento vas a procesar:")
    for clave, tipo in OPCIONES_DOCUMENTO.items():
        print(f"  {clave}. {tipo.value}")
    while True:
        respuesta = input("Escribe 1 o 2: ").strip()
        if respuesta in OPCIONES_DOCUMENTO:
            return OPCIONES_DOCUMENTO[respuesta]
        print("Opción no válida. Escribe 1 o 2.")


def elegir_forma_pago_consola() -> str:
    print("\nForma de pago para esta corrida:")
    for clave, nombre in OPCIONES_PAGO.items():
        print(f"  {clave}. {nombre}")
    while True:
        respuesta = input("Escribe 1 o 3: ").strip()
        if respuesta in OPCIONES_PAGO:
            return OPCIONES_PAGO[respuesta]
        print("Opción no válida. Escribe 1 o 3.")


def procesar(
    registro: RegistroExcel, pagina_ce: ComprobanteEgresoPage, config: Config, logger
) -> Resultado:
    """Valida, arma el comprobante y, solo con CARGA_REAL=true, lo graba y lo verifica."""
    previo = evaluar_valor(registro.valor)
    if previo:
        return previo

    filas = pagina_ce.buscar_nit(registro.nit)
    resultado = decidir(filas, registro.valor, config.fecha_pago)
    if resultado.decision != Decision.CARGAR:
        return resultado

    formulario = FormularioComprobante(pagina_ce)
    anio = config.fecha_pago.year
    esperado = abs(registro.valor)
    fecha_texto = config.fecha_pago.strftime("%d/%m/%Y")
    grabar_intentado = False
    consecutivo = None
    try:
        pagina_ce.abrir_formulario()
        formulario.fijar_fecha(config.fecha_pago)
        formulario.cargar_cliente(registro.nit)
        formulario.abrir_cuadro_cartera()
        marcadas, _ = formulario.marcar_documentos(anio)
        abonado = formulario.leer_abonado()

        revisar = evaluar_cuadro(marcadas, abonado, registro.valor)
        if revisar:
            formulario.cancelar_cuadro()
            if revisar.decision == Decision.REVISAR_ABONADO_DIFERENTE and formulario.resumen_marcados:
                revisar = Resultado(revisar.decision, f"{revisar.motivo} | {formulario.resumen_marcados}")
            return revisar

        formulario.aceptar_cuadro()
        formulario.abrir_forma_de_pago()
        formulario.elegir_forma_pago(config.forma_pago)
        formulario.aceptar_ventanas_de_pago(config.forma_pago)

        problemas = formulario.verificar_pago(esperado)
        fecha = formulario.leer_fecha()
        if fecha != fecha_texto:
            problemas.append(f"La fecha del comprobante quedó {fecha}")
        if problemas:
            return Resultado(Decision.REVISAR_PAGO_NO_VERIFICADO, "; ".join(problemas))

        detalle = (
            f"{marcadas} documentos de {anio}, ABONADO {abonado}, "
            f"forma de pago {config.forma_pago}, fecha {fecha_texto}, Diferencia 0"
        )
        if not config.carga_real:
            return Resultado(
                Decision.LISTO_PARA_CARGAR, f"Verificado hasta antes de Grabar: {detalle}"
            )

        grabar_intentado = True
        consecutivo = formulario.grabar()
    finally:
        try:
            pagina_ce.cerrar_formularios_abiertos()
        except Exception:
            if not grabar_intentado:
                raise
            logger.exception("No se pudo cerrar el formulario después de grabar")

    # Verificación posterior: el comprobante debe aparecer en la Consulta, y solo uno.
    try:
        filas = pagina_ce.buscar_nit(registro.nit)
        coincidentes = buscar_coincidencias(filas, registro.valor, config.fecha_pago)
    except Exception as e:
        raise CargaIncierta(
            f"Se grabó (consecutivo propuesto {consecutivo}) pero no se pudo verificar en la "
            f"Consulta: {' '.join(str(e).split())[:200]}"
        ) from e
    if not coincidentes:
        raise CargaIncierta(
            f"Se presionó Grabar (consecutivo propuesto {consecutivo}) pero la Consulta no "
            "muestra el comprobante"
        )
    if len(coincidentes) != 1:
        raise CargaIncierta(
            f"Después de grabar hay {len(coincidentes)} comprobantes del {fecha_texto} por el "
            "mismo valor (posible duplicado); revisar y anular a mano"
        )
    return Resultado(Decision.CARGADO, f"{describir_comprobante(coincidentes[0])}. {detalle}")


def procesar_recibo(
    registro: RegistroExcel, pagina: ReciboCajaPage, config: Config, logger
) -> Resultado:
    """Recibo de caja: fecha del día de ejecución, documentos de ese año y Respaldo = último
    número de la Consulta + 1. Solo con CARGA_REAL=true graba y verifica."""
    previo = evaluar_valor(registro.valor, TipoDocumento.RECIBO_CAJA)
    if previo:
        return previo

    filas = pagina.buscar_nit(registro.nit)
    # La fecha cambia cada día: se omite si ya hay un recibo del año con el mismo valor.
    resultado = decidir(filas, registro.valor, config.fecha_pago, todo_el_anio=True)
    if resultado.decision != Decision.CARGAR:
        return resultado
    respaldo = siguiente_consecutivo(filas)
    if respaldo is None:
        return Resultado(
            Decision.REVISAR_SIN_RESPALDO,
            "Hay recibos en la Consulta pero ninguno con número legible",
        )

    formulario = FormularioComprobante(pagina)
    anio = config.fecha_pago.year
    esperado = registro.valor
    fecha_texto = config.fecha_pago.strftime("%d/%m/%Y")
    grabar_intentado = False
    consecutivo = None
    try:
        pagina.abrir_formulario()
        formulario.fijar_fecha(config.fecha_pago)
        formulario.cargar_cliente(registro.nit)
        formulario.abrir_cuadro_cartera()
        marcadas, _ = formulario.marcar_documentos(anio)
        abonado = formulario.leer_abonado()
        revisar = evaluar_cuadro(marcadas, abonado, registro.valor)
        if revisar:
            formulario.cancelar_cuadro()
            if revisar.decision == Decision.REVISAR_ABONADO_DIFERENTE and formulario.resumen_marcados:
                revisar = Resultado(revisar.decision, f"{revisar.motivo} | {formulario.resumen_marcados}")
            return revisar

        formulario.aceptar_cuadro()
        formulario.abrir_forma_de_pago()
        formulario.elegir_forma_pago(FORMA_PAGO_RECIBO)
        aceptar_lista_formas_pago(formulario, FORMA_PAGO_RECIBO)
        aplicar_respaldo(formulario, respaldo, FORMA_PAGO_RECIBO)
        formulario.aceptar_ventanas_de_pago(FORMA_PAGO_RECIBO)

        problemas = verificar_pago_recibo(formulario, esperado)
        fecha = formulario.leer_fecha()
        if fecha != fecha_texto:
            problemas.append(f"La fecha del recibo quedó {fecha}")
        if problemas:
            return Resultado(Decision.REVISAR_PAGO_NO_VERIFICADO, "; ".join(problemas))

        detalle = (
            f"{marcadas} documentos de {anio}, ABONADO {abonado}, "
            f"forma de pago {FORMA_PAGO_RECIBO}, Respaldo {respaldo}, fecha {fecha_texto}, "
            "Total Pagos = Excel, Diferencia 0"
        )
        if not config.carga_real:
            return Resultado(
                Decision.LISTO_PARA_CARGAR, f"Verificado hasta antes de Grabar: {detalle}"
            )

        grabar_intentado = True
        consecutivo = formulario.grabar()
    finally:
        try:
            pagina.cerrar_formularios_abiertos()
        except Exception:
            if not grabar_intentado:
                raise
            logger.exception("No se pudo cerrar el formulario después de grabar")

    # La verificación solo LEE la Consulta: si falla por lentitud de la plataforma se repite
    # (hasta 3 intentos) limpiando antes cualquier ventana que haya quedado abierta. Nunca
    # se vuelve a grabar.
    coincidentes = None
    ultimo_error = None
    for intento in range(1, 4):
        try:
            filas = pagina.buscar_nit(registro.nit)
            coincidentes = buscar_coincidencias(
                filas, registro.valor, config.fecha_pago, todo_el_anio=True
            )
            break
        except Exception as e:
            ultimo_error = e
            logger.warning(
                f"Verificación posterior: intento {intento}/3 falló: "
                f"{' '.join(str(e).split())[:1500]}"
            )
            try:
                pagina.cerrar_formularios_abiertos()
            except Exception:
                logger.exception("No se pudo limpiar la pantalla antes de reintentar")
            pagina.page.wait_for_timeout(5000)
    if coincidentes is None:
        raise CargaIncierta(
            f"Se grabó (consecutivo propuesto {consecutivo}) pero no se pudo verificar en la "
            f"Consulta tras 3 intentos: {' '.join(str(ultimo_error).split())[:200]}"
        ) from ultimo_error
    if not coincidentes:
        raise CargaIncierta(
            f"Se presionó Grabar (consecutivo propuesto {consecutivo}) pero la Consulta no "
            "muestra el recibo"
        )
    if len(coincidentes) != 1:
        raise CargaIncierta(
            f"Después de grabar hay {len(coincidentes)} recibos de {anio} por el mismo valor "
            "(posible duplicado); revisar y anular a mano"
        )
    return Resultado(Decision.CARGADO, f"{describir_comprobante(coincidentes[0])}. {detalle}")


def main() -> None:
    limite = int(sys.argv[1]) if len(sys.argv) > 1 else None
    desde = int(sys.argv[2]) if len(sys.argv) > 2 else None
    config = cargar_config()

    if config.carga_real and not limite:
        sys.exit(
            "Con CARGA_REAL=true indica cuántos registros revisar, p. ej.: python main.py 5"
        )

    tipo = elegir_tipo_documento_consola()
    es_recibo = tipo == TipoDocumento.RECIBO_CAJA
    if es_recibo:
        # Recibos: fecha del día de ejecución y forma de pago fija.
        config = replace(config, fecha_pago=date.today(), forma_pago=FORMA_PAGO_RECIBO)
        logger = crear_logger(config.dir_logs)
    else:
        config = replace(config, forma_pago=elegir_forma_pago_consola())
        logger = crear_logger(config.dir_logs)
    texto = (
        f"Documento: {tipo.value} | fecha {config.fecha_pago:%d/%m/%Y} | "
        f"forma de pago: {config.forma_pago}"
    )
    print(texto)
    logger.info(texto)

    if config.carga_real:
        print(
            f"CARGA REAL ACTIVADA: se revisarán hasta {limite} registros y se grabarán "
            f"hasta {config.max_cargas} documento(s) en PRODUCCIÓN ({tipo.value}), con forma de pago: "
            f"{config.forma_pago}."
        )
        if input("Escribe GRABAR para continuar: ").strip() != "GRABAR":
            sys.exit("Cancelado.")
        logger.warning(f"CARGA REAL activada (máximo {config.max_cargas} comprobante(s))")

    registros, invalidas = leer_registros(config)
    if desde:
        registros = [r for r in registros if r.fila >= desde]
        invalidas = [i for i in invalidas if i.fila >= desde]
    if limite:
        registros = registros[:limite]

    logger.info(
        f"Excel leído: {len(registros)} registros a procesar, {len(invalidas)} filas inválidas"
    )
    if registros:
        logger.info(
            f"Primer registro leído: fila {registros[0].fila}, NIT {registros[0].nit} | "
            f"último: fila {registros[-1].fila}, NIT {registros[-1].nit}"
        )

    resultados = ResultadosExcel(config.dir_output)
    for invalida in invalidas:
        resultados.agregar_invalida(invalida)

    with sync_playwright() as p:
        navegador = p.chromium.launch(
            channel=config.canal_navegador, headless=config.headless, args=["--start-maximized"]
        )
        contexto = navegador.new_context(no_viewport=True)
        page = contexto.new_page()
        if not config.pantallazos:
            page.screenshot = lambda *args, **kwargs: None  # sin pantallazos
        pagina_ce = None
        try:
            LoginPage(page, config, logger).iniciar_sesion()
            clase_pagina = ReciboCajaPage if es_recibo else ComprobanteEgresoPage
            pagina_ce = clase_pagina(page, config, logger)
            pagina_ce.abrir()
            pagina_ce.fijar_filtros()

            errores_seguidos = 0
            cargas = 0
            for registro in registros:
                detener = False
                try:
                    tramitar = procesar_recibo if es_recibo else procesar
                    resultado = tramitar(registro, pagina_ce, config, logger)
                    errores_seguidos = 0
                    if resultado.decision == Decision.CARGADO:
                        cargas += 1
                except CargaIncierta as e:
                    motivo = " ".join(str(e).split())[:300]
                    resultado = Resultado(Decision.ERROR_CARGA_INCIERTA, motivo)
                    logger.error(f"Fila {registro.fila} ({registro.nit}): CARGA INCIERTA: {motivo}")
                    detener = True
                except CarteraIncompleta as e:
                    # No es una falla del robot: ese NIT se hace a mano. No cuenta como error.
                    errores_seguidos = 0
                    motivo = " ".join(str(e).split())[:300]
                    resultado = Resultado(Decision.REVISAR_CARTERA_INCOMPLETA, motivo)
                    logger.warning(f"Fila {registro.fila} ({registro.nit}): {motivo}")
                    try:
                        pagina_ce.cerrar_formularios_abiertos()
                    except Exception:
                        logger.exception("No se pudo limpiar la pantalla tras la cartera incompleta")
                except Exception as e:
                    errores_seguidos += 1
                    completo = " ".join(str(e).split())
                    motivo = completo[:300]
                    resultado = Resultado(Decision.ERROR_LECTURA, motivo)
                    # En el log queda el mensaje completo (el Excel conserva solo 300 caracteres).
                    logger.error(
                        f"Fila {registro.fila} ({registro.nit}): error: {completo[:2500]}"
                    )
                    try:
                        pagina_ce.cerrar_formularios_abiertos()
                    except Exception:
                        logger.exception("No se pudo limpiar la pantalla tras el error")

                resultados.agregar(registro, resultado.decision, resultado.motivo)
                logger.info(f"Fila {registro.fila} ({registro.nit}): {resultado.decision.name}")

                if detener:
                    logger.error("Se detiene la corrida: revisa a mano el último registro")
                    break
                if errores_seguidos >= MAX_ERRORES_SEGUIDOS:
                    logger.error("Demasiados errores seguidos; se detiene el proceso")
                    break
                if config.carga_real and cargas >= config.max_cargas:
                    logger.info(
                        f"Se alcanzó el máximo de {config.max_cargas} comprobante(s) grabado(s)"
                    )
                    break

            logger.info(f"Resultados guardados en {resultados.ruta}")
        except Exception:
            logger.exception("El proceso terminó con error")
        finally:
            if pagina_ce is not None:
                try:
                    pagina_ce.cerrar_formularios_abiertos()
                except Exception:
                    logger.exception("No se pudo cerrar el formulario abierto; ciérralo a mano")
            navegador.close()


if __name__ == "__main__":
    main()