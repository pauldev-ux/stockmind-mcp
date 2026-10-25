# CLAUDE.md — StockMind

StockMind es un asistente de inventario con IA que se conecta a cualquier ERP
por MCP. Es el proyecto de grado (UAGRM) del autor. Todo corre en localhost,
en Windows.

## Entorno

- Windows 11, PowerShell. **La ruta tiene espacios**
  (`D:\UAGRM\Taller de Grado`): comillas siempre.
- Python **3.14** en `.venv` (`py -3.14 -m venv .venv`). Hay un Python 3.10
  instalado, pero no sirve porque el mínimo es 3.11.
  Las dependencias están fijadas en `requirements.txt`, que también instala el
  paquete con `-e .`.
- Comandos: `.\.venv\Scripts\python.exe -m pytest`,
  `python scripts\generar_datos.py [--forzar] [--formato xlsx]`, y el servidor
  con `.\.venv\Scripts\stockmind-mcp.exe` (stdio).
- **SDK MCP 2.x**: `FastMCP` se renombró a `MCPServer`
  (`from mcp.server.mcpserver import MCPServer`); `mcp.server.fastmcp` ya no
  existe. Los tests usan `mcp.Client(servidor)` en el mismo proceso.
- MCP Inspector 2.x pide Node ≥ 22.19. La máquina tiene 22.12, así que se usa
  `npx @modelcontextprotocol/inspector@1`.
- `docs/` es documentación del autor: **no borrar**.
  `docs/analisis-canaan360.md` describe el primer ERP real.

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
- `application/servicio.py`: lo usan el servidor MCP y, más adelante, un
  backend **FastAPI** en otro proceso con `construir_servicio(origen="web")`.
  No metas lógica de negocio en `mcp_server/`.

### Modelo común: decisiones por Canaan360

- **Ids:** son `str`. Canaan usa BIGINT; el CSV, `P0001`.
- **`codigo`:** es opcional y **no único**. `obtener_producto` busca por id y
  luego por código; si el código se repite, lanza `CodigoAmbiguo` con los
  candidatos.
- **Stock:**
  - `stock_actual` es el total.
  - `stock_por_almacen`, `almacen` y `Lote` son opcionales, porque Canaan
    tiene almacenes por empresa y lotes con FEFO.
  - El FEFO lo resuelve el adaptador de cada ERP, no el dominio.
- **Movimientos:**
  - `cantidad` siempre es mayor que 0.
  - El signo lo da `tipo` (entrada `+`, salida `-`) o `sentido` en los
    ajustes. Se mapea a `ajuste_positivo`/`ajuste_negativo` del kardex.
  - `saldo` es el saldo después del movimiento (`saldo_cantidad`).
- **Datos propios del ERP:** van en `Producto.atributos`, p. ej. tipo o moneda
  en Canaan.
- **Campos obligatorios:** cambian por ERP. `campos_obligatorios_producto()`
  del conector se combina con `CAMPOS_COMUNES_PRODUCTO`.
- **Crear un producto no crea stock**, igual que en Canaan. El stock entra
  con un movimiento `entrada`.

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
   - Los campos desconocidos se **rechazan**, no se ignoran. En Canaan se
     perdía `motivo` en silencio.
   - Se aceptan la coma decimal y las fechas `DD/MM/AAAA`.
   - En salidas y ajustes, `motivo` es obligatorio.
   - No se aceptan fechas futuras.
4. **No se permite stock negativo**: lo revisa la validación y también el
   adaptador.
5. **Auditoría.** Cada operación se registra en `logs/auditoria.jsonl`.
   - Eventos: `preparada`, `confirmada`, `rechazada` y `cancelada`.
   - Campos: `origen`, `conector` y un prefijo de 12 caracteres del hash del token.
6. **Las tools MCP no lanzan errores de negocio.** Devuelven
   `{"ok": false, "error", "mensaje", ...}`; de eso se encarga el decorador
   `_seguro`.
7. **stdio:** stdout es el protocolo. Los logs van a stderr; **nunca `print`
   en el servidor**.

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

Son **sintéticos**: el análisis de Canaan360 muestra que no hay historial
real suficiente para predecir.

## Próximos pasos previstos

- **`Canaan360Connector`:**
  - Conexión directa a MySQL, porque Canaan no tiene API ni Sanctum en uso.
  - Siempre con `empresa_id`.
  - Stock: `SUM(cantidad_disponible)` de `cerecom_stock_actual`.
  - Kardex: `cerecom_kardex` con `estado='activo'`.
  - Ventas: ventas `confirmado` desde `venta_detalles`.
  - Lo que falte por resolver está en "Dudas" de `docs/analisis-canaan360.md`.
- **Backend FastAPI** con chat propio, que reutilice `construir_servicio(origen="web")`.
- **Predicción de demanda y punto de reorden**, a partir de `ventas_por_producto`
  y del kardex.

Para agregar un conector: implementa `InventarioConnector` en `adapters/`,
regístralo en `composicion.CONECTORES` y escribe tests con datos de prueba,
sin tocar la BD real.
