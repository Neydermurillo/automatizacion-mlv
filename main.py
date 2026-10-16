from logging import config
import sys
from dataclasses import replace
from playwright.sync_api import sync_playwright

from src.config import Config, cargar_config
from src.excel_io import RegistroExcel, ResultadosExcel, leer_registros
from src.logger import crear_logger
from src.pages.comprobante_egreso_page import ComprobanteEgresoPage
from src.pages.formulario_page import CargaIncierta, FormularioComprobante
from src.pages.login_page import LoginPage
from src.validacion import (
    Decision,
    Resultado,
    contar_coincidencias,
    decidir,
    evaluar_cuadro,
    evaluar_valor,
)

MAX_ERRORES_SEGUIDOS = 3



OPCIONES_PAGO = {
    "1": "PAGO EN EFECTIVO",
    "3": "Transferencia Caja social",
}


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
        verif = decidir(filas, registro.valor, config.fecha_pago)
    except Exception as e:
        raise CargaIncierta(
            f"Se grabó (consecutivo propuesto {consecutivo}) pero no se pudo verificar en la "
            f"Consulta: {' '.join(str(e).split())[:200]}"
        ) from e
    if verif.decision != Decision.OMITIR_YA_INGRESADO:
        raise CargaIncierta(
            f"Se presionó Grabar (consecutivo propuesto {consecutivo}) pero la Consulta no "
            f"muestra el comprobante ({verif.decision.name})"
        )
    n = contar_coincidencias(filas, registro.valor, config.fecha_pago)
    if n != 1:
        raise CargaIncierta(
            f"Después de grabar hay {n} comprobantes del {fecha_texto} por el mismo valor "
            "(posible duplicado); revisar y anular a mano"
        )
    return Resultado(Decision.CARGADO, f"{verif.motivo}. {detalle}")


def main() -> None:
    limite = int(sys.argv[1]) if len(sys.argv) > 1 else None
    config = cargar_config()
    config = replace(config, forma_pago=elegir_forma_pago_consola())
    print(f"Forma de pago elegida: {config.forma_pago}")
    desde = int(sys.argv[2]) if len(sys.argv) > 2 else None   # <- esta línea faltaba
    logger = crear_logger(config.dir_logs)
    
    print(f"Forma de pago configurada: {config.forma_pago}")
    logger.info(f"Forma de pago configurada: {config.forma_pago}")

    if config.carga_real:
        if not limite:
            sys.exit(
                "Con CARGA_REAL=true indica cuántos registros revisar, p. ej.: python main.py 5"
            )
        print(
              f"CARGA REAL ACTIVADA: se revisarán hasta {limite} registros y se grabarán "
              f"hasta {config.max_cargas} comprobante(s) en PRODUCCIÓN, con forma de pago: "
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
            pagina_ce = ComprobanteEgresoPage(page, config, logger)
            pagina_ce.abrir()
            pagina_ce.fijar_filtros()

            errores_seguidos = 0
            cargas = 0
            for registro in registros:
                detener = False
                try:
                    resultado = procesar(registro, pagina_ce, config, logger)
                    errores_seguidos = 0
                    if resultado.decision == Decision.CARGADO:
                        cargas += 1
                except CargaIncierta as e:
                    motivo = " ".join(str(e).split())[:300]
                    resultado = Resultado(Decision.ERROR_CARGA_INCIERTA, motivo)
                    logger.error(f"Fila {registro.fila} ({registro.nit}): CARGA INCIERTA: {motivo}")
                    detener = True
                except Exception as e:
                    errores_seguidos += 1
                    motivo = " ".join(str(e).split())[:300]
                    resultado = Resultado(Decision.ERROR_LECTURA, motivo)
                    logger.error(f"Fila {registro.fila} ({registro.nit}): error: {motivo}")
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