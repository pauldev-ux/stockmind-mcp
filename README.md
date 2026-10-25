# StockMind

Asistente de inventario con IA que se conecta a cualquier ERP mediante
**MCP (Model Context Protocol)**. Proyecto de grado (UAGRM).

La IA (Claude Desktop, o el chat web que vendrá después) consulta productos,
stock, kardex y ventas, y puede crear productos o registrar movimientos, pero
**nunca escribe sin una confirmación explícita del usuario**.

```
IA ──MCP──► mcp_server ──► application (validación · confirmación · auditoría) ──► puerto ──► adaptador ──► ERP
                                                                                 InventarioConnector   CSV/Excel hoy
                                                                                                        Canaan360 después
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
python -m pytest
```

Cubren el conector CSV/Excel en ambos formatos, la validación, el flujo de
confirmación (memoria y SQLite), las tools MCP de punta a punta y la
coherencia del generador.

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
| `CONNECTOR` | `csv` | Conector activo |
| `DATA_DIR` | `data` | Carpeta de los archivos del conector CSV |
| `FORMATO_DATOS` | `csv` | `csv` o `xlsx` |
| `CONFIRMACIONES_DB` | `data/stockmind.db` | SQLite de tokens, compartido entre procesos |
| `TOKEN_TTL_MINUTOS` | `10` | Vigencia del token |
| `AUDITORIA_LOG` | `logs/auditoria.jsonl` | Log de auditoría |
| `LOG_LEVEL` | `INFO` | Los logs van a stderr |

Las rutas relativas se resuelven desde la raíz del proyecto.

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

## Estructura

```
src/stockmind/
  domain/        modelo común (Pydantic) y errores
  ports/         InventarioConnector (interfaz abstracta)
  adapters/      CsvExcelConnector
  application/   validación, confirmaciones (ConfirmacionStore), auditoría, servicio
  mcp_server/    servidor MCP (MCPServer del SDK oficial)
  config/        settings (pydantic-settings)
  composicion.py arma el servicio con el conector de .env
scripts/generar_datos.py
tests/
docs/            documentación (análisis de Canaan360)
```

Las decisiones de arquitectura y las reglas están en [CLAUDE.md](CLAUDE.md).
