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
- **Datos propios del ERP:** van en `Producto.atributos` (tipo de producto,
  moneda…).
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

- **Backend FastAPI** con chat propio, que reutilice `construir_servicio(origen="web")`.
- **Predicción de demanda y punto de reorden**, a partir de `ventas_por_producto`
  y del kardex.
