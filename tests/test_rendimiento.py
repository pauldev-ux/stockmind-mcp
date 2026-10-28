"""Menos tokens y menos espera: recorte de resultados, esquemas compactos, ventana del historial,
streaming real, medición de tiempos y conexión MCP persistente. Sin red."""

import asyncio
import json

import pytest

from stockmind.agente.agente import (
    Agente,
    compactar_esquema,
    recortar_para_modelo,
    ventana_historial,
)
from stockmind.agente.cliente_mcp import ClienteMCP, ConexionPersistente
from stockmind.agente.historial import HistorialStore
from stockmind.agente.llm import LLMProvider
from stockmind.agente.modelos import (
    BloqueLlamada,
    BloqueResultado,
    BloqueTexto,
    EventoTexto,
    Mensaje,
    RespuestaLLM,
    Uso,
)
from stockmind.agente.proveedores.falso import Paso, ProveedorFalso
from stockmind.mcp_server.server import crear_servidor


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def servidor(servicio):
    return crear_servidor(servicio)


# ------------------------------------------------------- resultados recortados

def test_recorta_listas_largas_y_conserva_totales():
    datos = {"ok": True, "total": 40, "productos": [{"id": i} for i in range(40)],
             "resumen": {"clases": [{"c": c} for c in "ABCDEFG"], "monto": 10}, "avisos": ["x"]}
    corto, recortado = recortar_para_modelo(datos, 5)
    assert recortado and corto["productos"] == [{"id": i} for i in range(5)] and corto["productos_total"] == 40
    assert corto["resumen"]["clases_total"] == 7 and corto["resumen"]["monto"] == 10
    assert corto["total"] == 40 and corto["avisos"] == ["x"]
    assert recortar_para_modelo(datos, 0) == (datos, False)            # 0 = sin límite
    assert recortar_para_modelo({"a": [1, 2]}, 5) == ({"a": [1, 2]}, False)


@pytest.mark.anyio
async def test_el_modelo_recibe_el_resultado_recortado_y_el_panel_el_completo(servidor, tmp_path):
    falso = ProveedorFalso([Paso(llamadas=[("buscar_productos", {"texto": ""})]), Paso(texto="Listo.")])
    agente = Agente(falso, HistorialStore(tmp_path / "chat.db"), max_filas_resultado=1)
    async with ClienteMCP.en_proceso(servidor) as mcp:
        eventos = [e async for e in agente.conversar(mcp, "ana", "lista todo")]
    (fin,) = [e["datos"] for e in eventos if e["evento"] == "tool_fin"]
    assert len(fin["datos"]["productos"]) == fin["datos"]["total"] > 1    # completo para el panel
    resultado = falso.llamadas[1]["mensajes"][-1].bloques[0]
    visto = json.loads(resultado.contenido)
    assert len(visto["productos"]) == 1 and visto["productos_total"] == fin["datos"]["total"]
    assert "primeras 1 filas" in visto["nota_recorte"]


# ------------------------------------------------------------ esquemas compactos

@pytest.mark.anyio
async def test_esquemas_compactos_de_todas_las_tools(servidor):
    async with ClienteMCP.en_proceso(servidor) as mcp:
        herramientas = await mcp.herramientas()
    antes = sum(len(json.dumps(h.esquema)) for h in herramientas)
    despues = 0
    for h in herramientas:
        compacto = compactar_esquema(h.esquema)
        despues += len(json.dumps(compacto))
        texto = json.dumps(compacto)
        assert '"title"' not in texto and '{"type": "null"}' not in texto, h.nombre
        assert set(compacto.get("properties", {})) == set(h.esquema.get("properties", {})), h.nombre
        assert compacto.get("required", []) == h.esquema.get("required", []), h.nombre
    assert despues < antes * 0.6
    movimiento = next(compactar_esquema(h.esquema) for h in herramientas if h.nombre == "registrar_movimiento")
    assert movimiento["properties"]["tipo"] == {"type": "string"}
    assert movimiento["properties"]["cantidad"] == {"anyOf": [{"type": "number"}, {"type": "string"}]}


def test_compactar_no_toca_una_propiedad_llamada_title():
    esquema = {"type": "object", "title": "X", "properties": {"title": {"type": "string", "title": "Title"}}}
    assert compactar_esquema(esquema) == {"type": "object", "properties": {"title": {"type": "string"}}}


# ------------------------------------------------------------ ventana del historial

def _turno(n: int, con_tool: bool = False) -> list[Mensaje]:
    mensajes = [Mensaje(rol="usuario", bloques=[BloqueTexto(texto=f"pregunta {n}")])]
    if con_tool:
        mensajes += [Mensaje(rol="asistente", bloques=[BloqueLlamada(id=f"t{n}", nombre="x")],
                             crudo={"proveedor": "anthropic", "contenido": [{"type": "thinking"}]}),
                     Mensaje(rol="usuario", bloques=[BloqueResultado(id=f"t{n}", nombre="x", contenido="{}")])]
    mensajes.append(Mensaje(rol="asistente", bloques=[BloqueTexto(texto=f"respuesta {n}")],
                            crudo={"proveedor": "anthropic", "contenido": [{"type": "text"}]}))
    return mensajes


def test_ventana_ultimos_turnos_mas_resumen():
    mensajes = [m for n in range(1, 6) for m in _turno(n, con_tool=n % 2 == 0)]
    mensajes.insert(4, Mensaje(rol="usuario", nota_sistema=True,
                               bloques=[BloqueTexto(texto="[Nota del sistema] El usuario confirmó X.")]))
    ventana = ventana_historial(mensajes, 2)
    resumen, *recientes = ventana
    assert resumen.nota_sistema and resumen.rol == "usuario"
    assert "pregunta 1" in resumen.texto and "respuesta 3" in resumen.texto and "confirmó X" in resumen.texto
    assert "pregunta 4" not in resumen.texto
    assert recientes[0].texto == "pregunta 4"                       # empieza en una pregunta, sin cortar tools
    assert [m.texto for m in recientes if m.rol == "usuario" and m.texto] == ["pregunta 4", "pregunta 5"]
    assert all(m.crudo for m in recientes if m.rol == "asistente")    # Gemini: crudo intacto
    sin_crudo = ventana_historial(mensajes, 2, sin_crudo_previo=True)
    assert all(m.crudo is None for m in sin_crudo if m.rol == "asistente")   # Anthropic: prefijo nuevo
    assert ventana_historial(mensajes, 10) == mensajes                # sin recorte: igual (caché intacta)
    assert ventana_historial(mensajes, 0) == mensajes


@pytest.mark.anyio
async def test_el_agente_envia_solo_la_ventana(servidor, tmp_path):
    falso = ProveedorFalso([Paso(texto=f"r{i}") for i in range(4)])
    agente = Agente(falso, HistorialStore(tmp_path / "chat.db"), historial_turnos=2)
    conversacion = None
    async with ClienteMCP.en_proceso(servidor) as mcp:
        for i in range(4):
            eventos = [e async for e in agente.conversar(mcp, "ana", f"p{i}", conversacion)]
            conversacion = eventos[0]["datos"]["id"]
    enviados = falso.llamadas[-1]["mensajes"]
    assert enviados[0].nota_sistema and "p0" in enviados[0].texto and "p1" in enviados[0].texto
    assert [m.texto for m in enviados[1:]] == ["p2", "r2", "p3"]
    assert len(agente.historial.mensajes(conversacion)) == 8          # el historial completo sigue guardado


# ------------------------------------------------------- streaming y tiempos

class ProveedorPausado(LLMProvider):
    """Emite un fragmento y espera a que el test confirme que ya llegó al consumidor."""
    nombre, modelo = "pausado", "p-1"

    def __init__(self):
        self.seguir = asyncio.Event()

    async def responder(self, sistema, mensajes, herramientas):
        yield EventoTexto(texto="Hola ")
        await self.seguir.wait()
        yield EventoTexto(texto="Ana")
        yield RespuestaLLM(mensaje=Mensaje(rol="asistente", bloques=[BloqueTexto(texto="Hola Ana")]),
                           uso=Uso(proveedor="pausado", modelo="p-1"), motivo="fin")


@pytest.mark.anyio
async def test_el_texto_llega_mientras_el_modelo_sigue_escribiendo(servidor, tmp_path):
    proveedor = ProveedorPausado()
    agente = Agente(proveedor, HistorialStore(tmp_path / "chat.db"))
    async with ClienteMCP.en_proceso(servidor) as mcp:
        flujo = agente.conversar(mcp, "ana", "hola")
        tipos = []
        async for e in flujo:
            tipos.append(e["evento"])
            if e["evento"] == "texto":
                assert e["datos"]["texto"] == "Hola " and not proveedor.seguir.is_set()
                proveedor.seguir.set()                              # recién ahora "sigue" el modelo
                break
        tipos += [e["evento"] async for e in flujo]
    assert tipos.count("texto") == 2 and tipos[-1] == "fin"


@pytest.mark.anyio
async def test_medir_tiempos(servidor, tmp_path):
    falso = ProveedorFalso([Paso(llamadas=[("obtener_producto", {"id_o_codigo": "CON-001"})]), Paso(texto="Ok.")])
    agente = Agente(falso, HistorialStore(tmp_path / "chat.db"), medir_tiempos=True)
    async with ClienteMCP.en_proceso(servidor) as mcp:
        eventos = [e async for e in agente.conversar(mcp, "ana", "stock")]
    tiempos = [e["datos"] for e in eventos if e["evento"] == "tiempo"]
    assert [t["etapa"] for t in tiempos] == ["tools_list", "modelo", "tool", "modelo", "mensaje"]
    assert all(t["total"] >= 0 for t in tiempos) and tiempos[2]["nombre"] == "obtener_producto"
    assert tiempos[1]["primer_token"] is not None and tiempos[1]["entrada"] > 0
    sin = Agente(ProveedorFalso([Paso(texto="x")]), HistorialStore(tmp_path / "otro.db"))
    async with ClienteMCP.en_proceso(servidor) as mcp:
        assert "tiempo" not in [e["evento"] async for e in sin.conversar(mcp, "ana", "x")]


# ------------------------------------------------------- conexión MCP persistente

@pytest.mark.anyio
async def test_conexion_persistente_se_abre_una_vez_y_se_reabre_si_se_rompe(servidor):
    conexion = ConexionPersistente(lambda: ClienteMCP.en_proceso(servidor))
    for _ in range(3):
        async with conexion() as mcp:
            assert (await mcp.llamar("obtener_producto", {"id_o_codigo": "CON-001"})).ok
    assert conexion.aperturas == 1
    primera = await conexion.obtener()
    primera.roto = True                                             # p. ej. el servidor MCP se reinició
    async with conexion() as mcp:
        assert mcp is not primera and (await mcp.llamar("obtener_producto", {"id_o_codigo": "CON-001"})).ok
    assert conexion.aperturas == 2
    await conexion.cerrar()
    await conexion.cerrar()                                         # idempotente


def test_la_api_reutiliza_la_conexion_mcp():
    from stockmind.api.app import crear_app
    from stockmind.api.config import ApiSettings

    app = crear_app(ApiSettings(api_token="x" * 32, mcp_token="t"), proveedor=ProveedorFalso())
    assert isinstance(app.state.conexion_mcp, ConexionPersistente)
