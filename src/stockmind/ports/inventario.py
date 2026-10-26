"""Puerto: lo que StockMind necesita de cualquier ERP.

Cada adaptador (CSV/Excel, conectores para ERPs reales…) implementa esta interfaz. Los adaptadores
solo hacen E/S y traducen al modelo común; la validación, la confirmación y la
auditoría viven en `application/` y no se repiten en cada adaptador.
"""

from abc import ABC, abstractmethod
from datetime import date

from stockmind.domain.bandeja import OperacionBandeja
from stockmind.domain.modelos import (
    CampoRequerido,
    ErrorCampo,
    ModoEscritura,
    MovimientoInventario,
    Producto,
    ProductoNuevo,
    ResumenVentasProducto,
    Revision,
)


class InventarioConnector(ABC):
    #: Nombre corto del conector, se usa en la auditoría.
    nombre: str = "desconocido"
    #: Nombre para mostrar en los mensajes al usuario.
    nombre_visible: str = "el ERP"
    #: DIRECTA escribe en el ERP al confirmar; BANDEJA deja la operación pendiente
    #: en la bandeja de salida; NINGUNA rechaza las escrituras antes de validar.
    modo_escritura: ModoEscritura = ModoEscritura.DIRECTA

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

    def advertencias_producto_nuevo(self, datos: ProductoNuevo) -> list[str]:
        """Avisos que no bloquean (p. ej. cómo guardará el ERP el nombre). Opcional."""
        return []

    def validar_movimiento_nuevo(
        self, movimiento: MovimientoInventario, producto: Producto, pendientes: list[OperacionBandeja]
    ) -> Revision:
        """Reglas propias del ERP para un movimiento (almacén, lote…). Opcional.

        `pendientes` son los movimientos del mismo producto que siguen en la
        bandeja: su stock ya está comprometido aunque el ERP todavía no lo sepa.
        """
        return Revision()
