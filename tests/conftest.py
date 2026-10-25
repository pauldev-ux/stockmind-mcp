from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from stockmind.adapters.csv_excel import CsvExcelConnector, escribir_tabla, ruta_tabla
from stockmind.application.auditoria import Auditoria
from stockmind.application.confirmaciones import GestorConfirmaciones, MemoriaConfirmacionStore
from stockmind.application.servicio import InventarioService

HOY = date(2026, 10, 25)

PRODUCTOS = [
    # id,   codigo,   nombre,                       categoria,      unidad,  precio, costo, stock, min, proveedor
    ("P0001", "CON-001", "Cemento IP-30 bolsa 50 kg", "Construcción", "bolsa", "60", "52", "100", "20", "Andina"),
    ("P0002", "PLO-001", "Tubería PVC 1/2\"", "Plomería", "pieza", "24", "18", "5", "10", "Oriente"),
    ("P0003", "ELE-001", "Foco LED 9 W", "Electricidad", "unidad", "15", "9", "0", "12", "Oriente"),
]


class RelojFalso:
    """Reloj controlable para probar vencimientos sin esperar."""

    def __init__(self, inicio: datetime):
        self.ahora = inicio

    def __call__(self) -> datetime:
        return self.ahora

    def avanzar(self, **kwargs: float) -> None:
        self.ahora += timedelta(**kwargs)


def escribir_datos_prueba(carpeta: Path, formato: str = "csv") -> None:
    columnas = ["id", "codigo", "nombre", "categoria", "unidad", "precio_venta", "costo", "stock_actual",
                "stock_minimo", "proveedor"]
    productos = pd.DataFrame(PRODUCTOS, columns=columnas).assign(almacen="Principal", activo="1")
    movimientos = pd.DataFrame([
        ("M000001", "P0001", "entrada", "+", "120", "2026-10-01", "Saldo inicial", "120"),
        ("M000002", "P0001", "salida", "-", "15", "2026-10-10", "Venta VEN-000001", "105"),
        ("M000003", "P0001", "salida", "-", "5", "2026-10-20", "Venta VEN-000002", "100"),
        ("M000004", "P0002", "entrada", "+", "5", "2026-10-01", "Saldo inicial", "5"),
    ], columns=["id", "producto_id", "tipo", "sentido", "cantidad", "fecha", "motivo", "saldo"])
    ventas = pd.DataFrame([
        ("VEN-000001", "2026-10-10", "150"),
        ("VEN-000002", "2026-10-20", "348"),
    ], columns=["id", "fecha", "total"])
    detalle = pd.DataFrame([
        ("VEN-000001", "P0001", "15", "60", "900"),
        ("VEN-000002", "P0001", "5", "60", "300"),
        ("VEN-000002", "P0002", "2", "24", "48"),
    ], columns=["venta_id", "producto_id", "cantidad", "precio_unitario", "subtotal"])
    for nombre, df in [("productos", productos), ("movimientos", movimientos), ("ventas", ventas),
                       ("ventas_detalle", detalle)]:
        escribir_tabla(df, ruta_tabla(carpeta, nombre, formato), nombre)


@pytest.fixture(params=["csv"])
def formato(request) -> str:
    return request.param


@pytest.fixture
def datos_dir(tmp_path: Path, formato: str) -> Path:
    carpeta = tmp_path / "data"
    escribir_datos_prueba(carpeta, formato)
    return carpeta


@pytest.fixture
def conector(datos_dir: Path, formato: str) -> CsvExcelConnector:
    return CsvExcelConnector(datos_dir, formato)


@pytest.fixture
def reloj() -> RelojFalso:
    return RelojFalso(datetime(2026, 10, 25, 14, 0, tzinfo=UTC))


@pytest.fixture
def auditoria(tmp_path: Path, reloj: RelojFalso) -> Auditoria:
    return Auditoria(tmp_path / "logs" / "auditoria.jsonl", reloj)


@pytest.fixture
def servicio(conector: CsvExcelConnector, reloj: RelojFalso, auditoria: Auditoria) -> InventarioService:
    return InventarioService(
        conector,
        GestorConfirmaciones(MemoriaConfirmacionStore(), ttl=timedelta(minutes=10), reloj=reloj),
        auditoria,
        origen="test",
        hoy=lambda: HOY,
    )
