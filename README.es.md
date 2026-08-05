# gearwatch

[![CI](https://github.com/keivanmalhani/gearwatch/actions/workflows/ci.yml/badge.svg)](https://github.com/keivanmalhani/gearwatch/actions/workflows/ci.yml)
[![Licencia: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencias de ejecucion: ninguna](https://img.shields.io/badge/runtime%20dependencies-none-brightgreen.svg)](pyproject.toml)

**Seguimiento de precios de equipo fotografico usado a partir de APIs oficiales.**

Le dices a gearwatch que estas buscando. El programa obtiene ventas completadas
comparables desde las APIs oficiales de eBay (Browse y Marketplace Insights),
construye un historial de precios por modelo y por estado, y te dice si un
anuncio activo es realmente una oportunidad frente a la distribucion de ventas
recientes de ese mismo modelo.

*Read this in [English](README.md).*

---

## Solo APIs oficiales. Sin scraping. Nunca.

Esto es una restriccion arquitectonica dura, no una preferencia:

- **Sin scraping.** gearwatch nunca descarga una pagina web de un marketplace.
- **Sin analisis de HTML.** No hay ningun parser de HTML en el codigo. El unico
  HTML que toca es el panel que el mismo escribe.
- **Sin navegador headless.** Nada de Selenium, Playwright, Puppeteer ni ningun
  tipo de automatizacion de navegador.
- **Sin endpoints no documentados.** Solo las APIs REST publicadas, versionadas
  y autenticadas que eBay documenta para desarrolladores.
- **Sin credenciales en disco.** Las credenciales vienen de variables de
  entorno, viven en memoria durante la ejecucion del proceso, y se eliminan de
  cada error, cada linea de log y cada repr.

Toda peticion pasa por un unico cliente con limitador de tasa, reintentos y
cache. Si quieres ver toda la superficie de red del programa, lee
`src/gearwatch/http.py`; no hay nada mas.

## Por que existe

El mercado de segunda mano es opaco. Los precios de venta pedidos son ruido:
cualquiera puede publicar un objetivo al precio que quiera, y un anuncio que
lleva cuatro meses sin venderse a 1.400 solo te dice que 1.400 es demasiado.
Los precios de venta reales son senal.

Una banda p25 / mediana / p75 por modelo, por estado, seguida en el tiempo,
convierte "es 850 un buen precio?" en un numero con un tamano de muestra al
lado. El autor revende equipo, asi que esto es una herramienta real, y ademas
demuestra integracion seria con una API de terceros: OAuth2, paginacion, limites
de tasa, cache y el trabajo aburrido de correccion que hace que todo eso sea
seguro.

## Cero dependencias de ejecucion

gearwatch funciona solo con la biblioteca estandar de Python 3.11:
`urllib.request`, `json`, `sqlite3`, `argparse`, `statistics`, `hashlib`,
`base64`, `time`, `re`, `dataclasses`. Nada que auditar, nada que fijar, sin
cadena de suministro. `pytest` es la unica dependencia de desarrollo.

## Inicio rapido (sin credenciales)

El repositorio incluye un fixture realista, asi que todo el flujo corre sin red:

```
git clone https://github.com/keivanmalhani/gearwatch
cd gearwatch
python -m pip install -e ".[dev]"

gearwatch --db demo.db init
gearwatch --db demo.db watch add "Sony FE 35mm f/1.4 GM" \
    --max-price 900 --condition excellent --currency USD --require gm
gearwatch --db demo.db watch add "Fujifilm XF 56mm f/1.2 R" \
    --max-price 600 --condition excellent --currency USD

gearwatch --db demo.db sync --fixture fixtures/demo.json --days 90
gearwatch --db demo.db prices
gearwatch --db demo.db deals --min-score 60
gearwatch --db demo.db dashboard -o dashboard.html
```

### Lo que realmente imprime

```
[1] Sony FE 35mm f/1.4 GM
    target: excellent, at or below 900.00 USD
    comps: 12 used of 13 fetched in this condition and currency
    outliers: 1 dropped outside the 1.5 IQR fence [812.50 USD .. 992.50 USD]: 320.00
    band:  min 860.00 USD | p25 883.75 USD | median 902.00 USD | p75 926.25 USD | max 950.00 USD
    trimmed mean (10 percent off each tail): 904.10 USD
    trend: down 45.00 USD (-4.9%) later half vs earlier half; 6 and 6 comps per half
```

```
[1] Sony FE 35mm f/1.4 GM (excellent, USD)
    band: p25 883.75 USD | median 902.00 USD | p75 926.25 USD  (12 comps (1 outlier dropped))
    [100] 849.00 USD  excellent  under p25 of recent sold, 12 comps  [under your max price, strong]
          Sony FE 35mm f/1.4 GM SEL35F14GM Lens - Excellent, Moving Sale
          item v1|405512340201|0
```

Fijate en lo que *no* aparecio: un anuncio de 399,00 con "FOR PARTS ONLY", un
Sony 35mm **f/1.8** de 875,00, una venta de 850,00 EUR y un Zeiss 35mm f/1.4
**ZA** de 780,00. Los cuatro fueron excluidos, y las cuatro exclusiones fueron
contadas y reportadas.

## La metodologia de precios, dicha con honestidad

Todo esto vive en `src/gearwatch/stats.py` y esta fijado por tests calculados a
mano.

### Percentiles

Interpolacion lineal sobre la muestra ordenada en el rango `q * (n - 1)`. Es la
misma definicion que usa numpy por defecto y coincide con `statistics.median` en
el percentil 50. Se eligio porque se puede verificar con lapiz y papel, y eso es
exactamente lo que hacen los tests.

### Valores atipicos

Las ventas fuera de `p25 - 1.5 * IQR` a `p75 + 1.5 * IQR` se retiran antes de
calcular la banda principal. Una sola copia con hongos vendida por un tercio del
precio habitual arrastra la mediana con dinero real.

**Nada se descarta en silencio.** El numero de valores retirados y los precios
exactos se imprimen en el informe, se muestran en el panel y viajan en el objeto
`PriceStat`. Las vallas son inclusivas, asi que un conjunto de precios identicos
(IQR igual a cero) conserva todos los valores en lugar de declarar atipica a
toda la muestra.

### La negativa

**gearwatch no publica una banda con menos de 5 ventas completadas.**
Este es el comportamiento mas importante de la herramienta. Por debajo del
minimo, `PriceStat.sufficient` es `False`, todos los campos de precio son
`None`, y el informe imprime:

```
    band: NOT REPORTED - insufficient data: 3 completed sales, need at least 5
```

Una mediana de tres no es un mercado, es una anecdota. Imprimirla con dos
decimales le daria una autoridad que no se ha ganado. El umbral se configura con
`--min-comps` y se aplica otra vez despues de quitar atipicos: si al quitarlos la
muestra baja del minimo, la banda se retira y la razon lo dice.

El conjunto de pruebas verifica esto en cada recuento de 0 a 4, y verifica que
con exactamente 5 comparables si se produce una banda.

### Media recortada

La media tras descartar `floor(n * 0.1)` valores en cada cola. Con menos de 10
comparables el recorte es cero y degrada a la media aritmetica, lo cual se
declara en vez de ocultarse.

### Tendencia

Las ventas se ordenan por fecha, se dividen en una mitad anterior y otra
posterior, y se reporta la diferencia de las dos medianas. Es un instrumento
tosco. Cuando alguna mitad tiene menos de 6 comparables, la salida se etiqueta
explicitamente como `weak signal`, con los recuentos de cada mitad. Indica hacia
donde se inclina el mercado; no es un pronostico.

### Puntuacion de oportunidad

Para un anuncio activo, gearwatch localiza el precio pedido dentro de la
distribucion de ventas con el metodo de rango medio:
`(por_debajo + 0,5 * iguales) / n * 100`. Un precio por debajo de todas las
ventas es percentil 0, por encima de todas es 100, y la puntuacion es
`100 - percentil`, de modo que mas alto es mejor compra.

El veredicto esta en lenguaje llano y **siempre lleva el tamano de la muestra**:

- `under p25 of recent sold, 14 comps`
- `above the median, 7 comps, thin data, treat with caution`
- `not scored: insufficient data: 3 completed sales, need at least 5`

Nunca hay un numero suelto. Si el estado del anuncio difiere del de la banda, el
veredicto lo dice. Si la moneda difiere, el anuncio no se puntua en absoluto.

### Moneda

gearwatch nunca convierte monedas. Una venta en una moneda distinta a la del
seguimiento se excluye y se cuenta con la razon `currency_mismatch`. Aplicar el
tipo de cambio de hoy a una venta de hace seis semanas seria inventar datos, y
inventar datos es lo unico que una herramienta de precios no debe hacer.

## Coincidencia de titulos

Los titulos de los marketplaces estan sucios:

```
MINT!! Sony FE 35mm F1.4 GM SEL35F14GM Lens *READ*
SONY SEL35F14GM FE 35mm F/1.4 GM E-Mount Lens L@@K
Sony FE 35 mm f/1.4 GM Lens - Near Mint from Japan
```

`src/gearwatch/match.py` normaliza todos ellos al mismo conjunto de tokens:
minusculas, puntuacion eliminada, distancias focales canonicas (`35 mm`,
`35mm`, `35MM` pasan a ser `35mm`), aperturas canonicas (`f/1.4`, `F1.4`,
`f 1.4` y el `F14` comprimido dentro de los codigos de fabricante pasan a ser
`f1.4`), codigos de fabricante expandidos (`SEL35F14GM` tambien emite `35mm` y
`f1.4`) y rangos de zoom intactos, de modo que `24-70mm` nunca se parte en `24`
y `70mm`.

La coincidencia funciona en tres etapas:

1. **Los tokens negativos son absolutos.** `for parts`, `not working`,
   `broken`, `as is`, `read`, `box only`, `hood only`, `replica` y similares
   descalifican un titulo por completo. Anade los tuyos con `--exclude`, por
   ejemplo `--exclude "body only"` cuando sigues un kit.
2. **Los tokens obligatorios deben aparecer todos.** Por defecto son la marca,
   la distancia focal y la apertura. `--require gm` promueve un token adicional,
   que es como el Zeiss 35mm f/1.4 **ZA** se queda fuera de una banda G Master.
3. **Los tokens opcionales generan la puntuacion.** 70 puntos por cumplir todos
   los requisitos y hasta 30 mas por solapamiento de tokens opcionales.

Un 35mm f/1.8 nunca puede coincidir con un seguimiento del 35mm f/1.4. Hay una
tabla de titulos sucios en `tests/test_match.py` que cubre ambas direcciones.

## Credenciales

gearwatch lee las credenciales **solo de variables de entorno**. No hay ninguna
opcion de linea de comandos, ningun archivo de configuracion y ninguna columna
de base de datos que pueda suministrarlas.

```
export EBAY_CLIENT_ID=tu-app-id
export EBAY_CLIENT_SECRET=tu-cert-id

gearwatch auth check
```

`auth check` informa de la presencia y nombra la variable que falte. Nunca
imprime, registra, hashea ni revela la longitud de un valor:

```
credentials: incomplete
  EBAY_CLIENT_ID: set
  EBAY_CLIENT_SECRET: MISSING
error: missing environment variable(s): EBAY_CLIENT_SECRET
```

### Como conseguir claves de la API de eBay

1. Crea una cuenta gratuita en el [eBay Developers
   Program](https://developer.ebay.com/).
2. Crea un conjunto de claves de aplicacion. El **App ID (Client ID)** es
   `EBAY_CLIENT_ID` y el **Cert ID (Client Secret)** es `EBAY_CLIENT_SECRET`.
3. La API Browse (anuncios activos) funciona con un keyset de produccion normal.
4. La **API Marketplace Insights** (ventas completadas, la parte que hace util
   esta herramienta) es de lanzamiento limitado. Hay que solicitar acceso a eBay
   y ser aprobado. Mientras tanto, usa gearwatch en modo fixture. Ver la seccion
   de limitaciones.

### El flujo OAuth2

gearwatch usa la concesion client-credentials para tokens de aplicacion:

- El client id y el secreto se envian como cabecera HTTP Basic a
  `https://api.ebay.com/identity/v1/oauth2/token`. Nunca aparecen en una URL, en
  una cadena de consulta ni en el cuerpo de la peticion.
- El token devuelto se guarda en memoria junto con su vencimiento absoluto.
- Se reutiliza hasta que le quedan menos de **60 segundos** de vida, y entonces
  se renueva.
- El endpoint de token se llama con la cache desactivada. Un archivo de token en
  cache seria una credencial en reposo que nadie pidio.

## Limites de tasa, reintentos y cache

- **Limitador token bucket delante de cada llamada.** Por defecto, 1 peticion
  por segundo con rafaga de 3, deliberadamente conservador. eBay concede mucho
  mas; la idea es ser buen ciudadano por defecto y dejar que el operador lo suba
  a conciencia con `--rate`.
- **Se respeta el HTTP 429.** La cabecera `Retry-After` se interpreta (tanto en
  segundos como en formato de fecha HTTP) y se respeta, con tope de 30 segundos.
  Sin `Retry-After`, el retroceso es exponencial desde 0,5 segundos con jitter
  equitativo (mitad fija, mitad aleatoria) para que los reintentos nunca se
  sincronicen en avalancha.
- **Los 5xx se reintentan, los 4xx no.** Reintentar un 404 es solo ruido.
- **Los reintentos se rinden.** Tras 4 reintentos el cliente lanza
  `RetryExhausted` con el error subyacente adjunto como `__cause__`.
- **Las respuestas se cachean en disco con un TTL de 6 horas**
  (`DEFAULT_CACHE_TTL_SECONDS = 21600`). Un objetivo usado no se mueve de forma
  significativa en un dia, asi que repetir `gearwatch sync` no cuesta nada.
  Desactivalo con `--no-cache`, cambia la ruta con `--cache-dir` o
  `$GEARWATCH_CACHE_DIR`.

La clave de cache es `sha256(metodo + url + cuerpo)`. Las cabeceras se excluyen a
proposito, lo que significa dos cosas: renovar el token no invalida la cache, y
ningun material de credencial puede acabar en un nombre de archivo. De las
cabeceras de respuesta solo se guarda `Content-Type`, asi que un `Set-Cookie`
perdido nunca llega al disco.

## Seguridad

Esto es una pieza de portafolio y la postura de seguridad pretende ser ejemplar.

| Propiedad | Como se garantiza | Donde se prueba |
| --- | --- | --- |
| Credenciales solo del entorno | `Credentials.from_env` es la unica via usada | `tests/test_auth.py` |
| Ningun secreto en un repr | Los campos de `Credentials` y `Token` son `repr=False` con `__repr__` propio | `tests/test_auth.py` |
| Ningun secreto en una excepcion | Todo mensaje de error pasa por `redact()` antes de llegar a `Exception.__init__`, asi que el secreto nunca esta en `args` | `tests/test_auth.py` |
| Ningun secreto en un log | `RedactingFilter` se instala en el logger del paquete | `tests/test_auth.py` |
| Ningun bearer token en ningun sitio | Secretos registrados mas una expresion regular que limpia cualquier `Bearer <...>` o `Basic <...>` aunque no este registrado | `tests/test_http.py` |
| Ningun secreto en la base de datos | No hay tabla de token, secreto, credencial ni auth. Un test recorre cada celda de cada tabla | `tests/test_db.py` |
| Ningun secreto en la cache | Las claves de cache excluyen cabeceras; solo se guarda `Content-Type` | `tests/test_http.py` |
| El panel no hace peticiones | Se verifica por ausencia de `http`, `<script src`, `<link`, `url(` | `tests/test_dashboard.py` |
| El conjunto de pruebas no puede usar la red | Un fixture autouse sustituye `socket.socket` por una excepcion | `tests/conftest.py` |

El test de fuga usa un valor canario distintivo, simula un servidor que devuelve
las credenciales enviadas dentro del cuerpo de error (la forma realista en que
los secretos acaban en logs e informes de fallos) y verifica que el canario no
aparece ni en `str(exc)`, ni en `repr(exc)`, ni en la traza formateada.

## El panel

`gearwatch dashboard -o dashboard.html` escribe un unico archivo autocontenido.
CSS en linea, JS en linea, sin CDN, sin fuentes remotas, sin analitica, sin
ninguna referencia externa. Tema oscuro, un solo color de acento.

Por cada seguimiento muestra la banda de precios de venta como un diagrama de
caja SVG en linea calculado a mano (bigotes hasta el minimo y el maximo, caja de
p25 a p75, linea en la mediana, puntos para los anuncios activos), el numero de
comparables, la tendencia, los atipicos retirados y los anuncios activos
ordenados por puntuacion con los buenos resaltados. Hay una marca de tiempo
"data as of" en la cabecera y un recuento de comparables junto a cada numero.

El unico compromiso deliberado: **no hay enlaces a los anuncios.** Un panel que
llama a casa no es un panel autocontenido, y cualquier URL externa romperia la
garantia de funcionamiento sin red. En su lugar se muestra el identificador del
articulo.

## Referencia de comandos

```
gearwatch [--db RUTA] [--min-comps N] COMANDO

  init                                     crea o migra la base de datos
  watch add CONSULTA [--max-price N]       anade un seguimiento
           [--condition C] [--currency C]
           [--marketplace M]
           [--require TOKEN ...]
           [--exclude TOKEN ...]
  watch list                               lista los seguimientos
  watch remove ID                          elimina un seguimiento y sus datos
  sync [--fixture RUTA] [--days N]         obtiene ventas y anuncios
       [--max-pages N] [--page-size N]
       [--watch ID] [--cache-dir RUTA]
       [--no-cache] [--rate R]
  prices [--watch ID]                      el informe de bandas de precio
  deals [--watch ID] [--min-score N]       anuncios que baten la banda
  dashboard [-o RUTA] [--watch ID]         escribe el panel HTML
  auth check                               verifica que hay credenciales
```

Estados: `new`, `like_new`, `excellent`, `good`, `fair`, `parts`.

Codigos de salida: `0` exito, `1` un fallo esperado sobre el que puedes actuar
(sin credenciales, sin seguimientos, fixture ausente), `2` error de uso.

`allow_abbrev` esta desactivado en el parser principal **y en cada subparser**,
de modo que `--max` nunca se convierte en silencio en `--max-price` en una
herramienta que gasta dinero. (argparse no propaga ese ajuste a los subparsers;
gearwatch crea todos los subparsers a traves de un ayudante que lo fija.)

Entorno: `GEARWATCH_DB` fija la base de datos por defecto y
`GEARWATCH_CACHE_DIR` el directorio de cache de respuestas por defecto.

## Desarrollo

```
python -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
```

**220 tests, 573 aserciones, cero llamadas de red.** El conjunto cubre:

| Area | Tests | Que fija |
| --- | --- | --- |
| `test_stats.py` | 32 | percentiles calculados a mano, retirada por IQR, la negativa en 0-4 y la banda en exactamente 5, media recortada, muestras sin dispersion, tendencias al alza y a la baja, percentiles de oportunidad |
| `test_match.py` | 50 | tabla de titulos sucios en ambas direcciones, variantes de apertura y focal, expansion de codigos, modelos casi identicos |
| `test_ebay.py` | 35 | normalizacion del fixture, paginacion y su tope, exclusion por moneda, ids de estado desconocidos |
| `test_http.py` | 29 | espaciado del limitador con reloj inyectado, 429 y `Retry-After`, agotamiento de reintentos, aciertos y caducidad de cache, timeouts, redaccion |
| `test_cli.py` | 22 | todo el flujo de extremo a extremo en un directorio temporal, codigos de salida, fugas en `auth check` |
| `test_db.py` | 19 | migraciones, ida y vuelta, sync idempotente, ninguna credencial en ninguna celda |
| `test_auth.py` | 18 | cache de token, el limite de renovacion de 60 segundos, redaccion en repr y excepciones |
| `test_dashboard.py` | 15 | autocontencion, el SVG se analiza como XML, geometria del diagrama de caja |

El fixture de demostracion (`fixtures/demo.json`) contiene 32 ventas completadas
de dos modelos mas 8 anuncios activos, con suciedad deliberada: un precio
atipico, una venta en otra moneda, un modelo casi identico, un id de estado no
mapeado y un anuncio "for parts". Si el flujo deja de excluir alguno de ellos,
los tests fallan.

## Limitaciones

Lee esto antes de fiarte de un numero.

- **Los datos de ventas completadas requieren aprobacion.** La API Marketplace
  Insights de eBay es de lanzamiento limitado. Sin ella puedes seguir los
  anuncios activos, pero la distribucion de ventas, que es todo el sentido de la
  herramienta, necesita que eBay apruebe tu solicitud. El modo fixture existe en
  parte por esto.
- **Los comparables son especificos de cada marketplace.** Una banda construida
  con ventas de EBAY_US describe el mercado estadounidense en eBay. No describe
  KEH, MPB, FredMiranda, tu tienda local ni eBay Alemania. No traslades una
  banda entre marketplaces.
- **Las etiquetas de estado las declara el vendedor y son ruidosas.** El
  "excellent" de un vendedor es el "good" de otro. gearwatch mapea los ids de
  estado de eBay a seis categorias e informa de cual uso, pero no puede
  inspeccionar el objetivo. Trata una banda por estado como orientativa.
- **Las muestras pequenas siguen siendo pequenas.** Para un modelo raro puede
  que nunca haya 5 ventas en 90 dias. gearwatch seguira negandose en lugar de
  adivinar, lo cual es correcto, pero implica que la herramienta es mas util con
  equipo que se mueve a menudo.
- **Los precios de venta incluyen variacion de envio y opacidad de ofertas.**
  eBay reporta el precio de venta; las mejores ofertas aceptadas y los acuerdos
  de envio pueden esconderse bajo un numero de formas que la API no expone.
- **La tendencia son dos medianas.** No es una regresion, no esta ajustada por
  estacionalidad, y lo dice cada vez que se imprime.
- **La normalizacion de aperturas es una heuristica.** Los codigos de dos
  digitos se expanden (`F14` a `f1.4`) salvo `11`, `16`, `22` y `32`, que es
  mucho mas probable que sean marcas de apertura minima reales. Numeros de parte
  inusuales pueden normalizarse de forma extrana. La regla es pequena,
  documentada y probada, pero sigue siendo una regla practica.
- **Esto no es asesoramiento financiero** y gearwatch no compra nada por ti.

## Licencia

MIT. Copyright (c) 2026 Keivan Malhani. Ver [LICENSE](LICENSE).
