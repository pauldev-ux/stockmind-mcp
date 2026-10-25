"""Servidor MCP de StockMind (SDK oficial de Python, MCPServer; en mcp 1.x se llamaba FastMCP).

Transporte stdio: stdout es el canal del protocolo, así que todo log va a stderr.
Las tools nunca lanzan errores de negocio: devuelven {"ok": false, ...} con el
mensaje en español para que la IA se lo explique al usuario.
"""

import functools
import logging
import sys
from collections.abc import Callable
from datetime import date
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from stockmind.application.servicio import InventarioService
from stockmind.application.validacion import convertir
from stockmind.domain.errores import ErrorValidacion, StockMindError
from stockmind.domain.modelos import CampoRequerido, ErrorCampo, Producto

log = logging.getLogger("stockmind.mcp")

INSTRUCCIONES = """\
StockMind consulta y modifica el inventario del ERP conectado.

Reglas para escribir (crear productos o registrar movimientos):
1. Las tools crear_producto y registrar_movimiento NO guardan nada: validan y
   devuelven una vista previa y un token.
2. Muestra la vista previa al usuario y espera que la acepte de forma explícita.
   Nunca llames a confirmar_operacion por tu cuenta.
3. Si la respuesta trae "faltantes" o "invalidos", pídele esos datos al usuario
   en lenguaje natural; no inventes valores.
4. El token vence a los 10 minutos y sirve una sola vez.
5. No se permite stock negativo.

Antes de crear un producto, consulta campos_obligatorios_producto: cada ERP
exige campos distintos.
"""

_LECTURA = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_PREPARAR = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
_CONFIRMAR = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


def _producto(p: Producto) -> dict[str, Any]:
    datos = p.model_dump(mode="json", exclude_none=True, exclude={"atributos"} if not p.atributos else None)
    return {**datos, "bajo_stock": p.bajo_stock}


def _fecha(campo: str, valor: str | None) -> date | None:
    """Convierte una fecha opcional; el error sale en español como el resto de validaciones."""
    if valor is None or not str(valor).strip():
        return None
    try:
        return convertir(CampoRequerido(nombre=campo, tipo="fecha", descripcion=campo), valor)
    except ValueError as e:
        raise ErrorValidacion(invalidos=[ErrorCampo(campo=campo, mensaje=str(e))]) from None


def _seguro(funcion: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Convierte los errores en respuestas {"ok": false} en lugar de excepciones."""

    @functools.wraps(funcion)
    def envoltura(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return funcion(*args, **kwargs)
        except StockMindError as e:
            return e.a_dict()
        except Exception:
            log.exception("Error inesperado en la tool %s", funcion.__name__)
            return {"ok": False, "error": "ErrorInterno",
                    "mensaje": "Ocurrió un error interno; revisa el log del servidor."}

    return envoltura


def crear_servidor(servicio: InventarioService) -> MCPServer:
    mcp = MCPServer(name="stockmind", title="StockMind", instructions=INSTRUCCIONES, version="0.1.0")

    # ---------------------------------------------------------------- lectura

    @mcp.tool(annotations=_LECTURA)
    @_seguro
    def buscar_productos(texto: str = "", limite: int = 20) -> dict[str, Any]:
        """Busca productos por código, nombre, categoría o proveedor (sin importar tildes ni mayúsculas).
        Con texto vacío lista todos. Máximo 100 resultados."""
        productos = servicio.buscar_productos(texto, limite)
        return {"ok": True, "total": len(productos), "productos": [_producto(p) for p in productos]}

    @mcp.tool(annotations=_LECTURA)
    @_seguro
    def obtener_producto(id_o_codigo: str) -> dict[str, Any]:
        """Devuelve un producto por su id o su código, con stock actual y stock mínimo."""
        return {"ok": True, "producto": _producto(servicio.obtener_producto(id_o_codigo))}

    @mcp.tool(annotations=_LECTURA)
    @_seguro
    def listar_bajo_stock() -> dict[str, Any]:
        """Productos activos cuyo stock actual está por debajo del stock mínimo, los más críticos primero."""
        productos = servicio.listar_bajo_stock()
        return {"ok": True, "total": len(productos), "productos": [_producto(p) for p in productos]}

    @mcp.tool(annotations=_LECTURA)
    @_seguro
    def listar_movimientos(
        producto: str, desde: str | None = None, hasta: str | None = None, limite: int = 100
    ) -> dict[str, Any]:
        """Kardex de un producto (id o código): entradas, salidas y ajustes con su saldo.
        Fechas AAAA-MM-DD, inclusivas. Devuelve los últimos `limite` movimientos del rango."""
        p, movimientos, total = servicio.listar_movimientos(
            producto, _fecha("desde", desde), _fecha("hasta", hasta), limite)
        return {
            "ok": True,
            "producto": {"id": p.id, "codigo": p.codigo, "nombre": p.nombre, "stock_actual": p.stock_actual},
            "total_en_rango": total,
            "mostrados": len(movimientos),
            "movimientos": [m.model_dump(mode="json", exclude_none=True) for m in movimientos],
        }

    @mcp.tool(annotations=_LECTURA)
    @_seguro
    def ventas_por_producto(desde: str | None = None, hasta: str | None = None, limite: int = 50) -> dict[str, Any]:
        """Unidades y monto vendidos por producto en un rango de fechas (AAAA-MM-DD, inclusivo),
        ordenados de mayor a menor cantidad. Sin fechas usa todo el historial."""
        resumen = servicio.ventas_por_producto(_fecha("desde", desde), _fecha("hasta", hasta), limite)
        return {"ok": True, "desde": desde, "hasta": hasta, "productos": [r.model_dump(mode="json") for r in resumen]}

    @mcp.tool(annotations=_LECTURA)
    @_seguro
    def campos_obligatorios_producto() -> dict[str, Any]:
        """Campos obligatorios y opcionales para crear un producto en el ERP conectado."""
        return {"ok": True, **servicio.campos_producto()}

    # -------------------------------------------------------------- escritura

    @mcp.tool(annotations=_PREPARAR)
    @_seguro
    def crear_producto(datos: dict[str, Any] | None = None) -> dict[str, Any]:
        """Prepara la creación de un producto. NO guarda: valida y devuelve una vista previa y un token
        que vence en 10 minutos. `datos` es un objeto con los campos (ver campos_obligatorios_producto),
        p. ej. {"nombre": "...", "categoria": "...", "unidad": "...", "precio_venta": 10, "costo": 7}.
        Crear un producto no crea stock: luego registra una entrada."""
        return servicio.preparar_crear_producto(datos or {})

    @mcp.tool(annotations=_PREPARAR)
    @_seguro
    def registrar_movimiento(
        producto: str | None = None,
        tipo: str | None = None,
        cantidad: float | str | None = None,
        sentido: str | None = None,
        motivo: str | None = None,
        fecha: str | None = None,
        almacen: str | None = None,
        lote: str | None = None,
        fecha_vencimiento: str | None = None,
        costo_unitario: float | str | None = None,
        documento: str | None = None,
    ) -> dict[str, Any]:
        """Prepara un movimiento de inventario. NO guarda: valida y devuelve una vista previa y un token
        que vence en 10 minutos.
        - producto: id o código. tipo: entrada | salida | ajuste. cantidad: mayor que 0.
        - sentido: solo en ajustes, '+' suma o '-' resta.
        - motivo: obligatorio en salidas y ajustes.
        - fecha: AAAA-MM-DD, por defecto hoy; no puede ser futura.
        Rechaza movimientos que dejarían el stock negativo."""
        argumentos = {
            "producto_id": producto, "tipo": tipo, "cantidad": cantidad, "sentido": sentido, "motivo": motivo,
            "fecha": fecha, "almacen": almacen, "lote": lote, "fecha_vencimiento": fecha_vencimiento,
            "costo_unitario": costo_unitario, "documento": documento,
        }
        return servicio.preparar_movimiento({k: v for k, v in argumentos.items() if v is not None})

    @mcp.tool(annotations=_CONFIRMAR)
    @_seguro
    def confirmar_operacion(token: str) -> dict[str, Any]:
        """Ejecuta una operación preparada. Llamar SOLO después de que el usuario aceptó
        explícitamente la vista previa. El token sirve una vez y vence a los 10 minutos."""
        return servicio.confirmar(token)

    @mcp.tool(annotations=_CONFIRMAR)
    @_seguro
    def cancelar_operacion(token: str) -> dict[str, Any]:
        """Descarta una operación preparada que el usuario no quiere guardar."""
        return servicio.cancelar(token)

    return mcp


def main() -> None:
    from stockmind.composicion import construir_servicio
    from stockmind.config.settings import get_settings

    settings = get_settings()
    logging.basicConfig(
        stream=sys.stderr, level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    servicio = construir_servicio(settings, origen="mcp")
    log.info("StockMind MCP con conector '%s'", servicio.conector.nombre)
    crear_servidor(servicio).run("stdio")


if __name__ == "__main__":
    main()
