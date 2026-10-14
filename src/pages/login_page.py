import logging
import time

from playwright.sync_api import Error as PWError, Locator, Page

from src.config import Config


class LoginError(Exception):
    """No fue posible iniciar sesión en la plataforma."""


class LoginPage:
    def __init__(self, page: Page, config: Config, logger: logging.Logger):
        self.page = page
        self.config = config
        self.logger = logger

    def iniciar_sesion(self) -> None:
        self.logger.info("Abriendo página de login")
        self.page.goto(self.config.url_login)

        self.page.get_by_placeholder("Escriba su correo").fill(self.config.usuario)
        self.page.get_by_placeholder("Escriba la contraseña").fill(self.config.clave)
        self.page.get_by_text("Iniciar sesión", exact=True).click()

        self._esperar_ingreso()
        self.logger.info("Login exitoso")

    @staticmethod
    def _visible(localizador: Locator) -> bool:
        """True si el elemento está visible; False si no existe o la página navega."""
        try:
            return localizador.first.is_visible()
        except PWError:
            return False

    def _esperar_ingreso(self) -> None:
        """Atiende las pantallas intermedias hasta entrar al sistema.

        Pantallas posibles, en cualquier orden:
        - Aviso de múltiples intentos de ingreso -> responde 'No'.
        - Lista de empresas -> hace clic en la empresa configurada.
        """
        sistema = self.page.get_by_text("Opciones del sistema")
        aviso = self.page.get_by_text("varios intentos de ingreso")
        empresa = self.page.get_by_text(self.config.empresa, exact=True)
        empresa_elegida = False

        limite = time.monotonic() + self.config.timeout_ms / 1000
        while time.monotonic() < limite:
            if self._visible(sistema):
                return

            if self._visible(aviso):
                self.logger.info("Aviso de múltiples intentos detectado, se responde 'No'")
                self.page.get_by_role("button", name="No", exact=True).click()
            elif not empresa_elegida and self._visible(empresa):
                self.logger.info(f"Seleccionando empresa: {self.config.empresa}")
                empresa.first.click()
                empresa_elegida = True

            self.page.wait_for_timeout(300)

        self.config.dir_output.mkdir(exist_ok=True)
        ruta = self.config.dir_output / "error_login.png"
        self.page.screenshot(path=str(ruta))
        raise LoginError(f"No se confirmó el login. Pantallazo en {ruta}")