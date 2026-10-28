# CLAUDE.md — StockMind

StockMind es un asistente de inventario con IA que se conecta a cualquier ERP
por MCP. Es el proyecto de grado (UAGRM) del autor. Todo corre en localhost,
en Windows. **El repositorio es público.**

> Si existe `docs/CLAUDE-privado.md` (local, no versionado), léelo al empezar.
> Tiene las reglas y los detalles del conector privado para un ERP real.

## Reglas permanentes del autor (prioridad máxima)

- **NUNCA hagas commits ni otras operaciones de git que cambien el índice o el
  historial** (add, rm, commit, amend, rebase, reset, merge, push, tag, stash…).
  Los commits los hace el autor. Sí se permiten consultas: `git status`,
  `git diff`, `git log`, `git ls-files`, `git check-ignore`.
- **NUNCA modifiques el ERP real, su código ni su base de datos.** Contra su
  base solo se ejecutan consultas: nada de INSERT, UPDATE, DELETE, ALTER,
  CREATE, DROP, GRANT ni crear usuarios. Los tests de integración tampoco
  intentan escribir, ni siquiera para comprobar que falla.
- **Nada interno del ERP real en archivos públicos.** Ni nombres, ni tablas, ni
  rutas locales, ni errores del ERP. Eso va en `docs/` o en el conector
  privado, que están en `.gitignore`. En lo público se dice solo "conector para
  un ERP real (privado)".

## Entorno

- Windows 11, PowerShell. **La ruta tiene espacios**
  (`D:\UAGRM\Taller de Grado`): comillas siempre.
- Python **3.14** en `.venv` (`py -3.14 -m venv .venv`). Hay un Python 3.10
  instalado, pero no sirve porque el mínimo es 3.11.
  - Las dependencias están fijadas en `requirements.txt`, que también instala
    el paquete con `-e .`.
  - Los conectores con MySQL usan el extra `pip install -e ".[mysql]"`.
- Comandos: `.\.venv\Scripts\python.exe -m pytest`,
  `python scripts\generar_datos.py [--forzar] [--formato xlsx]`, y el servidor
  con `.\.venv\Scripts\stockmind-mcp.exe` (stdio).
- **SDK MCP 2.x**: `FastMCP` se renombró a `MCPServer`
  (`from mcp.server.mcpserver import MCPServer`); `mcp.server.fastmcp` ya no
  existe. Los tests usan `mcp.Client(servidor)` en el mismo proceso.
- MCP Inspector 2.x pide Node ≥ 22.19. La máquina tiene 22.12, así que se usa
  `npx @modelcontextprotocol/inspector@1`.
- `docs/` es documentación privada del autor: **no borrar** (está en `.gitignore`).

## Arquitectura (hexagonal)

```
mcp_server ─► application.InventarioService ─► ports.InventarioConnector ◄─ adapters.*
                 ├─ validacion   (dict → modelo común, errores en español)
                 ├─ confirmaciones (GestorConfirmaciones + ConfirmacionStore)
                 └─ auditoria    (JSONL)
composicion.py: único lugar que conoce adaptadores y aplicación (elige por CONNECTOR en .env)
```

- `domain/`: modelos Pydantic y errores, sin dependencias de infraestructura.
- `ports/inventario.py`: el contrato que implementa cada ERP.
  - Tiene 8 métodos abstractos.
    `ventas_por_producto(desde, hasta, almacen=None)` acepta el filtro por almacén.
  - Tiene **métodos con implementación por defecto**, que un conector con base
    de datos debería optimizar:
    - `listar_productos()`, que usa `buscar_productos("")`;
    - `resumen_movimientos(desde, hasta, almacen)`, que da por producto
      entradas, salidas, neto y última entrada y salida, recorriendo
      `listar_movimientos`.
  - Tiene un gancho opcional, `validar_producto_nuevo`, para reglas que
    necesitan los datos del ERP (nombre único…).
- `adapters/`: solo E/S y traducción al modelo común. **No** validan entradas
  de usuario ni emiten tokens, pero **sí** rechazan el stock negativo como
  segunda barrera (`StockInsuficiente`).
- **Modo de escritura** (`modo_escritura`, `ModoEscritura`):
  - `DIRECTA` (CSV): al confirmar se escribe en el ERP.
  - `BANDEJA`: al confirmar, la operación queda **pendiente** en la bandeja de
    salida (ver más abajo). Nunca se llama a `crear_producto` o
    `registrar_movimiento` del conector.
  - `NINGUNA`: `EscrituraNoHabilitada` **antes** de validar o emitir un token.
  - `nombre_visible` es el nombre que se muestra en los mensajes.
- **Ganchos opcionales del puerto:**
  - `validar_producto_nuevo` (errores) y `advertencias_producto_nuevo`.
  - `validar_movimiento_nuevo(mov, producto, pendientes) → Revision`, que
    devuelve faltantes, errores, advertencias y `detalle`. El `detalle` (p. ej.
    almacén o reparto por lotes) se muestra en la vista previa y se guarda en la
    bandeja.
  - **Orden de la validación de movimientos:**
    1. `construir_movimiento`;
    2. las reglas del ERP;
    3. `revisar_stock`, con el almacén que eligió el ERP si el usuario no
       indicó uno.
- `application/servicio.py`: lo usan el servidor MCP y, más adelante, un
  backend **FastAPI** en otro proceso con `construir_servicio(origen="web")`.
  No metas lógica de negocio en `mcp_server/`.

### Conectores incluidos y opcionales

- **Incluidos:** `composicion.CONECTORES` (hoy, `csv`).
- **Opcionales:** cualquier subpaquete `stockmind.adapters.<nombre>` que
  exponga `crear_conector(settings)`. Se elige con `CONNECTOR=<nombre>`.
  - Si el paquete no existe, el error lo dice con claridad.
  - Si le falta una dependencia, sugiere el extra `[mysql]`.
- **Privados:** por convención, todo subpaquete de `adapters/` y
  `tests/privados/` están en `.gitignore`.
  - Cada conector lee su propia configuración del mismo `.env`, con su
    propio prefijo.
  - **El código público no puede importar nada de un conector privado.**
  - Los tests públicos deben pasar sin él.

### Modelo común

- **Ids:** son `str`, para que sirvan igual los BIGINT de una base que los
  códigos del CSV (`P0001`).
- **`codigo`:** es opcional y **no único**. `obtener_producto` busca por id y
  luego por código; si el código se repite, lanza `CodigoAmbiguo` con los
  candidatos.
- **Stock:**
  - `stock_actual` es el total.
  - `stock_por_almacen`, `almacen` y `Producto.lotes` son opcionales, para
    ERPs con varios almacenes y lotes (FEFO).
  - El FEFO lo resuelve el adaptador de cada ERP, no el dominio. Los lotes van
    ya ordenados.
- **Movimientos:**
  - `cantidad` siempre es mayor que 0.
  - El signo lo da `tipo` (entrada `+`, salida `-`) o `sentido` en los ajustes.
  - `saldo` es el saldo después del movimiento; en un ERP puede ser por almacén.
  - Los datos propios del ERP van en `MovimientoInventario.atributos`.
    **Excluirlo** al guardar los datos de una operación: si no, la
    revalidación lo rechaza como campo desconocido.
- **Moneda:** `Producto.moneda` (opcional; si es `None`, se usa `MONEDA_BASE`).
- **Datos propios del ERP:** van en `Producto.atributos` (p. ej. el tipo de producto).
- **Campos obligatorios:** cambian por ERP. `campos_obligatorios_producto()`
  del conector se combina con `CAMPOS_COMUNES_PRODUCTO`.
- **Crear un producto no crea stock.** El stock entra con un movimiento
  `entrada`.

## Reglas (no romper)

1. **Las escrituras se hacen en dos pasos.**
   - `crear_producto` y `registrar_movimiento` solo validan y devuelven la
     vista previa y un token.
   - Se ejecutan únicamente con `confirmar_operacion(token)`.
   - Al confirmar **se revalida**, porque el stock pudo cambiar.
2. **Tokens:**
   - Vencen a los 10 minutos (`TOKEN_TTL_MINUTOS`) y son de **un solo uso**:
     la toma es atómica, con `UPDATE … WHERE estado='pendiente' AND vence > ? RETURNING`.
   - Se guarda **solo el SHA-256**, nunca el token.
   - Viven en SQLite (`data/stockmind.db`, `SqliteConfirmacionStore`) para
     compartirlos entre procesos. En los tests se usa `MemoriaConfirmacionStore`.
   - Un token cuya confirmación falla queda gastado: hay que preparar de nuevo.
3. **Validar antes de escribir** y devolver en español `faltantes` e
   `invalidos`, para que la IA se los pida al usuario.
   - Los campos desconocidos se **rechazan**, no se descartan en silencio.
   - Se aceptan la coma decimal y las fechas `DD/MM/AAAA`.
   - En salidas y ajustes, `motivo` es obligatorio.
   - No se aceptan fechas futuras.
4. **No se permite stock negativo**: lo revisa la validación y también el
   adaptador.
5. **Auditoría.** Cada operación se registra en `logs/auditoria.jsonl`.
   - Eventos: `preparada`, `confirmada`, `rechazada` y `cancelada`; en la
     bandeja, `encolada`, `exportada`, `registrada` y `descartada`.
   - Campos: `origen`, `conector` y un prefijo de 12 caracteres del hash del token.
6. **Las tools MCP no lanzan errores de negocio.** Devuelven
   `{"ok": false, "error", "mensaje", ...}`; de eso se encarga el decorador
   `_seguro`.
7. **stdio:** stdout es el protocolo. Los logs van a stderr; **nunca `print`
   en el servidor**.
8. **`.env`:** con `env_ignore_empty=True`, una variable vacía cuenta como no
   definida y no rompe el arranque.

## Bandeja de salida (modo BANDEJA)

- **Almacenamiento:** `application/bandeja.py` (`BandejaStore`).
  - `SqliteBandejaStore` usa la misma `data/stockmind.db`, tabla
    `bandeja_operaciones`, compartida entre procesos.
  - `MemoriaBandejaStore` se usa en los tests.
- **Modelo:** `domain/bandeja.py`.
  - `OperacionBandeja` guarda los datos confirmados, la vista previa,
    `detalle_erp`, la guía, el estado, las fechas, la observación,
    `registrada_por` (`manual`/`conciliacion`), `id_externo`, `exportada_en` y
    `archivo`.
  - La referencia es `SM-000012`, y `parsear_referencia` acepta `SM-12` o `12`.
- **Estados:** `pendiente` → `registrada` | `descartada`.
  - La transición es atómica (`UPDATE … WHERE estado='pendiente' RETURNING`).
    Cualquier otra lanza `TransicionInvalida`.
  - Descartar exige un motivo.
- **Validación en modo bandeja:**
  - No se permiten dos productos pendientes con el mismo nombre.
  - **El stock comprometido** (salidas pendientes del mismo producto y almacén)
    se descuenta al validar. Al confirmar se revalida todo.
- **Integración:** `ports/bandeja.py::IntegracionBandeja` (`guia`, `exportar`,
  `verificar`).
  - El conector la implementa si quiere el formato del ERP y la conciliación.
  - Si no, se usa `IntegracionGenerica`: un Excel simple, una guía genérica y
    sin conciliación.
  - `verificar` es **solo lectura**. Una coincidencia exacta marca la operación
    como `registrada` (`registrada_por="conciliacion"`); las posibles **solo se
    informan**.
- **Exportar:** escribe en `exports/` (`EXPORTS_DIR`, ignorada por git) y no
  cambia el estado. Solo exporta las operaciones del conector activo.
- **Respuestas:** siempre dejan claro que la operación quedó **PENDIENTE** en el
  ERP (`estado: "pendiente_en_erp"`). Las instrucciones del servidor MCP se lo
  piden a la IA.
- **Auditoría:** `encolada`, `exportada`, `registrada` y `descartada`, con el
  campo `referencia`.

## Reportes (`application/reportes.py`, `domain/reportes.py`)

- **Genéricos:** usan solo métodos del puerto, así que funcionan con cualquier
  conector. `ServicioReportes` vive en `InventarioService.reportes`.
- **Modelos:** cada reporte es un Pydantic que hereda de `Reporte`.
  - Campos comunes: `periodo`, `filtros`, **`metodo`** (pasos del cálculo con
    los números usados) y **`avisos`**.
  - **`sin_datos`:** sin datos, el valor queda en `None` y hay un aviso;
    **nunca un cero engañoso**.
- **Período:** `desde`/`hasta` o `ultimos_dias` (terminan hoy), nunca los dos.
  - Por defecto: 30 días para más vendidos, bajo stock y resumen; 90 para sin
    movimiento, ABC y rotación.
  - No se aceptan fechas futuras.
- **Filtros:** `categoria` (sobre `Producto.categoria`) y `almacen` (stock con
  `stock_en`, y ventas y movimientos filtrados por el conector).
- **Fórmulas:**
  - **Bajo stock:**
    - Entra si `stock < stock_minimo`, o si se pasa `cobertura_menor_a`.
    - Venta diaria = vendido en `dias_ventas` / `dias_ventas`.
    - Cobertura = stock / venta diaria; sin ventas del producto es `None`.
    - Sugerido = `max(0, ⌈max(stock_minimo, venta diaria × dias_objetivo) − stock⌉)`.
      Sin tiempo de entrega ni stock de seguridad: eso es para la fase de
      predicción.
  - **Sin movimiento:** sin ventas ni salidas en N días. Capital = stock × costo,
    sumado por moneda.
  - **ABC:**
    - La clase se decide por el acumulado **antes** del producto: menos de A es
      A, menos de A + B es B, y el resto C.
    - Así el producto que cruza el 80 % queda en A.
    - Los productos sin ventas van aparte.
  - **Rotación:**
    - Costo de lo **vendido** (unidades × costo actual) / inventario promedio.
    - Inventario inicial = stock − neto desde el inicio del período; final =
      stock − neto posterior al período.
    - Las **otras salidas** (producción, mermas, transferencias) se informan
      aparte y no entran en la rotación.
  - **Valorización:** stock × costo y stock × precio, por categoría y por moneda.
    - Avisa si hay productos con stock y precio o costo en 0 (dato sin cargar).
  - **Resumen ejecutivo:** combina los anteriores y arma los `hallazgos` con
    reglas fijas.
- **Monedas:** `Producto.moneda`; si es `None`, se usa `MONEDA_BASE`. **Los
  totales nunca mezclan monedas.** Los montos de ventas van en la moneda base
  que informa el conector.
- **Exportación:** con `exportar=True`, `application/exportar_reportes.py` genera
  `exports/reporte_<tipo>_<sello>.xlsx`, con una hoja por lista y "Cómo se calculó".
- **Tests:** `tests/test_reportes.py` usa un CSV armado a mano; los resultados
  están calculados en el docstring.

## Chat con IA (`agente/`, `api/`)

- **El agente es un cliente MCP.** `agente/cliente_mcp.py` usa el SDK oficial:
  streamable HTTP con `Authorization: Bearer <MCP_TOKEN>`, o en proceso para tests y
  la consola.
  - **`agente/` y `api/` no importan `application`, `adapters`, `domain`, `ports` ni
    `composicion`.** Hay un test que lo verifica. Todo, incluidos los reportes y la
    bandeja del panel, pasa por las tools MCP.
- **El servidor MCP en HTTP:** `stockmind-mcp --http` (ruta `/mcp`) escucha en
  `127.0.0.1` y el middleware `ExigirToken` devuelve 401 sin `MCP_TOKEN`. Sin
  `--http` sigue en stdio para Claude Desktop.
- **`LLMProvider`** (`agente/llm.py`) trabaja con un formato neutro (`agente/modelos.py`).
  - Los mensajes del modelo guardan su `crudo`, que **se reenvía sin cambios** al mismo
    proveedor (thinking y firmas). A otro proveedor se le envía solo la versión neutra.
  - Los turnos seguidos del mismo rol se fusionan, con `tool_result` primero.
  - **Anthropic** (`proveedores/claude.py`):
    - modelo por defecto `claude-sonnet-5-5`, thinking y `output_config.effort` según `LLM_THINKING`;
    - caché de prompt automática, `eager_input_streaming` y `fallbacks: "default"` con
      el beta `server-side-fallback-2026-07-01`;
    - tras un fallback, se omiten los thinking/tool_use anteriores al último bloque
      `fallback`.
  - **Gemini** (`proveedores/gemini.py`):
    - modelo por defecto `gemini-3.8-flash` (nivel gratuito); `thinking_level` según `LLM_THINKING`;
    - API sin estado, con la llamada automática de funciones **desactivada**;
    - todas las `Part` se reenvían tal cual, incluida la `thought_signature`;
    - una clave inválida llega como **400** `API_KEY_INVALID` (no 401). Los errores
      incluyen el estado y el mensaje de Gemini, con la clave censurada (`detalle_error`).
  - **Falso:** responde por guion, para los tests.
  - **Reintentos y circuit breaker** (`ConReintentos` en `agente/llm.py`; `crear_proveedor`
    siempre lo pone):
    - `ErrorLLM(saturado=True)`: 503 (Gemini), 529/503/`overloaded_error` a mitad del stream
      (Anthropic; llega con HTTP 200) y timeout (`LLM_TIMEOUT_SEGUNDOS`; sin él, el SDK de
      Gemini esperaba indefinidamente). `reintentable=True` sin saturación: 429.
    - Un modelo saturado queda en enfriamiento `LLM_ENFRIAMIENTO_SEGUNDOS` (estado en la
      instancia: vale para todas las vueltas y mensajes) y se usa el siguiente de
      `LLM_MODELOS_RESPALDO` sin reintentar. Si todos están enfriados, se prueban igual.
    - Con respaldo, el primero se reintenta 1 vez (solo no saturación); sin respaldo, espera
      exponencial (`LLM_REINTENTOS`, `LLM_ESPERA_INICIAL`: 2, 4, 8 s). Los SDK van con
      `max_retries=0` para no reintentar dos veces.
    - Nunca se reintenta si ya se emitió texto. Cada cambio emite `EventoReintento`, que
      sale como el evento SSE `reintento`.
    - Al cambiar de modelo de Gemini, el `crudo` se reenvía tal cual (la documentación pide
      reenviar los bloques de razonamiento del modelo anterior).
- **Human in the loop:**
  - `HERRAMIENTAS_SOLO_USUARIO` (`confirmar_operacion`, `cancelar_operacion`,
    `marcar_registrada`, `descartar_pendiente`) **nunca se ofrecen al modelo**. Si las
    pide por nombre, se rechazan sin llegar al MCP.
  - **Vista previa con token:** se emite `accion_pendiente` (con el token, para el
    frontend), y el modelo recibe el resultado **sin token** y con `accion_id`.
  - **Confirmar o cancelar** van por `POST /api/v1/acciones/...`, con el token **en el
    cuerpo**.
    - Se verifica que la acción sea de esa conversación y de ese usuario; en SQLite
      solo se guarda el hash.
    - La toma es atómica (`pendiente` → `procesando`).
    - Después se agrega una nota del sistema al historial.
- **Historial:** `agente/historial.py`, en la misma `data/stockmind.db`, con las tablas
  `chat_conversaciones`, `chat_mensajes`, `chat_uso_llm` (uso de tokens por llamada) y
  `chat_acciones`.
  - El historial solo crece; no se editan mensajes viejos.
- **Autenticación:** `api/auth.py`, con `TokenEstatico` (`API_TOKEN`) y `JWTFirmado`
  (HS256; `ERP_JWT_SECRETO` de al menos 32 bytes; `sub` y `exp` obligatorios). El
  usuario JWT queda como `erp:<sub>`.
- **Límites:** `MAX_ITERACIONES_TOOLS` por mensaje (después corta con el error
  `LimiteIteraciones`) y `MAX_LARGO_MENSAJE`.
- **Vista previa = fin del turno:** si una tool deja una acción pendiente, el agente no vuelve
  a llamar al modelo en ese mensaje. Antes el modelo tardaba (minutos, con Gemini saturado) y el
  token de 10 min vencía antes de que la consola preguntara «¿Confirmar?».
- **Tiempos:** todo instante va en UTC con zona explícita (`exigir_zona`; un datetime sin zona
  es un error) y al usuario se le muestra en hora local (`hora_local`).
- **Tokens por llamada:**
  - thinking `LLM_THINKING` (bajo por defecto). `apagado` = lo mínimo de cada modelo: Gemini
    `MINIMAL` (o `LOW` en 3.8-flash), Sonnet 5.5 `between_tools`, Opus 5.5 no se apaga;
  - `compactar_esquema`: sin `title`, sin `default: null`, `anyOf [X, null]` → X (−55 %);
  - `recortar_para_modelo`: primeras `MAX_FILAS_RESULTADO` filas y `<lista>_total`. El evento
    `tool_fin` lleva el resultado completo (`datos`, sin token) para el panel;
  - `ventana_historial`: últimos `HISTORIAL_TURNOS` turnos y un resumen fijo de los anteriores
    (sin llamada extra). Si recorta, a Anthropic (`contexto_ligado`) se le envían los turnos
    previos sin `crudo`, porque su thinking queda ligado al prefijo exacto.
- **Conexiones:** la consola abre una sola sesión MCP; la API usa `ConexionPersistente` (una
  tarea dueña la abre una vez y la reabre si `ClienteMCP.roto`). Los modelos de respaldo
  comparten el cliente HTTP del principal. `chat_consola.py --tiempos` mide cada etapa.
- **Gotchas del SDK MCP:**
  - lo que se lanza dentro de `async with Client(...)` sale envuelto en
    `ExceptionGroup`, y `ClienteMCP` lo desenvuelve;
  - en un helper `evento(**datos)` no uses un parámetro llamado `nombre`, porque choca
    con los datos de las tools.
- **Arranque:** `scripts/iniciar.ps1` levanta los dos procesos y los detiene juntos.
  `scripts/chat_consola.py` es la prueba manual con el proveedor real (gasta cuota: lo
  corre el autor, no Claude).

## Conector CSV/Excel

- **Archivos:** `data/{productos,movimientos,ventas,ventas_detalle}.{csv|xlsx}`.
  Las columnas están en `COLUMNAS`.
- **Lectura:** todo se lee como texto y se convierte en el adaptador, porque
  los archivos pueden editarse a mano en Excel. Hay caché por mtime.
- **Escritura:**
  - Es atómica: temporal más `os.replace`, con rollback si falla la segunda tabla.
  - Se serializa entre procesos con `data/.stockmind.lock`.
  - Si Excel tiene el archivo abierto, sale un `ErrorAlmacenamiento` con un
    mensaje claro.
- **Formato:** el CSV va en UTF-8 con BOM, para que Excel muestre bien las tildes.
- **Ids nuevos:** `P0001` para productos y `M000001` para movimientos.

## Datos sintéticos

`scripts/generar_datos.py` usa semilla 42 y cubre del 25/10/2025 al 24/10/2026.

- **Catálogo:** 40 productos de ferretería en Bs, con proveedores ficticios.
- **Demanda:** binomial negativa con tendencia, semana, temporada de Santa
  Cruz y feriados.
- **Inventario:** política de punto de pedido.
- **Reproducibilidad:** los tests comprueban que el kardex cuadra con el stock
  y con las ventas, y que el resultado se repite con la misma semilla.
- **Publicación:** los CSV de `data/` se versionan; la base `data/stockmind.db`, no.

Son **sintéticos**, porque el ERP real todavía no tiene historial suficiente
para predecir.

## Próximos pasos previstos

- **Panel web en React**, que use la API (`/api/v1`) y los eventos SSE.
- **Integración en un módulo del ERP**, con JWT firmado por el ERP.
- **Adaptador OpenAI-compatible** para un modelo local (Ollama o LM Studio), para que
  los datos del ERP no salgan de la empresa.
- **Predicción de demanda y punto de reorden**, a partir de `ventas_por_producto`
  y del kardex.
