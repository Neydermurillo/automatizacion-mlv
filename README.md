# Automatización de Comprobantes de Egreso – DS Nube

Automatiza la carga de pagos desde un archivo Excel a la plataforma **DS Nube** (Digital SIGMA), módulo *Carteras CCxCP → Comprobante de Egreso*, usando Python + Playwright sobre Microsoft Edge.

Por cada registro del Excel (un NIT a la vez) la herramienta:

1. Valida en **Comprobante Egreso [Consulta]** si el pago ya existe (mismo NIT, fecha 31/12/2025 y mismo valor).
2. Si no existe, crea el comprobante: *Adicionar → Cliente → Cargar cuentas CC-CP → marcar documentos de 2025 → Aceptar → Forma de pago → Grabar*.
3. Verifica en la Consulta que quedó exactamente un comprobante coincidente.
4. Registra el resultado de cada fila en un Excel de salida.

> **Principio de diseño:** si algo no coincide o es dudoso, **no se ajusta ni se adivina**: la fila se marca "revisar a mano" y se continúa.

---

## Requisitos

- Python 3.11+
- Microsoft Edge instalado (se usa `channel="msedge"`, no se descarga Chromium)
- Acceso a DS Nube con un usuario con permisos sobre Comprobante de Egreso

## Instalación

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

`requirements.txt` debe incluir: `playwright`, `openpyxl`, `python-dotenv`, `pytest`.

## Configuración

Copia `.env.example` a `.env` y completa los valores:

| Variable | Descripción |
|---|---|
| `DS_USUARIO` | Usuario de DS Nube |
| `DS_CLAVE` | Clave de DS Nube |
| `DS_EMPRESA` | Nombre de la empresa a seleccionar al ingresar |
| `CARGA_REAL` | `false` = simulacro (no graba). `true` = graba comprobantes reales |
| `MAX_CARGAS` | Máximo de comprobantes que se graban por corrida |
| `PANTALLAZOS` | `false` por defecto; `true` guarda capturas para diagnóstico |

La forma de pago **se elige en la consola** al iniciar cada corrida (ver más abajo). Otros parámetros (fecha de pago 31/12/2025, columnas del Excel, timeouts) están en `src/config.py`.

> ⚠️ **Nunca subas `.env` al repositorio.** Contiene credenciales. Tampoco subas los Excel de `data/`, `output/` ni los `logs/`: contienen NITs y nombres de terceros.

## Formato del Excel de entrada

Archivo en `data/pagos.xlsx` (configurable en `src/config.py`):

| Columna | Contenido |
|---|---|
| C | NIT |
| D | Nombre |
| H | Valor final (**negativo**, sin centavos) |

La fila de encabezados es la 1; los datos inician en la fila 2.

## Uso

### 1. Simulacro (no graba nada)

Con `CARGA_REAL=false`:

```bash
python main.py 10
```

Procesa las primeras 10 filas hasta dejar el comprobante listo para grabar, sin presionar *Grabar*. Úsalo siempre antes de una corrida real.

### 2. Corrida real

En `.env`: `CARGA_REAL=true` y `MAX_CARGAS=<n>`.

```bash
python main.py 30
```

El programa:

1. Pide elegir la forma de pago: `1` = PAGO EN EFECTIVO, `3` = Transferencia Caja social.
2. Pide escribir `GRABAR` para confirmar.
3. Procesa las filas y se detiene al alcanzar `MAX_CARGAS`.

Para empezar desde una fila específica:

```bash
python main.py 30 15      # límite 30, desde la fila 15 del Excel
```

Al terminar, vuelve a poner `CARGA_REAL=false`.

## Resultados

Cada corrida genera `output/resultado_<fecha_hora>.xlsx` (se guarda tras cada fila) con:

`Fila Excel | NIT | Nombre | Valor Excel | Resultado | Motivo | Procesado`

| Resultado | Significado |
|---|---|
| `cargado` | Comprobante grabado y verificado en la Consulta |
| `omitir: ya ingresado` | Ya existe un comprobante del 31/12/2025 con el mismo valor |
| `revisar a mano: …` | Caso atípico: valor positivo, con centavos, sin documentos de 2025 por pagar, total abonado distinto al Excel, sin comprobantes, etc. |
| `error` | Fallo de lectura o de navegación en esa fila |

## Reglas de validación

- El valor del Excel debe ser **negativo** y **sin centavos**; de lo contrario → revisar a mano.
- Se marca (Enter en *Fecha*) cada documento de 2025 en el cuadro de cartera; el **ABONADO debe ser exactamente igual** al valor del Excel. Sin tolerancia ni redondeo.
- Antes de aceptar se verifica: Débitos, Total Pagos y Diferencia = 0, forma de pago y fecha 31/12/2025.
- Si el estado del pago es incierto, **se detiene toda la corrida** (nunca se reintenta *Grabar*).
- Tras grabar se verifica en la Consulta que exista exactamente un comprobante coincidente.

## Medidas de seguridad

- La grabación real requiere `CARGA_REAL=true` **y** escribir `GRABAR` en consola.
- Límite de comprobantes por corrida (`MAX_CARGAS`).
- La corrida se detiene tras 3 errores consecutivos o ante un resultado incierto.
- Operar la herramienta mientras otras personas registran manualmente los mismos NITs puede generar duplicados: coordinen antes de correr.

## Estructura del proyecto

```
.
├── main.py                       # Orquestación y consola
├── src/
│   ├── config.py                 # Configuración (.env)
│   ├── excel_io.py               # Lectura del Excel y Excel de resultados
│   ├── validacion.py             # Reglas de decisión (sin Playwright)
│   └── pages/
│       ├── login_page.py
│       ├── comprobante_egreso_page.py   # Consulta
│       └── formulario_page.py           # Formulario de comprobante
├── tests/                        # Pruebas unitarias (pytest)
├── data/                         # Excel de entrada (no versionar)
├── output/                       # Resultados (no versionar)
├── logs/                         # Logs (no versionar)
├── .env.example
└── requirements.txt
```

## Pruebas

```bash
pytest
```

Cubren las reglas de validación y la lectura del Excel (no requieren acceso a la plataforma).

## Mantenimiento

La plataforma usa ExtJS y la automatización depende de su estructura (selectores, títulos de ventanas). Si DS Nube se actualiza y algo deja de funcionar, activa `PANTALLAZOS=true`, ejecuta un simulacro con una sola fila y revisa el log para ubicar el paso que falló.

## Licencia / Propiedad

Definir según acuerdo con el cliente.
