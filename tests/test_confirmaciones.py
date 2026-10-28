import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

from stockmind.adapters.csv_excel import CsvExcelConnector
from stockmind.application.confirmaciones import (
    GestorConfirmaciones,
    MemoriaConfirmacionStore,
    SqliteConfirmacionStore,
    hash_token,
    hora_local,
)
from stockmind.domain.errores import ErrorValidacion, TokenInvalido

DATOS = {"producto_id": "P0001", "tipo": "entrada", "cantidad": 5}


@pytest.fixture(params=["memoria", "sqlite"])
def store(request, tmp_path):
    if request.param == "memoria":
        return MemoriaConfirmacionStore()
    return SqliteConfirmacionStore(tmp_path / "con espacios" / "stockmind.db")


@pytest.fixture
def gestor(store, reloj) -> GestorConfirmaciones:
    return GestorConfirmaciones(store, ttl=timedelta(minutes=10), reloj=reloj)


# ------------------------------------------------------- gestor de tokens

def test_emitir_y_consumir(gestor):
    token, emitida = gestor.emitir("registrar_movimiento", DATOS)
    assert emitida.vence - emitida.creada == timedelta(minutes=10)
    operacion = gestor.consumir(token)
    assert operacion.tipo == "registrar_movimiento"
    assert operacion.datos == DATOS


def test_token_de_un_solo_uso(gestor):
    token, _ = gestor.emitir("registrar_movimiento", DATOS)
    gestor.consumir(token)
    with pytest.raises(TokenInvalido, match="una sola vez"):
        gestor.consumir(token)


def test_token_vence_a_los_10_minutos(gestor, reloj):
    token_a, _ = gestor.emitir("registrar_movimiento", DATOS)
    token_b, _ = gestor.emitir("registrar_movimiento", DATOS)
    reloj.avanzar(minutes=9, seconds=59)
    gestor.consumir(token_a)
    reloj.avanzar(seconds=1)
    with pytest.raises(TokenInvalido, match="venció"):
        gestor.consumir(token_b)


def test_token_inexistente_o_vacio(gestor):
    with pytest.raises(TokenInvalido, match="no existe"):
        gestor.consumir("inventado")
    with pytest.raises(TokenInvalido, match="Falta"):
        gestor.consumir("  ")


def test_token_cancelado_no_se_puede_confirmar(gestor):
    token, _ = gestor.emitir("crear_producto", {"nombre": "X"})
    gestor.cancelar(token)
    with pytest.raises(TokenInvalido, match="cancelada"):
        gestor.consumir(token)


def test_solo_se_guarda_el_hash(gestor, store):
    token, emitida = gestor.emitir("registrar_movimiento", DATOS)
    assert emitida.token_hash == hash_token(token) != token
    assert store.obtener(token) is None
    assert store.obtener(hash_token(token)) is not None


def test_sqlite_no_contiene_el_token_en_claro(tmp_path, reloj):
    ruta = tmp_path / "stockmind.db"
    token, _ = GestorConfirmaciones(SqliteConfirmacionStore(ruta), reloj=reloj).emitir("crear_producto", {})
    with sqlite3.connect(ruta) as con:
        filas = con.execute("SELECT * FROM confirmaciones").fetchall()
    assert len(filas) == 1
    assert token not in json.dumps(filas)


def test_sqlite_compartido_entre_procesos(tmp_path, reloj):
    # Dos instancias sobre el mismo archivo simulan el servidor MCP y el backend web.
    ruta = tmp_path / "stockmind.db"
    mcp = GestorConfirmaciones(SqliteConfirmacionStore(ruta), reloj=reloj)
    web = GestorConfirmaciones(SqliteConfirmacionStore(ruta), reloj=reloj)
    token, _ = mcp.emitir("registrar_movimiento", DATOS)
    assert web.consumir(token).datos == DATOS
    with pytest.raises(TokenInvalido, match="una sola vez"):
        mcp.consumir(token)


def test_purga_vencidas_viejas(gestor, store, reloj):
    token, _ = gestor.emitir("crear_producto", {})
    reloj.avanzar(days=2)
    gestor.emitir("crear_producto", {})   # emitir purga lo vencido hace más de un día
    assert store.obtener(hash_token(token)) is None


# ------------------------------------------------- flujo completo (servicio)

PRODUCTO = {"nombre": "Martillo de uña", "categoria": "Herramientas", "unidad": "unidad",
            "precio_venta": 65, "costo": 45}


def test_crear_producto_no_guarda_hasta_confirmar(servicio, datos_dir):
    vista = servicio.preparar_crear_producto(PRODUCTO)
    assert vista["requiere_confirmacion"] is True
    assert vista["vista_previa"]["nombre"] == "Martillo de uña"
    assert "confirmar_operacion" in vista["instruccion"]
    assert CsvExcelConnector(datos_dir).buscar_productos("martillo") == []

    resultado = servicio.confirmar(vista["token"])
    assert resultado["ok"] is True
    assert resultado["resultado"]["id"] == "P0004"
    assert CsvExcelConnector(datos_dir).buscar_productos("martillo")[0].id == "P0004"


def test_movimiento_confirmado_actualiza_stock(servicio, datos_dir):
    vista = servicio.preparar_movimiento({"producto_id": "PLO-001", "tipo": "salida", "cantidad": 2,
                                          "motivo": "Venta mostrador"})
    assert vista["vista_previa"]["stock_actual"] == 5
    assert vista["vista_previa"]["stock_resultante"] == 3
    assert CsvExcelConnector(datos_dir).obtener_producto("P0002").stock_actual == 5
    servicio.confirmar(vista["token"])
    assert CsvExcelConnector(datos_dir).obtener_producto("P0002").stock_actual == 3


def test_datos_invalidos_no_emiten_token(servicio, auditoria):
    with pytest.raises(ErrorValidacion):
        servicio.preparar_movimiento({"producto_id": "P0002", "tipo": "salida", "cantidad": 99, "motivo": "x"})
    assert auditoria.leer() == []


def test_confirmar_revalida_el_stock(servicio, datos_dir, auditoria):
    # Dos salidas preparadas que juntas superan el stock: la segunda debe fallar al confirmar.
    primera = servicio.preparar_movimiento({"producto_id": "P0002", "tipo": "salida", "cantidad": 4, "motivo": "a"})
    segunda = servicio.preparar_movimiento({"producto_id": "P0002", "tipo": "salida", "cantidad": 4, "motivo": "b"})
    servicio.confirmar(primera["token"])
    with pytest.raises(ErrorValidacion, match="stock insuficiente"):
        servicio.confirmar(segunda["token"])
    assert CsvExcelConnector(datos_dir).obtener_producto("P0002").stock_actual == 1
    # El token rechazado queda gastado: hay que preparar de nuevo y ver una vista previa nueva.
    with pytest.raises(TokenInvalido):
        servicio.confirmar(segunda["token"])
    assert [r["evento"] for r in auditoria.leer()][-2:] == ["rechazada", "rechazada"]


def test_token_vencido_en_el_servicio(servicio, reloj, datos_dir):
    vista = servicio.preparar_movimiento({"producto_id": "P0003", "tipo": "entrada", "cantidad": 24})
    reloj.avanzar(minutes=10)
    with pytest.raises(TokenInvalido, match="venció"):
        servicio.confirmar(vista["token"])
    assert CsvExcelConnector(datos_dir).obtener_producto("P0003").stock_actual == 0


def test_auditoria_registra_cada_paso_sin_el_token(servicio, auditoria):
    vista = servicio.preparar_movimiento({"producto_id": "P0003", "tipo": "entrada", "cantidad": 24})
    servicio.confirmar(vista["token"])
    cancelada = servicio.preparar_crear_producto(PRODUCTO)
    servicio.cancelar(cancelada["token"])

    registros = auditoria.leer()
    assert [(r["evento"], r["operacion"]) for r in registros] == [
        ("preparada", "registrar_movimiento"),
        ("confirmada", "registrar_movimiento"),
        ("preparada", "crear_producto"),
        ("cancelada", "crear_producto"),
    ]
    confirmada = registros[1]
    assert confirmada["origen"] == "test" and confirmada["conector"] == "csv"
    assert confirmada["resultado"]["saldo"] == 24
    assert confirmada["token"] == hash_token(vista["token"])[:12]
    assert vista["token"] not in auditoria.ruta.read_text(encoding="utf-8")


LA_PAZ = timezone(timedelta(hours=-4))


def test_horas_en_utc_aunque_los_procesos_usen_otra_zona(tmp_path):
    """Un proceso emite con un reloj en UTC-4 (La Paz) y otro confirma con uno en UTC: es el mismo instante."""
    ruta = tmp_path / "stockmind.db"
    emitido = datetime(2026, 10, 28, 14, 35, 5, tzinfo=LA_PAZ)         # = 18:35:05 UTC
    token, op = GestorConfirmaciones(SqliteConfirmacionStore(ruta), reloj=lambda: emitido).emitir("t", DATOS)
    fila = sqlite3.connect(ruta).execute("SELECT creada, vence FROM confirmaciones").fetchone()
    assert fila == ("2026-10-28T18:35:05.000000+00:00", "2026-10-28T18:45:05.000000+00:00")   # UTC explícito

    otro_proceso = GestorConfirmaciones(SqliteConfirmacionStore(ruta),
                                        reloj=lambda: datetime(2026, 10, 28, 18, 44, 0, tzinfo=UTC))
    assert otro_proceso.consumir(token).vence == datetime(2026, 10, 28, 18, 45, 5, tzinfo=UTC)

    token2, _ = GestorConfirmaciones(SqliteConfirmacionStore(ruta), reloj=lambda: emitido).emitir("t", DATOS)
    tarde = GestorConfirmaciones(SqliteConfirmacionStore(ruta),
                                 reloj=lambda: datetime(2026, 10, 28, 18, 45, 6, tzinfo=UTC))
    with pytest.raises(TokenInvalido) as info:
        tarde.consumir(token2)
    assert f"venció a las {hora_local(op.vence)}" in str(info.value)    # al usuario, en su hora local


def test_hora_local_muestra_la_zona_y_rechaza_datetimes_sin_zona(store):
    # hora_local usa la zona del sistema; se comprueba contra la conversión de Python.
    instante = datetime(2026, 10, 28, 18, 45, tzinfo=UTC)
    esperado = instante.astimezone()
    assert hora_local(instante).startswith(f"{esperado:%H:%M} (UTC")
    with pytest.raises(ValueError, match="con zona horaria"):
        hora_local(datetime(2026, 10, 28, 14, 45))
    # Un reloj sin zona se rechaza en vez de interpretarse como hora local en silencio.
    with pytest.raises(ValueError, match="con zona horaria"):
        GestorConfirmaciones(store, reloj=lambda: datetime(2026, 10, 28, 14, 35)).emitir("t", DATOS)
