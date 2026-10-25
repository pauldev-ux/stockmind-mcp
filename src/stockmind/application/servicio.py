"""Casos de uso de StockMind. Los usan el servidor MCP y, más adelante, el backend web.

Lecturas: pasan directo al conector, con límites razonables para no saturar a la IA.
Escrituras: validar → vista previa + token → confirmar (revalida) → conector → auditoría.
"""

from collections.abc import Callable
from datetime import date
from typing import Any

from stockmind.application.auditoria import Auditoria
from stockmind.application.confirmaciones import GestorConfirmaciones, OperacionPendiente, hash_token
from stockmind.application.validacion import esquema_producto, validar_movimiento, validar_producto
from stockmind.domain.errores import ErrorValidacion, StockMindError, TokenInvalido
from stockmind.domain.modelos import (
    ErrorCampo,
    MovimientoInventario,
    Producto,
    ResumenVentasProducto,
)
from stockmind.ports.inventario import InventarioConnector

OP_CREAR_PRODUCTO = "crear_producto"
OP_REGISTRAR_MOVIMIENTO = "registrar_movimiento"

_INSTRUCCION_CONFIRMAR = (
    "Muestra esta vista previa al usuario y pídele una confirmación explícita. "
    "Solo si acepta, llama a confirmar_operacion con este token; si no, llama a cancelar_operacion. "
    "El token vence en {minutos} minutos y sirve una sola vez."
)


def _limitar(valor: int, minimo: int, maximo: int) -> int:
    return max(minimo, min(int(valor), maximo))


class InventarioService:
    def __init__(
        self,
        conector: InventarioConnector,
        confirmaciones: GestorConfirmaciones,
        auditoria: Auditoria,
        *,
        origen: str = "mcp",
        hoy: Callable[[], date] = date.today,
    ):
        self.conector = conector
        self.confirmaciones = confirmaciones
        self.auditoria = auditoria
        self.origen = origen
        self.hoy = hoy

    # ------------------------------------------------------------- lectura

    def buscar_productos(self, texto: str = "", limite: int = 20) -> list[Producto]:
        return self.conector.buscar_productos(texto or "", _limitar(limite, 1, 100))

    def obtener_producto(self, id_o_codigo: str) -> Producto:
        return self.conector.obtener_producto(id_o_codigo)

    def listar_bajo_stock(self) -> list[Producto]:
        return self.conector.listar_bajo_stock()

    def listar_movimientos(
        self, producto: str, desde: date | None = None, hasta: date | None = None, limite: int = 100
    ) -> tuple[Producto, list[MovimientoInventario], int]:
        """Devuelve el producto, los últimos `limite` movimientos del rango y el total del rango."""
        _validar_rango(desde, hasta)
        p = self.conector.obtener_producto(producto)
        movimientos = self.conector.listar_movimientos(p.id, desde, hasta)
        return p, movimientos[-_limitar(limite, 1, 1000):], len(movimientos)

    def ventas_por_producto(
        self, desde: date | None = None, hasta: date | None = None, limite: int = 50
    ) -> list[ResumenVentasProducto]:
        _validar_rango(desde, hasta)
        return self.conector.ventas_por_producto(desde, hasta)[: _limitar(limite, 1, 1000)]

    def campos_producto(self) -> dict[str, Any]:
        esquema = esquema_producto(self.conector)
        campos = [c.model_dump(exclude_none=True) for c in esquema.values()]
        return {
            "conector": self.conector.nombre,
            "obligatorios": [c for c in campos if c["obligatorio"]],
            "opcionales": [c for c in campos if not c["obligatorio"]],
        }

    # ---------------------------------------------------------- escritura

    def preparar_crear_producto(self, datos: dict[str, Any]) -> dict[str, Any]:
        nuevo, advertencias = validar_producto(datos, self.conector)
        return self._emitir(OP_CREAR_PRODUCTO, nuevo.a_datos_planos(), nuevo.a_datos_planos(), advertencias)

    def preparar_movimiento(self, datos: dict[str, Any]) -> dict[str, Any]:
        movimiento, producto, advertencias = validar_movimiento(datos, self.conector, self.hoy())
        disponible = producto.stock_en(movimiento.almacen)
        vista = {
            "producto": {"id": producto.id, "codigo": producto.codigo, "nombre": producto.nombre,
                         "unidad": producto.unidad},
            **movimiento.model_dump(mode="json", exclude_none=True, exclude={"producto_id"}),
            "stock_actual": disponible,
            "stock_resultante": round(disponible + movimiento.delta, 4),
        }
        datos_op = movimiento.model_dump(mode="json", exclude_none=True)
        return self._emitir(OP_REGISTRAR_MOVIMIENTO, datos_op, vista, advertencias)

    def _emitir(self, tipo: str, datos_op: dict[str, Any], vista: dict[str, Any],
                advertencias: list[str]) -> dict[str, Any]:
        token, operacion = self.confirmaciones.emitir(tipo, datos_op)
        self._auditar("preparada", operacion, datos=datos_op)
        minutos = int(self.confirmaciones.ttl.total_seconds() // 60)
        return {
            "ok": True,
            "requiere_confirmacion": True,
            "operacion": tipo,
            "token": token,
            "vence": operacion.vence.astimezone().isoformat(timespec="seconds"),
            "vista_previa": vista,
            "advertencias": advertencias,
            "instruccion": _INSTRUCCION_CONFIRMAR.format(minutos=minutos),
        }

    def confirmar(self, token: str) -> dict[str, Any]:
        try:
            operacion = self.confirmaciones.consumir(token)
        except TokenInvalido as e:
            self._auditar_rechazo_token(token, e)
            raise

        try:
            resultado = self._ejecutar(operacion)
        except Exception as e:
            mensaje = e.mensaje if isinstance(e, StockMindError) else f"{type(e).__name__}: {e}"
            self._auditar("rechazada", operacion, datos=operacion.datos, error=mensaje)
            raise

        self._auditar("confirmada", operacion, datos=operacion.datos, resultado=resultado)
        return {"ok": True, "operacion": operacion.tipo, "resultado": resultado,
                "mensaje": "Operación guardada correctamente."}

    def _ejecutar(self, operacion: OperacionPendiente) -> dict[str, Any]:
        # Se vuelve a validar: entre la vista previa y la confirmación otro
        # proceso pudo cambiar el stock o crear un producto con el mismo nombre.
        if operacion.tipo == OP_CREAR_PRODUCTO:
            nuevo, _ = validar_producto(operacion.datos, self.conector)
            return self.conector.crear_producto(nuevo).model_dump(mode="json")
        if operacion.tipo == OP_REGISTRAR_MOVIMIENTO:
            movimiento, _, _ = validar_movimiento(operacion.datos, self.conector, self.hoy())
            return self.conector.registrar_movimiento(movimiento).model_dump(mode="json")
        raise StockMindError(f"Operación desconocida: {operacion.tipo}")

    def cancelar(self, token: str) -> dict[str, Any]:
        operacion = self.confirmaciones.cancelar(token)
        self._auditar("cancelada", operacion, datos=operacion.datos)
        return {"ok": True, "operacion": operacion.tipo, "mensaje": "Operación cancelada; no se guardó nada."}

    # ---------------------------------------------------------- auditoría

    def _auditar(self, evento: str, operacion: OperacionPendiente, **extra: Any) -> None:
        self.auditoria.registrar(evento, operacion.tipo, conector=self.conector.nombre, origen=self.origen,
                                 token_hash=operacion.token_hash, **extra)

    def _auditar_rechazo_token(self, token: Any, error: TokenInvalido) -> None:
        token_hash = hash_token(token) if isinstance(token, str) and token.strip() else None
        self.auditoria.registrar("rechazada", "confirmar_operacion", conector=self.conector.nombre,
                                 origen=self.origen, token_hash=token_hash, error=error.mensaje)


def _validar_rango(desde: date | None, hasta: date | None) -> None:
    if desde and hasta and desde > hasta:
        raise ErrorValidacion(invalidos=[ErrorCampo(campo="desde", mensaje="no puede ser posterior a 'hasta'")])
