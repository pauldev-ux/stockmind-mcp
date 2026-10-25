from datetime import date

import pytest

from stockmind.application.validacion import validar_movimiento, validar_producto
from stockmind.domain.errores import ErrorValidacion
from stockmind.domain.modelos import TipoMovimiento
from tests.conftest import HOY

PRODUCTO_OK = {"nombre": "Martillo de uña", "categoria": "Herramientas", "unidad": "unidad",
               "precio_venta": 65, "costo": 45}


def _error(funcion, *args) -> ErrorValidacion:
    with pytest.raises(ErrorValidacion) as e:
        funcion(*args)
    return e.value


def _campos(lista) -> set[str]:
    return {getattr(x, "nombre", None) or x.campo for x in lista}


# ----------------------------------------------------------------- producto

def test_producto_vacio_lista_los_obligatorios_del_conector(conector):
    error = _error(validar_producto, {}, conector)
    assert _campos(error.faltantes) == {"nombre", "categoria", "unidad", "precio_venta", "costo"}
    assert "Faltan datos" in error.mensaje
    respuesta = error.a_dict()
    assert respuesta["ok"] is False
    assert respuesta["faltantes"][0]["descripcion"]


def test_producto_valido(conector):
    nuevo, advertencias = validar_producto(PRODUCTO_OK, conector)
    assert nuevo.nombre == "Martillo de uña"
    assert nuevo.stock_minimo == 0
    assert advertencias == []


def test_acepta_coma_decimal_y_textos_con_espacios(conector):
    nuevo, _ = validar_producto(PRODUCTO_OK | {"precio_venta": "65,50", "nombre": "  Martillo  "}, conector)
    assert nuevo.precio_venta == 65.5
    assert nuevo.nombre == "Martillo"


def test_numeros_invalidos_y_negativos(conector):
    error = _error(validar_producto, PRODUCTO_OK | {"precio_venta": "caro", "costo": -1}, conector)
    mensajes = {e.campo: e.mensaje for e in error.invalidos}
    assert "debe ser un número" in mensajes["precio_venta"]
    assert "mayor o igual a 0" in mensajes["costo"]
    assert error.faltantes == []   # un campo inválido no se repite como faltante


def test_campo_vacio_cuenta_como_faltante(conector):
    error = _error(validar_producto, PRODUCTO_OK | {"categoria": "   "}, conector)
    assert _campos(error.faltantes) == {"categoria"}


def test_rechaza_campos_desconocidos_en_vez_de_ignorarlos(conector):
    error = _error(validar_producto, PRODUCTO_OK | {"color": "rojo"}, conector)
    assert error.invalidos[0].campo == "color"
    assert "no reconocido" in error.invalidos[0].mensaje


def test_no_se_puede_crear_con_stock(conector):
    error = _error(validar_producto, PRODUCTO_OK | {"stock_actual": 10}, conector)
    assert "registrar_movimiento" in error.invalidos[0].mensaje


def test_nombre_repetido_lo_detecta_el_conector(conector):
    error = _error(validar_producto, PRODUCTO_OK | {"nombre": "Foco LED 9 W"}, conector)
    assert error.invalidos[0].campo == "nombre"


def test_advierte_precio_menor_que_costo(conector):
    _, advertencias = validar_producto(PRODUCTO_OK | {"precio_venta": 40}, conector)
    assert "menor que el costo" in advertencias[0]


# --------------------------------------------------------------- movimiento

def test_salida_valida(conector):
    mov, producto, advertencias = validar_movimiento(
        {"producto_id": "CON-001", "tipo": "SALIDA", "cantidad": "10", "motivo": "Venta"}, conector, HOY)
    assert producto.id == "P0001"
    assert mov.producto_id == "P0001"      # se guarda el id, no el código
    assert mov.tipo == TipoMovimiento.SALIDA and mov.delta == -10
    assert mov.fecha == HOY
    assert advertencias == []


def test_movimiento_vacio(conector):
    error = _error(validar_movimiento, {}, conector, HOY)
    assert _campos(error.faltantes) == {"producto_id", "tipo", "cantidad"}


def test_ajuste_exige_sentido_y_motivo(conector):
    error = _error(validar_movimiento, {"producto_id": "P0001", "tipo": "ajuste", "cantidad": 1}, conector, HOY)
    assert _campos(error.faltantes) == {"sentido", "motivo"}


def test_salida_exige_motivo(conector):
    error = _error(validar_movimiento, {"producto_id": "P0001", "tipo": "salida", "cantidad": 1}, conector, HOY)
    assert _campos(error.faltantes) == {"motivo"}


def test_entrada_no_exige_motivo(conector):
    mov, _, _ = validar_movimiento({"producto_id": "P0003", "tipo": "entrada", "cantidad": 24}, conector, HOY)
    assert mov.delta == 24


def test_sentido_acepta_palabras(conector):
    mov, _, _ = validar_movimiento({"producto_id": "P0001", "tipo": "ajuste", "cantidad": 1,
                                    "sentido": "negativo", "motivo": "Merma"}, conector, HOY)
    assert mov.delta == -1


def test_entrada_con_sentido_negativo_es_invalida(conector):
    error = _error(validar_movimiento, {"producto_id": "P0001", "tipo": "entrada", "cantidad": 1,
                                        "sentido": "-"}, conector, HOY)
    assert "ajuste" in error.invalidos[0].mensaje


@pytest.mark.parametrize("cantidad", [0, -3, "cero"])
def test_cantidad_debe_ser_positiva(conector, cantidad):
    error = _error(validar_movimiento, {"producto_id": "P0001", "tipo": "entrada", "cantidad": cantidad},
                   conector, HOY)
    assert error.invalidos[0].campo == "cantidad"


def test_fecha_futura_invalida_y_formato_boliviano(conector):
    error = _error(validar_movimiento, {"producto_id": "P0001", "tipo": "entrada", "cantidad": 1,
                                        "fecha": "2026-12-01"}, conector, HOY)
    assert "futura" in error.invalidos[0].mensaje
    mov, _, _ = validar_movimiento({"producto_id": "P0001", "tipo": "entrada", "cantidad": 1,
                                    "fecha": "20/10/2026"}, conector, HOY)
    assert mov.fecha == date(2026, 10, 20)


def test_producto_inexistente(conector):
    error = _error(validar_movimiento, {"producto_id": "NO-EXISTE", "tipo": "entrada", "cantidad": 1},
                   conector, HOY)
    assert error.invalidos[0].campo == "producto_id"


def test_no_permite_stock_negativo(conector):
    error = _error(validar_movimiento, {"producto_id": "PLO-001", "tipo": "salida", "cantidad": 6,
                                        "motivo": "Venta"}, conector, HOY)
    assert "hay 5 pieza" in error.invalidos[0].mensaje
    assert "negativo" in error.invalidos[0].mensaje


def test_advierte_si_queda_bajo_el_minimo(conector):
    _, _, advertencias = validar_movimiento({"producto_id": "P0001", "tipo": "salida", "cantidad": 85,
                                             "motivo": "Venta"}, conector, HOY)
    assert "por debajo del mínimo" in advertencias[0]
