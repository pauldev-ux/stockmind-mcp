"""Modelo común de inventario, independiente de cualquier ERP.

Decisiones pensadas para que entren ERPs reales como Canaan360 sin atarse a ellos:
- Los ids son texto: sirven igual para BIGINT de MySQL que para códigos de un CSV.
- `codigo` es opcional y no se asume único (en Canaan360 no lo es).
- El stock es un total; el desglose por almacén y los lotes son opcionales.
- La cantidad de un movimiento siempre es positiva; el signo lo da el tipo
  (o `sentido`, en los ajustes), igual que en un kardex.
"""

from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TipoMovimiento(StrEnum):
    ENTRADA = "entrada"
    SALIDA = "salida"
    AJUSTE = "ajuste"


class Sentido(StrEnum):
    POSITIVO = "+"
    NEGATIVO = "-"


class Lote(BaseModel):
    codigo: str
    # Número visible para el usuario (lote del proveedor o n.º de documento); puede repetirse.
    numero: str | None = None
    producto_id: str
    almacen: str | None = None
    cantidad: float
    fecha_vencimiento: date | None = None
    fecha_ingreso: date | None = None
    costo_unitario: float | None = None


class Producto(BaseModel):
    id: str
    codigo: str | None = None
    nombre: str
    categoria: str | None = None
    unidad: str | None = None
    precio_venta: float | None = None
    costo: float | None = None
    stock_actual: float = 0
    stock_minimo: float = 0
    proveedor: str | None = None
    almacen: str | None = None
    activo: bool = True
    # Desglose opcional {almacén: cantidad} para ERPs con varios almacenes.
    stock_por_almacen: dict[str, float] | None = None
    # Lotes con stock disponible, en orden FEFO, solo cuando el ERP los maneja.
    lotes: list[Lote] | None = None
    # Datos propios del ERP que no tienen lugar en el modelo común
    # (p. ej. tipo o moneda en Canaan360).
    atributos: dict[str, Any] = Field(default_factory=dict)

    @property
    def bajo_stock(self) -> bool:
        return self.stock_actual < self.stock_minimo

    def stock_en(self, almacen: str | None) -> float:
        if almacen and self.stock_por_almacen is not None:
            return self.stock_por_almacen.get(almacen, 0.0)
        return self.stock_actual


class ProductoNuevo(BaseModel):
    """Datos para crear un producto. Qué es obligatorio lo decide cada conector."""

    codigo: str | None = None
    nombre: str
    categoria: str | None = None
    unidad: str | None = None
    precio_venta: float | None = None
    costo: float | None = None
    stock_minimo: float = 0
    proveedor: str | None = None
    almacen: str | None = None
    atributos: dict[str, Any] = Field(default_factory=dict)

    def a_datos_planos(self) -> dict[str, Any]:
        """Campos comunes y atributos del ERP en un solo nivel, como los escribe el usuario."""
        datos = self.model_dump(mode="json", exclude={"atributos"}, exclude_none=True)
        datos.update(self.atributos)
        return datos


class MovimientoInventario(BaseModel):
    id: str | None = None
    producto_id: str
    tipo: TipoMovimiento
    # Obligatorio en ajustes; en entradas y salidas se deduce del tipo.
    sentido: Sentido | None = None
    cantidad: float = Field(gt=0)
    fecha: date
    motivo: str | None = None
    # Saldo del producto después del movimiento (como saldo_cantidad del kardex).
    saldo: float | None = None
    almacen: str | None = None
    lote: str | None = None
    fecha_vencimiento: date | None = None
    costo_unitario: float | None = Field(default=None, ge=0)
    # Documento de origen: COM-…, VEN-…, AJU-…
    documento: str | None = None
    # Datos propios del ERP (p. ej. el tipo_movimiento original del kardex).
    atributos: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _completar_sentido(self) -> "MovimientoInventario":
        implicito = {TipoMovimiento.ENTRADA: Sentido.POSITIVO, TipoMovimiento.SALIDA: Sentido.NEGATIVO}
        if self.tipo == TipoMovimiento.AJUSTE:
            if self.sentido is None:
                raise ValueError("un ajuste necesita sentido '+' o '-'")
        elif self.sentido is None:
            self.sentido = implicito[self.tipo]
        elif self.sentido != implicito[self.tipo]:
            raise ValueError(f"una {self.tipo.value} no puede tener sentido '{self.sentido.value}'")
        return self

    @property
    def delta(self) -> float:
        """Cantidad con signo: lo que el movimiento suma o resta al stock."""
        return self.cantidad if self.sentido == Sentido.POSITIVO else -self.cantidad


class DetalleVenta(BaseModel):
    venta_id: str | None = None
    producto_id: str
    cantidad: float = Field(gt=0)
    precio_unitario: float = Field(ge=0)
    subtotal: float = Field(ge=0)


class Venta(BaseModel):
    id: str
    fecha: date
    cliente: str | None = None
    almacen: str | None = None
    total: float = Field(ge=0)
    detalles: list[DetalleVenta] = Field(default_factory=list)


class ResumenVentasProducto(BaseModel):
    producto_id: str
    codigo: str | None = None
    nombre: str
    cantidad_total: float
    monto_total: float
    numero_ventas: int


class CampoRequerido(BaseModel):
    """Describe un campo de producto para que la IA sepa qué pedirle al usuario."""

    model_config = ConfigDict(frozen=True)

    nombre: str
    tipo: Literal["texto", "numero", "entero", "fecha", "opcion"]
    descripcion: str
    obligatorio: bool = True
    opciones: tuple[str, ...] | None = None
    minimo: float | None = None
    max_largo: int | None = None


class ErrorCampo(BaseModel):
    campo: str
    mensaje: str
