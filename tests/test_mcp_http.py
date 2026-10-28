"""El servidor MCP por streamable HTTP de verdad (uvicorn en un hilo), protegido con MCP_TOKEN."""

import socket
import threading
import time

import httpx2
import pytest
import uvicorn

from stockmind.adapters.csv_excel import CsvExcelConnector
from stockmind.agente.agente import Agente
from stockmind.agente.cliente_mcp import ClienteMCP, ErrorMCP
from stockmind.agente.historial import HistorialStore
from stockmind.agente.proveedores.falso import Paso, ProveedorFalso
from stockmind.mcp_server.server import crear_app_http, crear_servidor

TOKEN = "mcp-token-de-prueba"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def url_mcp(servicio):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto = s.getsockname()[1]
    servidor = uvicorn.Server(uvicorn.Config(crear_app_http(crear_servidor(servicio), TOKEN), host="127.0.0.1",
                                             port=puerto, log_level="warning"))
    hilo = threading.Thread(target=servidor.run, daemon=True)
    hilo.start()
    for _ in range(100):
        if servidor.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{puerto}/mcp"
    servidor.should_exit = True
    hilo.join(timeout=5)


def test_sin_token_responde_401(url_mcp):
    r = httpx2.post(url_mcp, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401 and r.json()["error"] == "NoAutorizado"
    r = httpx2.post(url_mcp, json={}, headers={"Authorization": "Bearer otro"})
    assert r.status_code == 401


@pytest.mark.anyio
async def test_cliente_con_token(url_mcp):
    async with ClienteMCP.conectar_http(url_mcp, TOKEN) as mcp:
        nombres = {h.nombre for h in await mcp.herramientas()}
        assert {"confirmar_operacion", "reporte_abc"} <= nombres
        resultado = await mcp.llamar("obtener_producto", {"id_o_codigo": "CON-001"})
        assert resultado.ok and resultado.datos["producto"]["stock_actual"] == 100


@pytest.mark.anyio
async def test_token_incorrecto_da_error_claro(url_mcp):
    with pytest.raises(ErrorMCP, match="MCP_TOKEN"):
        async with ClienteMCP.conectar_http(url_mcp, "token-equivocado") as mcp:
            await mcp.herramientas()


@pytest.mark.anyio
async def test_servidor_apagado_da_error_claro():
    with pytest.raises(ErrorMCP, match="No se pudo conectar"):
        async with ClienteMCP.conectar_http("http://127.0.0.1:9/mcp", TOKEN):
            pass


@pytest.mark.anyio
async def test_agente_de_punta_a_punta_por_http(url_mcp, tmp_path, datos_dir):
    falso = ProveedorFalso([Paso(llamadas=[("registrar_movimiento", {"producto": "PLO-001", "tipo": "entrada",
                                                                     "cantidad": 4})]),
                            Paso(texto="Confírmalo con el botón.")])
    agente = Agente(falso, HistorialStore(tmp_path / "chat.db"))
    async with ClienteMCP.conectar_http(url_mcp, TOKEN) as mcp:
        eventos = [e async for e in agente.conversar(mcp, "ana", "entraron 4 tubos")]
        accion = next(e["datos"] for e in eventos if e["evento"] == "accion_pendiente")
        resultado = await agente.confirmar(mcp, "ana", eventos[0]["datos"]["id"], accion["token"])
    assert resultado["estado"] == "confirmada"
    assert CsvExcelConnector(datos_dir).obtener_producto("P0002").stock_actual == 9


def test_la_api_abre_una_sola_conexion_mcp_para_todas_las_peticiones(url_mcp, tmp_path):
    from fastapi.testclient import TestClient

    from stockmind.api.app import crear_app
    from stockmind.api.config import ApiSettings

    config = ApiSettings(api_token="a" * 32, mcp_token=TOKEN, mcp_url=url_mcp, confirmaciones_db=tmp_path / "c.db")
    app = crear_app(config, proveedor=ProveedorFalso([Paso(texto="Hola.")] * 3))
    cabecera = {"Authorization": "Bearer " + "a" * 32}
    with TestClient(app) as cliente:
        for _ in range(3):
            assert cliente.get("/api/v1/salud", headers=cabecera).status_code == 200
        assert cliente.post("/api/v1/chat", json={"mensaje": "hola"}, headers=cabecera).status_code == 200
        assert app.state.conexion_mcp.aperturas == 1
