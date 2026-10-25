from datetime import date

import pandas as pd
import pytest

from stockmind.adapters.csv_excel import CsvExcelConnector, escribir_tabla, leer_tabla, ruta_tabla
from stockmind.domain.errores import (
    CodigoAmbiguo,
    ErrorAlmacenamiento,
    ErrorValidacion,
    ProductoNoEncontrado,
    StockInsuficiente,
)
from stockmind.domain.modelos import MovimientoInventario, ProductoNuevo, TipoMovimiento


# ------------------------------------------------------------------- lectura

def test_buscar_ignora_tildes_y_mayusculas(conector):
    assert [p.id for p in conector.buscar_productos("tuberia")] == ["P0002"]
    assert [p.id for p in conector.buscar_productos("CEMENTO")] == ["P0001"]


def test_buscar_por_varias_palabras_y_por_categoria(conector):
    assert [p.id for p in conector.buscar_productos("foco led")] == ["P0003"]
    assert [p.id for p in conector.buscar_productos("plomeria")] == ["P0002"]


def test_buscar_codigo_exacto_va_primero(conector):
    assert conector.buscar_productos("PLO-001")[0].id == "P0002"


def test_buscar_vacio_lista_todo_respetando_limite(conector):
    assert len(conector.buscar_productos("")) == 3
    assert len(conector.buscar_productos("", limite=2)) == 2


def test_obtener_por_id_y_por_codigo(conector):
    assert conector.obtener_producto("P0001").nombre.startswith("Cemento")
    p = conector.obtener_producto("plo-001")
    assert p.id == "P0002"
    assert p.stock_actual == 5
    assert p.precio_venta == 24


def test_obtener_inexistente(conector):
    with pytest.raises(ProductoNoEncontrado, match="XYZ"):
        conector.obtener_producto("XYZ")


def test_codigo_repetido_es_ambiguo(datos_dir, formato):
    # En Canaan360 el código no es único; el conector no debe elegir uno al azar.
    ruta = ruta_tabla(datos_dir, "productos", formato)
    df = leer_tabla(ruta, "productos")
    extra = df.iloc[[0]].assign(id="P0004", nombre="Cemento otra marca")
    escribir_tabla(pd.concat([df, extra]), ruta, "productos")
    with pytest.raises(CodigoAmbiguo) as e:
        CsvExcelConnector(datos_dir, formato).obtener_producto("CON-001")
    assert {c["id"] for c in e.value.candidatos} == {"P0001", "P0004"}


def test_bajo_stock_ordenado_por_criticidad(conector):
    assert [p.id for p in conector.listar_bajo_stock()] == ["P0003", "P0002"]


def test_movimientos_filtrados_por_fecha(conector):
    todos = conector.listar_movimientos("P0001")
    assert [m.id for m in todos] == ["M000001", "M000002", "M000003"]
    assert todos[1].tipo == TipoMovimiento.SALIDA and todos[1].delta == -15 and todos[1].saldo == 105
    rango = conector.listar_movimientos("P0001", desde=date(2026, 10, 5), hasta=date(2026, 10, 15))
    assert [m.id for m in rango] == ["M000002"]


def test_ventas_por_producto(conector):
    resumen = {r.producto_id: r for r in conector.ventas_por_producto()}
    assert resumen["P0001"].cantidad_total == 20
    assert resumen["P0001"].monto_total == 1200
    assert resumen["P0001"].numero_ventas == 2
    solo_dia_20 = conector.ventas_por_producto(desde=date(2026, 10, 20), hasta=date(2026, 10, 20))
    assert {r.producto_id: r.cantidad_total for r in solo_dia_20} == {"P0001": 5, "P0002": 2}


def test_tablas_inexistentes_se_leen_vacias(tmp_path):
    vacio = CsvExcelConnector(tmp_path / "nada")
    assert vacio.buscar_productos("") == []
    assert vacio.ventas_por_producto() == []


# ----------------------------------------------------------------- escritura

@pytest.fixture(params=["csv", "xlsx"])
def formato(request) -> str:
    """En este archivo los tests corren con los dos formatos."""
    return request.param


def _nuevo(**cambios) -> ProductoNuevo:
    datos = {"nombre": "Martillo de uña", "categoria": "Herramientas", "unidad": "unidad",
             "precio_venta": 65, "costo": 45} | cambios
    return ProductoNuevo(**datos)


def test_crear_producto_asigna_id_y_persiste(conector, datos_dir, formato):
    creado = conector.crear_producto(_nuevo(codigo="HER-001"))
    assert creado.id == "P0004"
    assert creado.stock_actual == 0
    releido = CsvExcelConnector(datos_dir, formato).obtener_producto("HER-001")
    assert releido.nombre == "Martillo de uña"
    assert releido.precio_venta == 65


def test_crear_producto_con_nombre_repetido_falla(conector):
    with pytest.raises(ErrorValidacion) as e:
        conector.crear_producto(_nuevo(nombre="cemento ip-30 BOLSA 50 kg"))
    assert e.value.invalidos[0].campo == "nombre"


def _movimiento(**cambios) -> MovimientoInventario:
    datos = {"producto_id": "P0002", "tipo": "salida", "cantidad": 3, "fecha": date(2026, 10, 25),
             "motivo": "Venta mostrador"} | cambios
    return MovimientoInventario(**datos)


def test_registrar_movimiento_actualiza_stock_y_saldo(conector, datos_dir, formato):
    registrado = conector.registrar_movimiento(_movimiento())
    assert registrado.id == "M000005"
    assert registrado.saldo == 2
    otro = CsvExcelConnector(datos_dir, formato)
    assert otro.obtener_producto("P0002").stock_actual == 2
    assert otro.listar_movimientos("P0002")[-1].saldo == 2


def test_ajuste_positivo_suma(conector):
    registrado = conector.registrar_movimiento(_movimiento(tipo="ajuste", sentido="+", cantidad=4.5))
    assert registrado.saldo == 9.5


def test_no_permite_stock_negativo_y_no_toca_archivos(conector, datos_dir, formato):
    antes = {t: ruta_tabla(datos_dir, t, formato).read_bytes() for t in ("productos", "movimientos")}
    with pytest.raises(StockInsuficiente):
        conector.registrar_movimiento(_movimiento(cantidad=6))
    for tabla, contenido in antes.items():
        assert ruta_tabla(datos_dir, tabla, formato).read_bytes() == contenido


def test_salida_exacta_deja_stock_en_cero(conector):
    assert conector.registrar_movimiento(_movimiento(cantidad=5)).saldo == 0


def test_archivo_bloqueado_da_error_claro(conector, monkeypatch):
    def bloqueado(*_):
        raise PermissionError("en uso")

    monkeypatch.setattr("stockmind.adapters.csv_excel.os.replace", bloqueado)
    with pytest.raises(ErrorAlmacenamiento, match="abierto en otro programa"):
        conector.registrar_movimiento(_movimiento())
