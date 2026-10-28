# StockMind

Asistente de inventario con IA que se conecta a cualquier ERP mediante
**MCP (Model Context Protocol)**. Proyecto de grado (UAGRM).

La IA (Claude Desktop, o el chat web que vendrá después) consulta productos,
stock, kardex y ventas, y puede crear productos o registrar movimientos, pero
**nunca escribe sin una confirmación explícita del usuario**.

```
IA ──MCP──► mcp_server ──► application (validación · confirmación · auditoría) ──► puerto ──► adaptador ──► ERP
                                                                                 InventarioConnector   CSV/Excel (lectura y escritura)
                                                                                                        Conector para un ERP real (privado)
```

## Requisitos

- Windows 10/11, Python **3.14** (funciona desde 3.11)
- Node.js, solo para MCP Inspector
- Claude Desktop, para usarlo con IA

> La carpeta del proyecto tiene espacios (`D:\UAGRM\Taller de Grado`):
> pon siempre las rutas entre comillas.

## Instalación (PowerShell)

```powershell
cd "D:\UAGRM\Taller de Grado"
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1          # si lo bloquea: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
pip install -r requirements.txt       # instala las dependencias y el paquete en modo editable
Copy-Item .env.example .env
```

## Generar datos de prueba

```powershell
python scripts\generar_datos.py                  # CSV en data\
python scripts\generar_datos.py --formato xlsx   # Excel (pon FORMATO_DATOS=xlsx en .env)
python scripts\generar_datos.py --forzar         # sobrescribe los datos existentes
```

Genera 40 productos de ferretería y 12 meses de ventas diarias, del 25/10/2025
al 24/10/2026, con semilla fija (42). La misma semilla produce los mismos archivos.

- **Tendencia:** +8 % anual.
- **Semana:** el sábado es el día fuerte y el domingo abre medio día.
- **Temporadas de Santa Cruz:** época seca para la obra gruesa, lluvias para
  los impermeabilizantes y diciembre para la pintura.
- **Feriados:** los nacionales y los de Santa Cruz.
- **Ruido:** binomial negativa, más pedidos grandes ocasionales de constructoras.

Los movimientos son coherentes con las ventas:

- un saldo inicial;
- una salida por cada línea de venta;
- compras de reposición con punto de pedido y demora de 3 a 7 días;
- mermas como ajustes.

El saldo nunca es negativo. Algunos productos terminan bajo el mínimo porque
el proveedor se demoró.

**Son datos sintéticos.** Así hay que declararlo en el documento de grado.

## Tests

```powershell
python -m pytest                      # todo; los de integración se saltan si no hay un ERP configurado
python -m pytest -m "not integracion"
```

Cubren:

- el conector CSV/Excel en ambos formatos;
- la validación y el flujo de confirmación (memoria y SQLite);
- las tools MCP de punta a punta;
- la coherencia del generador;
- la carga de conectores opcionales;
- la bandeja de salida con conectores falsos (estados, stock comprometido, exportar, verificar);
- los reportes, con un CSV armado a mano cuyos resultados se calculan en papel (ABC, cobertura,
  rotación) y con los datos sintéticos;
- el agente y la API con un LLM falso, sin llamadas reales; entre ellos, que **el modelo no puede
  confirmar operaciones**, el transporte MCP por HTTP con token y los adaptadores de Anthropic y Gemini.

Los tests públicos no necesitan ningún conector privado.

## Conectores opcionales (ERP real, privado)

StockMind se probó también con un **conector para un ERP real (privado)**, de
**solo lectura**. Ese conector no se publica en este repositorio.

**Cómo se agrega uno:**

1. Crea un subpaquete `src/stockmind/adapters/<nombre>/` que implemente
   `InventarioConnector` y exponga `crear_conector(settings)`.
2. Su configuración la lee el propio conector, desde el mismo `.env` y con
   su propio prefijo de variables.
3. Pon `CONNECTOR=<nombre>` en `.env`.
4. Si el conector usa MySQL, instala los extras: `pip install -e ".[mysql]"`.

Si el módulo no está instalado, StockMind lo dice con un mensaje claro y no
arranca con ese conector.

**Modo de escritura** (`modo_escritura` del conector):

| Modo | Al confirmar |
|---|---|
| `directa` | Se escribe en el ERP (p. ej. CSV/Excel). |
| `bandeja` | **No** se escribe en el ERP: la operación queda **pendiente** en la bandeja de salida local, para registrarla después en el ERP. |
| `ninguna` | Se rechaza con "Escritura no habilitada", antes de validar o emitir un token. |

**Reglas de un conector de solo lectura:** contra la base de datos del ERP
solo se ejecutan consultas.

- Las barreras son tres: un usuario con solo SELECT, la sesión en modo
  `READ ONLY` y una guardia en el código que rechaza cualquier sentencia que no
  sea una consulta.
- Todo el SQL va parametrizado.

## Reportes de inventario

Se calculan solo con los métodos del puerto, así que funcionan con cualquier
conector.

- **Cómo se justifican:** cada reporte trae `metodo` (cómo se calculó, con los
  números usados) y `avisos`.
- **Sin datos suficientes** (p. ej. sin ventas en el período), el valor queda en
  `null`, `sin_datos` en `true` y el aviso lo explica: **nunca un cero
  engañoso**.

| Tool | Qué calcula | Período por defecto |
|---|---|---|
| `reporte_mas_vendidos` | Top N por unidades y por monto, con % de participación | 30 días |
| `reporte_bajo_stock` | Stock < mínimo con **días de cobertura** (stock / venta diaria) y **sugerido a reponer** = máx(0, ⌈máx(mínimo, venta diaria × días objetivo) − stock⌉) | ventas de 30 días |
| `reporte_sin_movimiento` | Sin ventas ni salidas en N días, con **capital inmovilizado** (stock × costo) y última salida | 90 días |
| `reporte_abc` | Clasificación ABC por monto (A ≈ 80 %, B ≈ 15 %, C ≈ 5 %, configurables), con % acumulado | 90 días |
| `reporte_rotacion` | **Rotación** = costo de lo vendido / inventario promedio, días de inventario, otras salidas aparte, y **valorización** al costo y a precio de venta, por categoría | 90 días |
| `resumen_ejecutivo` | Lo más importante de todos, en pocas líneas (`hallazgos`) | 30 días |

- **Parámetros:** `desde`/`hasta` (AAAA-MM-DD) **o** `ultimos_dias`; `categoria`
  y `almacen` donde aplica.
- **Exportar:** `exportar=true` guarda un `.xlsx` en `exports/`, con una hoja
  "Cómo se calculó".
- **Monedas:** los totales salen por moneda (`Producto.moneda`, o `MONEDA_BASE`
  si no la informa).

## Bandeja de salida (ERP de solo lectura)

Con un conector en modo `bandeja`, el flujo de vista previa, token y
confirmación es el mismo. La diferencia está en lo que pasa al confirmar:

1. Se revalida con las reglas del ERP: campos, valores válidos, nombre único,
   almacén y lote, y que una salida no deje el stock negativo.
2. La operación se guarda **pendiente** en `data/stockmind.db` (tabla
   `bandeja_operaciones`), con una referencia (`SM-000012`) y una **guía paso
   a paso** para registrarla en el ERP.
3. La respuesta lo dice con claridad: **"NO se registró todavía en el ERP: quedó
   PENDIENTE"**.

**Estados:** `pendiente` → `registrada` o `descartada`.

- El cambio es único, atómico y deja fecha y observación.
- Descartar exige un motivo.
- Cada cambio queda en la auditoría (`encolada`, `exportada`, `registrada`,
  `descartada`).

**Stock comprometido:** las salidas pendientes se descuentan al validar nuevas
salidas, aunque el ERP todavía no las conozca.

**Tools:**

| Tool | Qué hace |
|---|---|
| `listar_pendientes(estado, tipo)` | Lista la bandeja: `pendiente` / `registrada` / `descartada` / `todas`; `productos` / `movimientos`. Las pendientes traen su guía |
| `exportar_pendientes(tipo)` | Genera en `exports/` los archivos para registrar en el ERP. No cambia el estado |
| `verificar_pendientes()` | Busca en el ERP (solo lectura). Lo encontrado de forma exacta se marca como registrado; las posibles coincidencias solo se informan |
| `marcar_registrada(id, observacion)` | Marca a mano una pendiente como registrada |
| `descartar_pendiente(id, motivo)` | Descarta una pendiente; no se registrará |

**Integración con el ERP:** un conector en modo bandeja puede implementar
`ports/bandeja.py::IntegracionBandeja` (`guia`, `exportar`, `verificar`)
para usar el formato de importación del ERP y conciliar. Si no la implementa,
se usa una integración genérica: un Excel con los datos, una guía genérica y
sin conciliación automática.

## Chat con IA (backend FastAPI)

El chat es un **agente cliente MCP**: se conecta al servidor MCP de StockMind por
**streamable HTTP** y usa sus tools, igual que Claude Desktop. **No llama directo al
código de la aplicación**, así que demuestra el estándar MCP de punta a punta. El
modelo de IA va detrás de un puerto (`LLMProvider`).

```
React / módulo del ERP ──HTTP──► stockmind-api (FastAPI)  ── historial, uso de tokens, auth
                                    │
                                    ├─ LLMProvider ── Anthropic | Gemini | falso (tests)
                                    └─ Cliente MCP ──streamable HTTP + MCP_TOKEN──► stockmind-mcp --http
```

### Cómo levantarlo

1. Instala los extras: `.\.venv\Scripts\pip install -e ".[api]"`.
2. En `.env` completa:
   - `MCP_TOKEN` y `API_TOKEN`: textos largos y secretos, por ejemplo
     `python -c "import secrets; print(secrets.token_urlsafe(32))"`;
   - `LLM_PROVIDER`, `LLM_API_KEY` y, si quieres, `LLM_MODEL`.
3. Con un solo comando levantas los dos procesos, y con Ctrl+C los detienes:

   ```powershell
   .\scripts\iniciar.ps1
   ```

   También puedes levantarlos a mano en dos terminales:
   `.\.venv\Scripts\stockmind-mcp.exe --http` y `.\.venv\Scripts\stockmind-api.exe`.
4. La documentación interactiva queda en `http://127.0.0.1:8000/docs`.

**Proveedores:**

| `LLM_PROVIDER` | Modelo por defecto | Nota |
|---|---|---|
| `anthropic` | `claude-sonnet-5-5` | Thinking según `LLM_THINKING`, caché de prompt y fallback del lado del servidor (`LLM_FALLBACKS`) |
| `gemini` | `gemini-3.8-flash` | Está en el nivel gratuito de la API de Gemini; sirve para desarrollar |
| `falso` | — | Responde por guion; para tests y demos sin costo |

**Saturación y límites de uso (circuit breaker):** si un modelo está saturado (503 en Gemini,
529/overloaded en Anthropic, o no responde en `LLM_TIMEOUT_SEGUNDOS`), queda en enfriamiento
`LLM_ENFRIAMIENTO_SEGUNDOS` (300 s) y el chat usa directamente el siguiente de
`LLM_MODELOS_RESPALDO`, sin reintentar. Con Gemini, por defecto son `gemini-3.6-flash`,
`gemini-3.5-flash-lite` y `gemini-3.1-flash-lite`: estables, en el nivel gratuito y con function
calling. Otros errores temporales (429) se reintentan una vez si hay respaldo, o con espera
exponencial (2 s, 4 s, 8 s) si no lo hay. El evento `reintento` avisa cada cambio, y la consola
muestra qué modelo respondió. No se reintenta si ya se mostró parte de la respuesta.

**Rendimiento:** thinking `bajo` por defecto (`LLM_THINKING`); al modelo se le envían esquemas de
tools compactos, solo los últimos `HISTORIAL_TURNOS` turnos (los anteriores, resumidos) y las
primeras `MAX_FILAS_RESULTADO` filas de cada lista (el evento `tool_fin` trae el resultado completo
para el panel). Cuando una tool deja una vista previa, el turno termina ahí: el usuario confirma
enseguida, sin esperar otra llamada al modelo. La API reutiliza una sola conexión MCP.
`scripts/chat_consola.py --tiempos` muestra cuánto tarda cada etapa.

### Human in the loop (obligatorio)

- **Al modelo nunca le llegan** `confirmar_operacion`, `cancelar_operacion`,
  `marcar_registrada` ni `descartar_pendiente`. Si las pide por nombre, el agente las
  rechaza sin ejecutarlas.
- **Vista previa:** cuando una tool devuelve una vista previa con token, la API emite
  un evento `accion_pendiente` (resumen, vista previa, advertencias, vencimiento y
  token) para que el frontend muestre los botones. **El modelo recibe el resultado
  sin el token.**
- **Confirmar o cancelar** lo hace el usuario con `POST /api/v1/acciones/confirmar`
  o `/cancelar`, enviando `{conversacion_id, token}` en el **cuerpo**, nunca en la
  URL. El resultado se agrega al historial como nota del sistema.
- **Bandeja:** `marcar_registrada` y `descartar_pendiente` son botones del panel:
  `POST /api/v1/bandeja/{ref}/registrar` y `/descartar`.

### Endpoints (`/api/v1`, todos con `Authorization: Bearer <token>`)

| Método y ruta | Qué hace |
|---|---|
| `POST /chat` | `{mensaje, conversacion_id?}` → **SSE** |
| `POST /acciones/confirmar` · `POST /acciones/cancelar` | `{conversacion_id, token}`; lo ejecuta el usuario |
| `GET /conversaciones` · `GET/DELETE /conversaciones/{id}` | Historial, acciones y uso de tokens por conversación |
| `GET /reportes/{tipo}` | `mas_vendidos`, `bajo_stock`, `sin_movimiento`, `abc`, `rotacion`, `resumen`, con los parámetros de la tool en la query |
| `GET /bandeja` · `POST /bandeja/{ref}/registrar` · `POST /bandeja/{ref}/descartar` | Bandeja de salida |
| `GET /salud` | Estado del servidor MCP, del conector y del proveedor (503 si el MCP no responde) |

**Eventos del SSE de `/chat`:**

| Evento | Datos |
|---|---|
| `conversacion` | `{id, titulo}` (llega primero) |
| `texto` | `{texto}`: un fragmento de la respuesta |
| `reintento` | `{modelo, intento, espera, motivo, cambio_de_modelo}`: el proveedor está saturado y se reintenta |
| `tool_inicio` | `{id, nombre, argumentos}` |
| `tool_fin` | `{id, nombre, ok, resumen}` |
| `accion_pendiente` | `{id, conversacion_id, operacion, resumen, modo_escritura, vista_previa, advertencias, vence, token}` |
| `uso` | `{proveedor, modelo, entrada, salida, cache_lectura, cache_escritura}`, una vez por llamada al modelo |
| `error` | `{error, mensaje}` |
| `fin` | `{conversacion_id, acciones_pendientes, uso}` (llega último) |

Los errores REST tienen siempre la forma `{"ok": false, "error", "mensaje", ...}`.

Ejemplo con `curl`:

```powershell
curl.exe -N -X POST http://127.0.0.1:8000/api/v1/chat -H "Authorization: Bearer $env:API_TOKEN" `
  -H "Content-Type: application/json" -d '{\"mensaje\": \"¿Qué productos están bajo stock?\"}'
```

### Seguridad

- **Autenticación:** `Authorization: Bearer <API_TOKEN>`, comparado en tiempo constante.
  - Está preparada para recibir del ERP un **JWT HS256** (`ERP_JWT_SECRETO` de al menos
    32 caracteres, y opcionalmente `ERP_JWT_EMISOR` y `ERP_JWT_AUDIENCIA`).
  - El JWT debe traer `sub` (el usuario) y `exp`. Cada usuario ve solo sus conversaciones.
- **El servidor MCP en HTTP** escucha en `127.0.0.1` y exige `MCP_TOKEN`, porque expone
  `confirmar_operacion`.
- **CORS:** `CORS_ORIGINS` (lista separada por comas; vacía por defecto).
- **Límites:** `MAX_ITERACIONES_TOOLS` (8 por mensaje) y `MAX_LARGO_MENSAJE`.
- **Sin secretos en el historial:** de cada acción pendiente se guarda solo el hash del
  token. El uso de tokens del modelo se registra por conversación.
- **Privacidad:** con un proveedor en la nube, los resultados de las tools (datos del
  inventario) se envían a ese proveedor. Para datos sensibles de un ERP, ver el trabajo
  futuro.

### Probar por consola con el proveedor real

```powershell
.\.venv\Scripts\python.exe scripts\chat_consola.py             # servidor MCP en el mismo proceso
.\.venv\Scripts\python.exe scripts\chat_consola.py --mcp-http  # contra stockmind-mcp --http
```

Antes de cada confirmación pregunta *¿Confirmar? [s/N]*: ese es el "botón" del usuario.
Comandos: `/nueva`, `/uso` y `/salir`.

### Trabajo futuro: modelo local compatible con OpenAI

Un adaptador `LLMProvider` para la **API compatible con OpenAI** permitiría usar un
modelo **local** (Ollama o LM Studio) en la misma red de la empresa.

- **El argumento es la privacidad:** los datos del ERP (productos, costos, ventas,
  proveedores) **no saldrían de la empresa**. Para integrar StockMind en un ERP real
  puede ser un requisito.
- **Lo que cambia:** el puerto ya está listo. Solo hay que traducir mensajes y tools al
  formato de *chat completions* y apuntar `base_url` al servidor local.
- **Lo que no cambia:** la seguridad (human in the loop, MCP con token, auditoría).
- **El costo:** los modelos locales chicos usan peor las tools. Habría que medir la
  calidad con los mismos casos de prueba antes de recomendarlo.

## Probar con MCP Inspector

Desde la raíz del proyecto, con los datos ya generados:

```powershell
npx @modelcontextprotocol/inspector@1 .\.venv\Scripts\stockmind-mcp.exe
```

Se abre el navegador. Pulsa **Connect** y luego **Tools → List Tools**. Para
probar el flujo de escritura:

1. Llama a `registrar_movimiento` con `producto=ELE-001`, `tipo=entrada` y
   `cantidad=10`. Devuelve una vista previa y un `token`.
2. Llama a `confirmar_operacion` con ese `token`. Se guarda el movimiento.
3. Vuelve a confirmar con el mismo token: lo rechaza, porque es de un solo uso.

Sin navegador (modo CLI):

```powershell
npx @modelcontextprotocol/inspector@1 --cli .\.venv\Scripts\stockmind-mcp.exe --method tools/list
npx @modelcontextprotocol/inspector@1 --cli .\.venv\Scripts\stockmind-mcp.exe --method tools/call --tool-name listar_bajo_stock
```

> Inspector 2.x pide Node ≥ 22.19. Con un Node más antiguo usa `@1`, como en
> los ejemplos. Después de actualizar Node, `npx @modelcontextprotocol/inspector@latest --web ...`.

## Conectar a Claude Desktop (Windows)

1. Abre Claude Desktop → **Configuración → Desarrollador → Editar configuración**.
   Así se abre `claude_desktop_config.json` sin buscarlo. Normalmente está en
   `%APPDATA%\Claude\claude_desktop_config.json`.
2. Agrega el servidor; tienes un ejemplo en `claude_desktop_config.example.json`.
   En JSON, cada `\` va doble:

   ```json
   {
     "mcpServers": {
       "stockmind": {
         "command": "D:\\UAGRM\\Taller de Grado\\.venv\\Scripts\\stockmind-mcp.exe",
         "args": []
       }
     }
   }
   ```

   No hace falta `cwd`: el servidor ubica `.env`, `data\` y `logs\` desde su
   propia ruta.
3. **Cierra Claude Desktop del todo**, también desde el ícono de la bandeja, y
   vuelve a abrirlo. StockMind aparece en el menú de herramientas.
4. Pruébalo con frases como:
   - "¿Qué productos están bajo el stock mínimo?"
   - "¿Cuánto cemento vendimos en agosto?"
   - "Registra una entrada de 50 bolsas de cemento".
   - "Crea un producto nuevo: carretilla de 60 litros".

Si no aparece, revisa `%APPDATA%\Claude\logs\mcp-server-stockmind.log`.

## Configuración (.env)

| Variable | Por defecto | Uso |
|---|---|---|
| `CONNECTOR` | `csv` | Conector activo: `csv` o un conector opcional instalado |
| `DATA_DIR` | `data` | Carpeta de los archivos del conector CSV |
| `FORMATO_DATOS` | `csv` | `csv` o `xlsx` |
| `CONFIRMACIONES_DB` | `data/stockmind.db` | SQLite local: tokens y bandeja de salida, compartido entre procesos |
| `TOKEN_TTL_MINUTOS` | `10` | Vigencia del token |
| `AUDITORIA_LOG` | `logs/auditoria.jsonl` | Log de auditoría |
| `EXPORTS_DIR` | `exports` | Archivos de `exportar_pendientes` y de los reportes (ignorada por git) |
| `MONEDA_BASE` | `BOB` | Moneda de los productos que no informan una |
| `MCP_HOST` / `MCP_PORT` / `MCP_TOKEN` | `127.0.0.1` / `8765` / — | Servidor MCP en HTTP (`stockmind-mcp --http`) |
| `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` | `anthropic` / (por proveedor) / — | Proveedor de IA del chat |
| `LLM_THINKING` / `LLM_FALLBACKS` / `LLM_MAX_TOKENS` | `bajo` / `true` / `16000` | Ajustes del modelo |
| `LLM_MODELOS_RESPALDO` / `LLM_REINTENTOS` / `LLM_ESPERA_INICIAL` | (por proveedor) / `3` / `2` | Reintentos y modelos de respaldo |
| `LLM_ENFRIAMIENTO_SEGUNDOS` / `LLM_TIMEOUT_SEGUNDOS` | `300` / `30` | Circuit breaker y tiempo máximo sin respuesta |
| `HISTORIAL_TURNOS` / `MAX_FILAS_RESULTADO` | `8` / `15` | Tokens por llamada (0 = sin límite) |
| `API_HOST` / `API_PORT` / `API_TOKEN` | `127.0.0.1` / `8000` / — | API del chat |
| `CORS_ORIGINS` | — | Orígenes permitidos (separados por comas) |
| `MCP_URL` | `http://127.0.0.1:8765/mcp` | Dónde encuentra la API al servidor MCP |
| `MAX_ITERACIONES_TOOLS` / `MAX_LARGO_MENSAJE` | `8` / `4000` | Límites por mensaje |
| `ERP_JWT_SECRETO` / `ERP_JWT_EMISOR` / `ERP_JWT_AUDIENCIA` | — | JWT firmado por el ERP (opcional) |
| `LOG_LEVEL` | `INFO` | Los logs van a stderr |

Las rutas relativas se resuelven desde la raíz del proyecto. Una variable vacía cuenta como no definida. Los conectores opcionales documentan sus propias variables.

## Tools MCP

| Tool | Tipo | Qué hace |
|---|---|---|
| `buscar_productos(texto, limite)` | lectura | Busca por código, nombre, categoría o proveedor, sin importar tildes |
| `obtener_producto(id_o_codigo)` | lectura | Detalle y stock de un producto |
| `listar_bajo_stock()` | lectura | Productos con stock < mínimo |
| `listar_movimientos(producto, desde, hasta, limite)` | lectura | Kardex con saldo |
| `ventas_por_producto(desde, hasta, limite)` | lectura | Unidades y monto vendidos |
| `campos_obligatorios_producto()` | lectura | Qué pide el ERP para crear un producto |
| `crear_producto(datos)` | prepara | Vista previa + token; **no guarda** |
| `registrar_movimiento(...)` | prepara | Vista previa + token; **no guarda** |
| `confirmar_operacion(token)` | escribe | Ejecuta la operación preparada |
| `cancelar_operacion(token)` | — | Descarta la operación preparada |
| `listar_pendientes`, `exportar_pendientes`, `verificar_pendientes`, `marcar_registrada`, `descartar_pendiente` | bandeja | Ver "Bandeja de salida" |
| `reporte_mas_vendidos`, `reporte_bajo_stock`, `reporte_sin_movimiento`, `reporte_abc`, `reporte_rotacion`, `resumen_ejecutivo` | lectura | Ver "Reportes de inventario" |

## Estructura

```
src/stockmind/
  domain/        modelo común (Pydantic) y errores
  ports/         InventarioConnector e IntegracionBandeja (interfaces abstractas)
  adapters/      CsvExcelConnector (los conectores opcionales van en subpaquetes)
  application/   validación, confirmaciones (ConfirmacionStore), bandeja (BandejaStore), reportes, auditoría, servicio
  mcp_server/    servidor MCP (MCPServer del SDK oficial; stdio o streamable HTTP con token)
  agente/        agente cliente MCP: LLMProvider (Anthropic, Gemini, falso), historial, prompt
  api/           backend FastAPI del chat (SSE, acciones del usuario, reportes, bandeja, salud)
  config/        settings (pydantic-settings)
  composicion.py arma el servicio con el conector de .env
scripts/generar_datos.py
scripts/iniciar.ps1         levanta el MCP en HTTP y la API con un comando
scripts/chat_consola.py     chat por consola con el proveedor real
data/            datos sintéticos generados (CSV)
tests/
```

Las decisiones de arquitectura y las reglas del proyecto están en [CLAUDE.md](CLAUDE.md).
