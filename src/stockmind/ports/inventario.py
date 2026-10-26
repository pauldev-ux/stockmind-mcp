"""Puerto: lo que StockMind necesita de cualquier ERP.

Cada adaptador (CSV/Excel, conectores para ERPs reales…) implementa esta interfaz. Los adaptadores
solo hacen E/S y traducen al modelo común; la validación, la confirmación y la
auditoría viven en `application/` y no se repiten en cada adaptador.
"""

from abc import ABC, abstractmethod
from datetime import date

from stockmind.domain.modelos import (
    CampoRequerido,
    ErrorCampo,
    MovimientoInventario,
    Producto,
    ProductoNuevo,
    ResumenVentasProducto,
)


class InventarioConnector(ABC):
    #: Nombre corto del conector, se usa en la auditoría.
    nombre: str = "desconocido"
    #: Nombre para mostrar en los mensajes al usuario.
    nombre_visible: str = "el ERP"
    #: False en conectores de solo lectura: el servicio rechaza las escrituras
    #: antes de validar o emitir un token.
    admite_escritura: bool = True

    @abstractmethod
    def buscar_productos(self, texto: str, limite: int = 20) -> list[Producto]:
        """Busca por código, nombre, categoría o proveedor. Texto vacío devuelve todos."""

    @abstractmethod
    def obtener_producto(self, id_o_codigo: str) -> Producto:
        """Busca por id y, si no hay, por código.

        Lanza ProductoNoEncontrado, o CodigoAmbiguo si el código se repite.
        """

    @abstractmethod
    def listar_bajo_stock(self) -> list[Producto]:
        """Productos activos con stock_actual < stock_minimo."""

    @abstractmethod
    def listar_movimientos(
        self, producto_id: str, desde: date | None = None, hasta: date | None = None
    ) -> list[MovimientoInventario]:
        """Movimientos de un producto en orden cronológico; las fechas son inclusivas."""

    @abstractmethod
    def ventas_por_producto(
        self, desde: date | None = None, hasta: date | None = None
    ) -> list[ResumenVentasProducto]:
        """Cantidad y monto vendidos por producto en el rango (inclusivo)."""

    @abstractmethod
    def crear_producto(self, datos: ProductoNuevo) -> Producto:
        """Guarda un producto ya validado. Crear un producto no crea stock."""

    @abstractmethod
    def registrar_movimiento(self, datos: MovimientoInventario) -> MovimientoInventario:
        """Guarda el movimiento, actualiza el stock y devuelve el movimiento con id y saldo.

        Debe lanzar StockInsuficiente si el stock quedaría negativo, aunque la
        capa de aplicación ya lo haya validado (otro proceso pudo escribir antes).
        """

    @abstractmethod
    def campos_obligatorios_producto(self) -> list[CampoRequerido]:
        """Campos que este ERP exige para crear un producto."""

    def validar_producto_nuevo(self, datos: ProductoNuevo) -> list[ErrorCampo]:
        """Reglas propias del ERP que requieren consultar sus datos (p. ej. nombre único).

        Opcional: por defecto no agrega errores.
        """
        return []
