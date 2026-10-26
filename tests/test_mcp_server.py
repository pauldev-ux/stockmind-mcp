"""Prueba las tools a través del protocolo MCP, con el cliente en el mismo proceso."""

import pytest
from mcp import Client

from stockmind.mcp_server.server import crear_servidor

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _llamar(client: Client, tool: str, argumentos: dict | None = None) -> dict:
    resultado = await client.call_tool(tool, argumentos or {})
    assert not resultado.is_error, resultado.content
    return resultado.structured_content


async def test_expone_las_tools_del_puerto(servicio):
    async with Client(crear_servidor(servicio)) as client:
        nombres = {t.name for t in (await client.list_tools()).tools}
    assert nombres == {
        "buscar_productos", "obtener_producto", "listar_bajo_stock", "listar_movimientos",
        "ventas_por_producto", "campos_obligatorios_producto", "crear_producto", "registrar_movimiento",
        "confirmar_operacion", "cancelar_operacion",
        "listar_pendientes", "exportar_pendientes", "verificar_pendientes", "marcar_registrada",
        "descartar_pendiente",
        "reporte_mas_vendidos", "reporte_bajo_stock", "reporte_sin_movimiento", "reporte_abc",
        "reporte_rotacion", "resumen_ejecutivo",
    }


async def test_flujo_de_confirmacion_por_mcp(servicio):
    async with Client(crear_servidor(servicio)) as client:
        vista = await _llamar(client, "registrar_movimiento",
                              {"producto": "ELE-001", "tipo": "entrada", "cantidad": 24})
        assert vista["ok"] and vista["vista_previa"]["stock_resultante"] == 24

        producto = await _llamar(client, "obtener_producto", {"id_o_codigo": "ELE-001"})
        assert producto["producto"]["stock_actual"] == 0   # todavía no se guardó

        confirmado = await _llamar(client, "confirmar_operacion", {"token": vista["token"]})
        assert confirmado["ok"] and confirmado["resultado"]["saldo"] == 24

        repetido = await _llamar(client, "confirmar_operacion", {"token": vista["token"]})
        assert repetido == {"ok": False, "error": "TokenInvalido", "mensaje": repetido["mensaje"]}


async def test_errores_de_validacion_llegan_en_espanol(servicio):
    async with Client(crear_servidor(servicio)) as client:
        respuesta = await _llamar(client, "crear_producto", {"datos": {"nombre": "Martillo"}})
        assert respuesta["ok"] is False
        assert {f["campo"] for f in respuesta["faltantes"]} == {"categoria", "unidad", "precio_venta", "costo"}

        fechas = await _llamar(client, "ventas_por_producto", {"desde": "ayer"})
        assert fechas["ok"] is False and "fecha" in fechas["invalidos"][0]["mensaje"]


async def test_lecturas(servicio):
    async with Client(crear_servidor(servicio)) as client:
        bajos = await _llamar(client, "listar_bajo_stock")
        assert [p["id"] for p in bajos["productos"]] == ["P0003", "P0002"]
        assert bajos["productos"][0]["bajo_stock"] is True

        kardex = await _llamar(client, "listar_movimientos", {"producto": "CON-001", "limite": 2})
        assert kardex["total_en_rango"] == 3 and kardex["mostrados"] == 2

        ventas = await _llamar(client, "ventas_por_producto", {"desde": "2026-10-01", "hasta": "2026-10-31"})
        assert ventas["productos"][0]["producto_id"] == "P0001"
