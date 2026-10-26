"""Modelos de los reportes de inventario.

Cada reporte trae, además de sus filas, `metodo` (cómo se calculó, con los
números usados) y `avisos` (p. ej. "no hay ventas en el período"), para que la
IA pueda justificar sus respuestas. Si faltan datos para un cálculo, el valor
queda en None y `sin_datos` en True: nunca un cero engañoso.
"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Periodo(BaseModel):
    desde: date
    hasta: date
    dias: int


class Reporte(BaseModel):
    tipo: str
    titulo: str
    periodo: Periodo | None = None
    filtros: dict[str, Any] = Field(default_factory=dict)
    generado_en: datetime
    metodo: list[str] = Field(default_factory=list)
    avisos: list[str] = Field(default_factory=list)
    sin_datos: bool = False


# ------------------------------------------------------------- más vendidos

class FilaVendido(BaseModel):
    producto_id: str
    codigo: str | None = None
    nombre: str
    categoria: str | None = None
    cantidad: float
    monto: float
    participacion_cantidad: float          # % del total de unidades del período
    participacion_monto: float             # % del total vendido del período
    numero_ventas: int


class ReporteMasVendidos(Reporte):
    total_unidades: float = 0
    total_monto: float = 0
    productos_con_ventas: int = 0
    por_cantidad: list[FilaVendido] = Field(default_factory=list)
    por_monto: list[FilaVendido] = Field(default_factory=list)


# ---------------------------------------------------------------- bajo stock

class FilaBajoStock(BaseModel):
    producto_id: str
    codigo: str | None = None
    nombre: str
    categoria: str | None = None
    unidad: str | None = None
    stock_actual: float
    stock_minimo: float
    vendido_periodo: float | None          # unidades vendidas en la ventana de ventas
    venta_diaria: float | None             # None si no hay datos de ventas
    cobertura_dias: float | None           # None si no se vende (cobertura indefinida)
    objetivo: float                        # máx(stock mínimo, venta diaria × días objetivo)
    sugerido_reponer: float
    motivo: Literal["bajo_minimo", "cobertura_baja"]


class ReporteBajoStock(Reporte):
    dias_ventas: int
    dias_objetivo: int
    productos: list[FilaBajoStock] = Field(default_factory=list)


# ------------------------------------------------------------- sin movimiento

class FilaSinMovimiento(BaseModel):
    producto_id: str
    codigo: str | None = None
    nombre: str
    categoria: str | None = None
    stock: float
    costo: float | None
    moneda: str
    capital_inmovilizado: float | None     # stock × costo; None si no hay costo
    ultima_salida: date | None             # None: nunca tuvo salidas registradas
    dias_sin_movimiento: int | None


class ReporteSinMovimiento(Reporte):
    dias: int
    capital_por_moneda: dict[str, float] = Field(default_factory=dict)
    productos: list[FilaSinMovimiento] = Field(default_factory=list)


# ------------------------------------------------------------------------ ABC

class FilaABC(BaseModel):
    producto_id: str
    codigo: str | None = None
    nombre: str
    categoria: str | None = None
    monto: float
    porcentaje: float
    porcentaje_acumulado: float
    clase: Literal["A", "B", "C"]


class ResumenClase(BaseModel):
    clase: Literal["A", "B", "C"]
    productos: int
    monto: float
    porcentaje: float


class ProductoSinVentas(BaseModel):
    producto_id: str
    codigo: str | None = None
    nombre: str
    categoria: str | None = None


class ReporteABC(Reporte):
    porcentaje_a: float
    porcentaje_b: float
    total_monto: float = 0
    resumen: list[ResumenClase] = Field(default_factory=list)
    productos: list[FilaABC] = Field(default_factory=list)
    sin_ventas: list[ProductoSinVentas] = Field(default_factory=list)


# ------------------------------------------------- rotación y valorización

class FilaValorizacion(BaseModel):
    grupo: str                             # categoría, o "TOTAL"
    moneda: str
    productos: int
    unidades: float
    valor_costo: float
    valor_venta: float
    margen_potencial: float                # valor_venta - valor_costo


class FilaRotacion(BaseModel):
    grupo: str
    moneda: str
    unidades_vendidas: float
    otras_salidas: float                   # salidas que no son ventas: producción, mermas, transferencias…
    costo_vendido: float
    inventario_inicial: float              # al costo, al inicio del período
    inventario_final: float                # al costo, al final del período
    inventario_promedio: float
    rotacion: float | None                 # veces en el período; None sin ventas o sin inventario
    dias_inventario: float | None


class ReporteRotacion(Reporte):
    valorizacion_total: list[FilaValorizacion] = Field(default_factory=list)
    valorizacion: list[FilaValorizacion] = Field(default_factory=list)
    rotacion_total: list[FilaRotacion] = Field(default_factory=list)
    rotacion: list[FilaRotacion] = Field(default_factory=list)


# --------------------------------------------------------- resumen ejecutivo

class ResumenEjecutivo(Reporte):
    hallazgos: list[str] = Field(default_factory=list)
    ventas: dict[str, Any] = Field(default_factory=dict)
    top_por_monto: list[FilaVendido] = Field(default_factory=list)
    bajo_stock: list[FilaBajoStock] = Field(default_factory=list)
    productos_bajo_stock: int = 0
    productos_sin_movimiento: int = 0
    capital_inmovilizado: dict[str, float] = Field(default_factory=dict)
    abc: list[ResumenClase] = Field(default_factory=list)
    valorizacion: list[FilaValorizacion] = Field(default_factory=list)
    rotacion: list[FilaRotacion] = Field(default_factory=list)
