"""El generador debe ser reproducible y producir un kardex coherente con las ventas."""

import importlib.util
from datetime import date
from pathlib import Path

import pytest

from stockmind.adapters.csv_excel import CsvExcelConnector

RAIZ = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def generador():
    spec = importlib.util.spec_from_file_location("generar_datos", RAIZ / "scripts" / "generar_datos.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture(scope="module")
def generado(generador, tmp_path_factory):
    carpeta = tmp_path_factory.mktemp("datos")
    resumen = generador.generar(carpeta, dias=60, inicio=date(2026, 1, 1))
    return carpeta, resumen


def test_kardex_coherente_con_stock_y_ventas(generado):
    carpeta, resumen = generado
    conector = CsvExcelConnector(carpeta)
    productos = conector.buscar_productos("", limite=100)
    assert len(productos) == resumen["productos"] == 40
    vendido = {r.producto_id: r.cantidad_total for r in conector.ventas_por_producto()}
    for p in productos:
        movimientos = conector.listar_movimientos(p.id)
        assert all(m.saldo >= 0 for m in movimientos)
        assert sum(m.delta for m in movimientos) == pytest.approx(p.stock_actual)
        assert movimientos[-1].saldo == pytest.approx(p.stock_actual)
        salidas = sum(m.cantidad for m in movimientos if m.tipo == "salida")
        assert salidas == pytest.approx(vendido.get(p.id, 0))


def test_es_reproducible(generador, generado, tmp_path):
    carpeta, _ = generado
    generador.generar(tmp_path, dias=60, inicio=date(2026, 1, 1))
    for tabla in ("productos", "movimientos", "ventas", "ventas_detalle"):
        assert (tmp_path / f"{tabla}.csv").read_bytes() == (carpeta / f"{tabla}.csv").read_bytes()


def test_sabado_vende_mas_que_domingo(generado):
    carpeta, _ = generado
    import pandas as pd

    ventas = pd.read_csv(carpeta / "ventas.csv", encoding="utf-8-sig", parse_dates=["fecha"])
    por_dia = ventas.groupby(ventas["fecha"].dt.day_of_week)["total"].sum()
    assert por_dia[5] > 2 * por_dia[6]
