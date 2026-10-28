"""Servidor MCP de StockMind (SDK oficial de Python, MCPServer; en mcp 1.x se llamaba FastMCP).

Transportes:
- stdio (por defecto, para Claude Desktop): stdout es el canal del protocolo, así
  que todo log va a stderr.
- streamable HTTP (`--http`, para el backend del chat): escucha en 127.0.0.1 y
  exige `Authorization: Bearer <MCP_TOKEN>`, porque expone confirmar_operacion.
Las tools nunca lanzan errores de negocio: devuelven {"ok": false, ...} con el
mensaje en español para que la IA se lo explique al usuario.
"""

import argparse
import functools
import hmac
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
exige campos distintos. Su campo "modo_escritura" dice cómo escribe el ERP.

Modo "bandeja" (ERP que no admite escritura directa):
- Al confirmar, la operación NO se registra en el ERP: queda PENDIENTE en la
  bandeja de salida con una referencia (SM-000012) y una guía paso a paso.
  Díselo siempre al usuario con esas palabras; nunca digas que ya quedó guardada
  en el ERP.
- exportar_pendientes genera los archivos para cargarlos en el ERP.
- verificar_pendientes busca en el ERP (solo lectura) lo ya registrado y lo
  marca solo; las "posibles" coincidencias se las muestras al usuario para que
  decida con marcar_registrada.
- marcar_registrada y descartar_pendiente cambian el estado a pedido del usuario.

Reportes (reporte_* y resumen_ejecutivo):
- Para justificar una cifra, usa el campo "metodo" del reporte.
- Si "sin_datos" es true o hay "avisos", díselo al usuario tal cual. Un valor
  null significa que no se pudo calcular, no que valga cero.
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
    def ventas_por_producto(desde: str | None = None, hasta: str | None = None, limite: int = 50,
                            almacen: str | None = None) -> dict[str, Any]:
        """Unidades y monto vendidos por producto en un rango de fechas (AAAA-MM-DD, inclusivo),
        ordenados de mayor a menor cantidad. Sin fechas usa todo el historial."""
        resumen = servicio.ventas_por_producto(_fecha("desde", desde), _fecha("hasta", hasta), limite, almacen)
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
        """Prepara la creación de un producto (NO guarda: devuelve una vista previa con token, 10 min).
        `datos`: los campos de campos_obligatorios_producto, p. ej. {"nombre", "categoria", "unidad",
        "precio_venta", "costo"}. No crea stock: después se registra una entrada."""
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
        """Prepara un movimiento de inventario (NO guarda: devuelve una vista previa con token, 10 min).
        producto: id o código. tipo: entrada | salida | ajuste. cantidad > 0. sentido ('+'/'-'): solo
        ajustes. motivo: obligatorio en salidas y ajustes. fecha AAAA-MM-DD (hoy por defecto, no futura).
        Rechaza el stock negativo."""
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

    # ------------------------------------------------------ bandeja de salida

    @mcp.tool(annotations=_LECTURA)
    @_seguro
    def listar_pendientes(estado: str = "pendiente", tipo: str | None = None) -> dict[str, Any]:
        """Bandeja de salida. estado: pendiente | registrada | descartada | todas; tipo: productos |
        movimientos | todos. Las pendientes traen la guía para registrarlas en el ERP."""
        return servicio.listar_pendientes(estado, tipo)

    @mcp.tool(annotations=_PREPARAR)
    @_seguro
    def exportar_pendientes(tipo: str = "todos") -> dict[str, Any]:
        """Genera en exports/ los archivos para registrar las operaciones PENDIENTES en el ERP
        (tipo: productos | movimientos | todos). No cambia su estado: siguen pendientes."""
        return servicio.exportar_pendientes(tipo)

    @mcp.tool(annotations=_PREPARAR)
    @_seguro
    def verificar_pendientes() -> dict[str, Any]:
        """Busca en el ERP (solo lectura) las operaciones pendientes. Las que aparecen de forma exacta
        se marcan como registradas; las posibles coincidencias solo se informan."""
        return servicio.verificar_pendientes()

    @mcp.tool(annotations=_CONFIRMAR)
    @_seguro
    def marcar_registrada(id: str, observacion: str | None = None) -> dict[str, Any]:
        """Marca una operación pendiente (p. ej. SM-000012) como ya registrada en el ERP.
        Usar solo cuando el usuario lo indique."""
        return servicio.marcar_registrada(id, observacion)

    @mcp.tool(annotations=_CONFIRMAR)
    @_seguro
    def descartar_pendiente(id: str, motivo: str | None = None) -> dict[str, Any]:
        """Descarta una operación pendiente (p. ej. SM-000012): no se registrará en el ERP.
        El motivo es obligatorio. Usar solo cuando el usuario lo indique."""
        return servicio.descartar_pendiente(id, motivo)

    # ------------------------------------------------------------- reportes

    def _reporte(reporte, exportar: bool) -> dict[str, Any]:
        respuesta = {"ok": True, **reporte.model_dump(mode="json")}
        if exportar:
            respuesta["archivo"] = str(servicio.exportar_reporte(reporte))
        return respuesta

    rep = servicio.reportes

    @mcp.tool(annotations=_LECTURA)
    @_seguro
    def reporte_mas_vendidos(desde: str | None = None, hasta: str | None = None, ultimos_dias: int | None = None,
                             top: int = 10, categoria: str | None = None, almacen: str | None = None,
                             exportar: bool = False) -> dict[str, Any]:
        """Top N de productos por unidades y por monto vendido. Período: desde/hasta (AAAA-MM-DD)
        o ultimos_dias (por defecto, los últimos 30). exportar=true guarda un .xlsx en exports/."""
        return _reporte(rep.mas_vendidos(_fecha("desde", desde), _fecha("hasta", hasta), ultimos_dias, top,
                                         categoria, almacen), exportar)

    @mcp.tool(annotations=_LECTURA)
    @_seguro
    def reporte_bajo_stock(dias_ventas: int = 30, dias_objetivo: int = 30, cobertura_menor_a: float | None = None,
                           categoria: str | None = None, almacen: str | None = None,
                           exportar: bool = False) -> dict[str, Any]:
        """Productos bajo el stock mínimo con días de cobertura (stock / venta diaria de dias_ventas) y
        cantidad sugerida para cubrir dias_objetivo. cobertura_menor_a suma los de poca cobertura."""
        return _reporte(rep.bajo_stock(dias_ventas, dias_objetivo, cobertura_menor_a, categoria, almacen), exportar)

    @mcp.tool(annotations=_LECTURA)
    @_seguro
    def reporte_sin_movimiento(dias: int = 90, categoria: str | None = None, almacen: str | None = None,
                               solo_con_stock: bool = True, exportar: bool = False) -> dict[str, Any]:
        """Productos sin ventas ni salidas en los últimos `dias` días, con su capital inmovilizado
        (stock × costo) y la fecha de su última salida."""
        return _reporte(rep.sin_movimiento(dias, categoria, almacen, solo_con_stock), exportar)

    @mcp.tool(annotations=_LECTURA)
    @_seguro
    def reporte_abc(desde: str | None = None, hasta: str | None = None, ultimos_dias: int | None = None,
                    porcentaje_a: float = 80, porcentaje_b: float = 15, categoria: str | None = None,
                    almacen: str | None = None, exportar: bool = False) -> dict[str, Any]:
        """Clasificación ABC por monto vendido (A ≈ 80 %, B ≈ 15 %, C el resto; umbrales configurables),
        con porcentaje y porcentaje acumulado. Período por defecto: últimos 90 días."""
        return _reporte(rep.abc(_fecha("desde", desde), _fecha("hasta", hasta), ultimos_dias, porcentaje_a,
                                porcentaje_b, categoria, almacen), exportar)

    @mcp.tool(annotations=_LECTURA)
    @_seguro
    def reporte_rotacion(desde: str | None = None, hasta: str | None = None, ultimos_dias: int | None = None,
                         categoria: str | None = None, almacen: str | None = None,
                         exportar: bool = False) -> dict[str, Any]:
        """Rotación (costo de lo vendido / inventario promedio), días de inventario y valorización al
        costo y a precio de venta, por categoría y moneda. Por defecto, últimos 90 días."""
        return _reporte(rep.rotacion(_fecha("desde", desde), _fecha("hasta", hasta), ultimos_dias,
                                     categoria, almacen), exportar)

    @mcp.tool(annotations=_LECTURA)
    @_seguro
    def resumen_ejecutivo(desde: str | None = None, hasta: str | None = None, ultimos_dias: int | None = None,
                          categoria: str | None = None, almacen: str | None = None,
                          exportar: bool = False) -> dict[str, Any]:
        """Resumen ejecutivo del inventario en pocas líneas (hallazgos): ventas, más vendidos, bajo stock,
        sin movimiento, ABC, valorización y rotación. Período por defecto: últimos 30 días."""
        return _reporte(rep.resumen_ejecutivo(_fecha("desde", desde), _fecha("hasta", hasta), ultimos_dias,
                                              categoria, almacen), exportar)

    return mcp


class ExigirToken:
    """Middleware ASGI: rechaza con 401 toda petición HTTP sin `Authorization: Bearer <token>`."""

    def __init__(self, app: Any, token: str):
        if not token:
            raise ValueError("El modo HTTP necesita un MCP_TOKEN no vacío.")
        self.app = app
        self._esperado = f"Bearer {token}".encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            recibido = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(recibido, self._esperado):
                from starlette.responses import JSONResponse

                respuesta = JSONResponse({"ok": False, "error": "NoAutorizado",
                                          "mensaje": "Falta MCP_TOKEN o es incorrecto."}, status_code=401)
                await respuesta(scope, receive, send)
                return
        await self.app(scope, receive, send)


def crear_app_http(servidor: MCPServer, token: str, host: str = "127.0.0.1") -> Any:
    """App ASGI de streamable HTTP (ruta /mcp) protegida con el token."""
    return ExigirToken(servidor.streamable_http_app(host=host), token)


def main(argv: list[str] | None = None) -> None:
    from stockmind.composicion import construir_servicio
    from stockmind.config.settings import get_settings

    parser = argparse.ArgumentParser(prog="stockmind-mcp", description="Servidor MCP de StockMind.")
    parser.add_argument("--http", action="store_true", help="usar streamable HTTP en vez de stdio")
    parser.add_argument("--host", help="interfaz HTTP (por defecto MCP_HOST, 127.0.0.1)")
    parser.add_argument("--port", type=int, help="puerto HTTP (por defecto MCP_PORT, 8765)")
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(
        stream=sys.stderr, level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    servicio = construir_servicio(settings, origen="mcp")
    log.info("StockMind MCP con conector '%s'", servicio.conector.nombre)
    servidor = crear_servidor(servicio)
    if not args.http:
        servidor.run("stdio")
        return

    token = settings.mcp_token.get_secret_value()
    if not token:
        sys.exit("El modo HTTP necesita MCP_TOKEN en .env (cualquier texto largo y secreto).")
    import uvicorn

    host, puerto = args.host or settings.mcp_host, args.port or settings.mcp_port
    log.info("MCP streamable HTTP en http://%s:%s/mcp (requiere MCP_TOKEN)", host, puerto)
    uvicorn.run(crear_app_http(servidor, token, host), host=host, port=puerto,
                log_level=settings.log_level.lower(), access_log=False)


if __name__ == "__main__":
    main()
