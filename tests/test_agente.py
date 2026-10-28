"""Agente cliente MCP con un LLM falso (sin llamadas reales) contra el servidor MCP real en proceso."""

import json
import re
from pathlib import Path

import pytest

from stockmind.adapters.csv_excel import CsvExcelConnector
from stockmind.agente.agente import HERRAMIENTAS_SOLO_USUARIO, Agente, ErrorAgente
from stockmind.agente.cliente_mcp import ClienteMCP
from stockmind.agente.historial import HistorialStore
from stockmind.agente.modelos import BloqueResultado
from stockmind.agente.proveedores.falso import Paso, ProveedorFalso
from stockmind.mcp_server.server import crear_servidor

pytestmark = pytest.mark.anyio

MOVIMIENTO = {"producto": "PLO-001", "tipo": "salida", "cantidad": 2, "motivo": "Venta mostrador"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def historial(tmp_path) -> HistorialStore:
    return HistorialStore(tmp_path / "chat.db")


@pytest.fixture
def servidor(servicio):
    return crear_servidor(servicio)


async def _conversar(agente, servidor, texto="hola", conversacion_id=None, usuario="ana"):
    async with ClienteMCP.en_proceso(servidor) as mcp:
        eventos = [e async for e in agente.conversar(mcp, usuario, texto, conversacion_id)]
    return eventos, mcp


def _de_tipo(eventos, tipo):
    return [e["datos"] for e in eventos if e["evento"] == tipo]


async def test_conversacion_simple_guarda_historial_y_uso(historial, servidor):
    agente = Agente(ProveedorFalso([Paso(texto="Hola, soy StockMind.")]), historial)
    eventos, _ = await _conversar(agente, servidor)
    assert [e["evento"] for e in eventos][0] == "conversacion" and eventos[-1]["evento"] == "fin"
    assert "".join(d["texto"] for d in _de_tipo(eventos, "texto")).strip() == "Hola, soy StockMind."
    conv = eventos[0]["datos"]["id"]
    assert [m.rol for m in historial.mensajes(conv)] == ["usuario", "asistente"]
    assert historial.uso(conv)[0]["llamadas"] == 1 and _de_tipo(eventos, "uso")[0]["proveedor"] == "falso"


async def test_usa_tools_mcp_y_le_pasa_el_resultado_al_modelo(historial, servidor):
    falso = ProveedorFalso([Paso(llamadas=[("listar_bajo_stock", {})]), Paso(texto="Hay 2 productos bajo el mínimo.")])
    eventos, mcp = await _conversar(Agente(falso, historial), servidor, "¿qué está bajo stock?")
    assert [d["nombre"] for d in _de_tipo(eventos, "tool_inicio")] == ["listar_bajo_stock"]
    assert _de_tipo(eventos, "tool_fin")[0]["ok"] is True
    resultado = falso.llamadas[1]["mensajes"][-1].bloques[0]
    assert isinstance(resultado, BloqueResultado) and "P0003" in resultado.contenido   # dato real del CSV, vía MCP
    assert mcp.llamadas == [("listar_bajo_stock", {})]


async def test_herramientas_solo_del_usuario_nunca_llegan_al_modelo(historial, servidor):
    falso = ProveedorFalso([Paso(texto="ok")])
    await _conversar(Agente(falso, historial), servidor)
    ofrecidas = set(falso.llamadas[0]["herramientas"])
    assert ofrecidas.isdisjoint(HERRAMIENTAS_SOLO_USUARIO)
    assert {"registrar_movimiento", "crear_producto", "reporte_abc"} <= ofrecidas


async def test_vista_previa_se_vuelve_accion_pendiente_y_el_modelo_no_ve_el_token(historial, servidor, datos_dir):
    falso = ProveedorFalso([Paso(llamadas=[("registrar_movimiento", MOVIMIENTO)]),
                            Paso(texto="Te muestro la vista previa; confírmala con el botón.")])
    eventos, _ = await _conversar(Agente(falso, historial), servidor, "registra una venta de 2 tubos")
    (accion,) = _de_tipo(eventos, "accion_pendiente")
    token = accion["token"]
    assert accion["operacion"] == "registrar_movimiento" and "Salida de 2" in accion["resumen"]
    assert accion["vista_previa"]["stock_resultante"] == 3
    # El turno termina con la vista previa: no hay otra llamada al modelo en este mensaje.
    assert len(falso.llamadas) == 1 and eventos[-1]["evento"] == "fin"
    assert eventos[-1]["datos"]["acciones_pendientes"] == 1
    assert CsvExcelConnector(datos_dir).obtener_producto("P0002").stock_actual == 5   # nada se guardó
    # Lo que verá el modelo en el siguiente turno: el resultado SIN token.
    await _conversar(Agente(falso, historial), servidor, "¿y ahora?", eventos[0]["datos"]["id"])
    visto_por_el_modelo = json.dumps([m.model_dump() for m in falso.llamadas[1]["mensajes"]], ensure_ascii=False)
    assert token not in visto_por_el_modelo and "accion_id" in visto_por_el_modelo


async def test_el_token_no_vence_aunque_el_modelo_tarde_en_responder(historial, servidor, reloj, datos_dir):
    """Caso real (28/10/2026): Gemini saturado tardó 10 min en la llamada que seguía a la vista previa;
    la consola preguntaba «¿Confirmar?» recién al final y el token (10 min) ya había vencido."""
    def modelo_lento(mensajes, herramientas):
        if len(falso.llamadas) == 1:
            return Paso(llamadas=[("registrar_movimiento", MOVIMIENTO)])
        reloj.avanzar(minutes=10, seconds=6)          # lo que tardó Gemini en contestar
        return Paso(texto="He preparado la operación.")

    falso = ProveedorFalso(modelo_lento)
    agente = Agente(falso, historial)
    eventos, _ = await _conversar(agente, servidor)
    conv, token = eventos[0]["datos"]["id"], _de_tipo(eventos, "accion_pendiente")[0]["token"]
    assert len(falso.llamadas) == 1                 # no se espera al modelo: el usuario decide ya
    reloj.avanzar(seconds=20)                       # el usuario lee la vista previa y confirma
    async with ClienteMCP.en_proceso(servidor) as mcp:
        hecho = await agente.confirmar(mcp, "ana", conv, token)
    assert hecho["estado"] == "confirmada", hecho
    assert CsvExcelConnector(datos_dir).obtener_producto("P0002").stock_actual == 3


async def test_el_modelo_no_puede_confirmar_operaciones(historial, servidor, servicio, datos_dir, auditoria):
    # Aunque conociera un token válido (aquí se lo damos a propósito), la llamada se rechaza sin ejecutarse.
    token = servicio.preparar_movimiento({"producto_id": "P0002", "tipo": "salida", "cantidad": 2,
                                          "motivo": "x"})["token"]
    falso = ProveedorFalso([Paso(llamadas=[("confirmar_operacion", {"token": token}),
                                           ("marcar_registrada", {"id": "SM-000001"})]),
                            Paso(texto="No puedo confirmar; usa el botón.")])
    eventos, mcp = await _conversar(Agente(falso, historial), servidor)
    rechazos = [d for d in _de_tipo(eventos, "tool_fin") if not d["ok"]]
    assert len(rechazos) == 2 and "solo la puede ejecutar el usuario" in rechazos[0]["resumen"]
    assert mcp.llamadas == []                                   # nada llegó al servidor MCP
    assert CsvExcelConnector(datos_dir).obtener_producto("P0002").stock_actual == 5
    assert "confirmada" not in [r["evento"] for r in auditoria.leer()]
    servicio.confirmar(token)                                   # el token sigue sin usar: el usuario sí puede


async def test_confirmacion_del_usuario(historial, servidor, datos_dir):
    falso = ProveedorFalso([Paso(llamadas=[("registrar_movimiento", MOVIMIENTO)]), Paso(texto="Confírmalo.")])
    agente = Agente(falso, historial)
    eventos, _ = await _conversar(agente, servidor)
    conv, token = eventos[0]["datos"]["id"], _de_tipo(eventos, "accion_pendiente")[0]["token"]
    async with ClienteMCP.en_proceso(servidor) as mcp:
        with pytest.raises(ErrorAgente, match="ya se confirmó"):
            await agente.confirmar(mcp, "ana", conv, "token-inventado")
        with pytest.raises(ErrorAgente, match="No existe esa conversación"):
            await agente.confirmar(mcp, "pedro", conv, token)   # la conversación es de ana
        hecho = await agente.confirmar(mcp, "ana", conv, token)
        assert hecho["ok"] and hecho["estado"] == "confirmada"
        assert mcp.llamadas == [("confirmar_operacion", {"token": token})]
        with pytest.raises(ErrorAgente):
            await agente.confirmar(mcp, "ana", conv, token)      # una sola vez
    assert CsvExcelConnector(datos_dir).obtener_producto("P0002").stock_actual == 3
    nota = historial.mensajes(conv)[-1]
    assert nota.nota_sistema and "El usuario confirmó" in nota.texto and token not in nota.texto
    assert historial.acciones(conv)[0].estado == "confirmada"


async def test_cancelacion_del_usuario(historial, servidor, datos_dir):
    falso = ProveedorFalso([Paso(llamadas=[("registrar_movimiento", MOVIMIENTO)]), Paso(texto="Ok.")])
    agente = Agente(falso, historial)
    eventos, _ = await _conversar(agente, servidor)
    conv, token = eventos[0]["datos"]["id"], _de_tipo(eventos, "accion_pendiente")[0]["token"]
    async with ClienteMCP.en_proceso(servidor) as mcp:
        assert (await agente.cancelar(mcp, "ana", conv, token))["estado"] == "cancelada"
    assert CsvExcelConnector(datos_dir).obtener_producto("P0002").stock_actual == 5
    assert "canceló" in historial.mensajes(conv)[-1].texto


async def test_siguiente_turno_incluye_la_nota_del_sistema(historial, servidor):
    falso = ProveedorFalso([Paso(llamadas=[("registrar_movimiento", MOVIMIENTO)]), Paso(texto="Confírmalo."),
                            Paso(texto="Listo, quedó registrado.")])
    agente = Agente(falso, historial)
    eventos, _ = await _conversar(agente, servidor)
    conv, token = eventos[0]["datos"]["id"], _de_tipo(eventos, "accion_pendiente")[0]["token"]
    async with ClienteMCP.en_proceso(servidor) as mcp:
        await agente.confirmar(mcp, "ana", conv, token)
    await _conversar(agente, servidor, "¿se guardó?", conv)
    ultimos = falso.llamadas[-1]["mensajes"][-2:]
    assert ultimos[0].nota_sistema and ultimos[1].texto == "¿se guardó?"


async def test_limite_de_iteraciones(historial, servidor):
    siempre_tool = ProveedorFalso(lambda mensajes, herramientas: Paso(llamadas=[("listar_bajo_stock", {})]))
    eventos, _ = await _conversar(Agente(siempre_tool, historial, max_iteraciones=3), servidor)
    assert len(_de_tipo(eventos, "tool_inicio")) == 3 and len(siempre_tool.llamadas) == 3
    assert _de_tipo(eventos, "error")[0]["error"] == "LimiteIteraciones"
    # El historial queda válido: cada llamada tiene su resultado.
    conv = eventos[0]["datos"]["id"]
    assert historial.mensajes(conv)[-1].rol == "usuario"


async def test_rechazo_del_proveedor_y_tool_desconocida(historial, servidor):
    falso = ProveedorFalso([Paso(llamadas=[("borrar_todo", {})]), Paso(texto="", motivo="rechazo")])
    eventos, mcp = await _conversar(Agente(falso, historial), servidor)
    assert "No existe la herramienta" in _de_tipo(eventos, "tool_fin")[0]["resumen"] and mcp.llamadas == []
    assert _de_tipo(eventos, "error")[0]["error"] == "RespuestaRechazada"


async def test_conversacion_ajena_no_existe(historial, servidor):
    eventos, _ = await _conversar(Agente(ProveedorFalso([Paso(texto="a")]), historial), servidor, usuario="ana")
    with pytest.raises(ErrorAgente, match="No existe esa conversación"):
        await _conversar(Agente(ProveedorFalso([Paso(texto="b")]), historial), servidor,
                         conversacion_id=eventos[0]["datos"]["id"], usuario="pedro")


def test_el_agente_y_la_api_solo_usan_mcp():
    raiz = Path(__file__).parents[1] / "src" / "stockmind"
    prohibido = re.compile(r"^\s*(from|import)\s+stockmind\.(application|adapters|composicion|domain|ports)\b", re.M)
    for carpeta in ("agente", "api"):
        for archivo in (raiz / carpeta).rglob("*.py"):
            assert not prohibido.search(archivo.read_text(encoding="utf-8")), archivo
