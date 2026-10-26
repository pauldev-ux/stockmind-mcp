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
- la bandeja de salida con conectores falsos (estados, stock comprometido, exportar, verificar).

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
| `EXPORTS_DIR` | `exports` | Archivos de `exportar_pendientes` (ignorada por git) |
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

## Estructura

```
src/stockmind/
  domain/        modelo común (Pydantic) y errores
  ports/         InventarioConnector e IntegracionBandeja (interfaces abstractas)
  adapters/      CsvExcelConnector (los conectores opcionales van en subpaquetes)
  application/   validación, confirmaciones (ConfirmacionStore), bandeja (BandejaStore), auditoría, servicio
  mcp_server/    servidor MCP (MCPServer del SDK oficial)
  config/        settings (pydantic-settings)
  composicion.py arma el servicio con el conector de .env
scripts/generar_datos.py
data/            datos sintéticos generados (CSV)
tests/
```

Las decisiones de arquitectura y las reglas del proyecto están en [CLAUDE.md](CLAUDE.md).
