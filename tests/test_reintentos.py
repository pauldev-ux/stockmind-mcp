"""Reintentos con espera exponencial y modelos de respaldo (ConReintentos), sin red ni esperas reales."""

import httpx2
import pytest
from anthropic import APIStatusError, OverloadedError
from google.genai import errors

from stockmind.agente.agente import Agente
from stockmind.agente.cliente_mcp import ClienteMCP
from stockmind.agente.historial import HistorialStore
from stockmind.agente.llm import ConReintentos, ErrorLLM
from stockmind.agente.modelos import EventoReintento, EventoTexto, RespuestaLLM
from stockmind.agente.proveedores import crear_proveedor, lista_respaldo
from stockmind.agente.proveedores.claude import ProveedorAnthropic
from stockmind.agente.proveedores.falso import Paso, ProveedorFalso
from stockmind.agente.proveedores.gemini import RESPALDO_POR_DEFECTO, ProveedorGemini, espera_sugerida
from stockmind.mcp_server.server import crear_servidor

SATURADO = ErrorLLM("El modelo está saturado.", saturado=True)
LIMITE = ErrorLLM("Límite de uso.", reintentable=True)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ProveedorQueFalla(ProveedorFalso):
    """Lanza los errores de la lista (uno por llamada) y después responde como ProveedorFalso."""

    def __init__(self, modelo: str, errores: list[Exception], texto_antes: str = ""):
        super().__init__([Paso(texto=f"respondo con {modelo}")] * 10, modelo=modelo)
        self.errores, self.texto_antes, self.intentos = list(errores), texto_antes, 0

    async def responder(self, sistema, mensajes, herramientas):
        self.intentos += 1
        if self.errores:
            if self.texto_antes:
                yield EventoTexto(texto=self.texto_antes)
            raise self.errores.pop(0)
        async for parte in super().responder(sistema, mensajes, herramientas):
            yield parte


class Reloj:
    def __init__(self):
        self.esperas: list[float] = []

    async def __call__(self, segundos: float) -> None:
        self.esperas.append(segundos)


async def _correr(proveedor) -> list:
    return [p async for p in proveedor.responder("s", [], [])]


@pytest.mark.anyio
async def test_sin_respaldo_reintenta_con_espera_exponencial():
    principal, reloj = ProveedorQueFalla("m1", [SATURADO, SATURADO]), Reloj()
    partes = await _correr(ConReintentos([principal], dormir=reloj))
    reintentos = [p for p in partes if isinstance(p, EventoReintento)]
    assert [(r.intento, r.espera, r.cambio_de_modelo) for r in reintentos] == [(1, 2, False), (2, 4, False)]
    assert reloj.esperas == [2, 4] and principal.intentos == 3
    assert isinstance(partes[-1], RespuestaLLM) and partes[-1].mensaje.texto == "respondo con m1"
    reloj = Reloj()
    with pytest.raises(ErrorLLM, match=r"Se agotaron los reintentos \(modelos probados: m1\)"):
        await _correr(ConReintentos([ProveedorQueFalla("m1", [SATURADO] * 9)], dormir=reloj))
    assert reloj.esperas == [2, 4, 8]                          # 3 reintentos: 2 s, 4 s, 8 s


@pytest.mark.anyio
async def test_saturado_pasa_directo_al_respaldo_y_queda_en_enfriamiento():
    tiempo = [0.0]
    principal = ProveedorQueFalla("m1", [SATURADO] * 9)
    respaldo = ProveedorQueFalla("m2", [])
    reloj = Reloj()
    proveedor = ConReintentos([principal, respaldo], dormir=reloj, enfriamiento=300, reloj=lambda: tiempo[0])
    partes = await _correr(proveedor)
    assert reloj.esperas == [] and principal.intentos == 1      # 503: sin reintentar al principal
    assert [p.modelo for p in partes if isinstance(p, EventoReintento)] == ["m2"]
    assert partes[-1].uso.modelo == "m2" and set(proveedor.en_enfriamiento()) == {"m1"}

    tiempo[0] = 120                                             # siguiente vuelta del agente: ni se intenta m1
    partes = await _correr(proveedor)
    assert principal.intentos == 1 and partes[-1].uso.modelo == "m2"
    assert not any(isinstance(p, EventoReintento) for p in partes)

    tiempo[0] = 301                                             # pasó el enfriamiento: se vuelve a probar
    principal.errores = []
    partes = await _correr(proveedor)
    assert principal.intentos == 2 and partes[-1].uso.modelo == "m1" and proveedor.en_enfriamiento() == {}


@pytest.mark.anyio
async def test_con_respaldo_el_principal_se_reintenta_una_sola_vez():
    principal = ProveedorQueFalla("m1", [LIMITE] * 4)           # 429 breve: no es saturación
    respaldo1 = ProveedorQueFalla("m2", [LIMITE])               # el respaldo se prueba una sola vez
    respaldo2 = ProveedorQueFalla("m3", [])
    reloj = Reloj()
    proveedor = ConReintentos([principal, respaldo1, respaldo2], dormir=reloj)
    assert proveedor.esperas == [2]
    partes = await _correr(proveedor)
    assert reloj.esperas == [2]
    assert [p.modelo for p in partes if isinstance(p, EventoReintento) and p.cambio_de_modelo] == ["m2", "m3"]
    assert (principal.intentos, respaldo1.intentos, respaldo2.intentos) == (2, 1, 1)
    assert partes[-1].uso.modelo == "m3"                        # el uso dice qué modelo respondió
    assert proveedor.en_enfriamiento() == {}                    # un 429 breve no abre el circuito


@pytest.mark.anyio
async def test_si_todos_estan_saturados_el_error_lo_dice_y_luego_se_prueban_igual():
    tiempo = [0.0]
    m1, m2 = ProveedorQueFalla("m1", [SATURADO] * 9), ProveedorQueFalla("m2", [SATURADO] * 9)
    proveedor = ConReintentos([m1, m2], dormir=Reloj(), reloj=lambda: tiempo[0])
    with pytest.raises(ErrorLLM, match=r"saturado\. Se agotaron los reintentos \(modelos probados: m1, m2\)"):
        await _correr(proveedor)
    assert set(proveedor.en_enfriamiento()) == {"m1"}           # el último no tiene a quién pasar
    m1.errores, m2.errores = [], []
    tiempo[0] = 10
    partes = await _correr(proveedor)                           # m1 enfriado: se usa m2 primero
    assert partes[-1].uso.modelo == "m2"


@pytest.mark.anyio
async def test_no_reintenta_errores_no_reintentables_ni_tras_mostrar_texto():
    reloj = Reloj()
    clave = ProveedorQueFalla("m1", [ErrorLLM("La API key es inválida.")])
    with pytest.raises(ErrorLLM, match="API key"):
        await _correr(ConReintentos([clave, ProveedorQueFalla("m2", [])], dormir=reloj))
    a_medias = ProveedorQueFalla("m1", [SATURADO], texto_antes="Hola, ")
    with pytest.raises(ErrorLLM, match="saturado"):
        await _correr(ConReintentos([a_medias, ProveedorQueFalla("m2", [])], dormir=reloj))
    assert reloj.esperas == [] and clave.intentos == 1 and a_medias.intentos == 1


@pytest.mark.anyio
async def test_espera_sugerida_larga_enfria_y_pasa_al_respaldo():
    cuota = ErrorLLM("Límite de uso.", reintentable=True, espera_sugerida=41)
    reloj, tiempo = Reloj(), [0.0]
    proveedor = ConReintentos([ProveedorQueFalla("m1", [cuota]), ProveedorQueFalla("m2", [])], dormir=reloj,
                              reloj=lambda: tiempo[0])
    partes = await _correr(proveedor)
    assert reloj.esperas == [] and partes[-1].uso.modelo == "m2"
    assert proveedor.en_enfriamiento() == {"m1": 41}            # lo que pidió Gemini, no los 300 s
    # Sin respaldo, se reintenta igual con las esperas normales.
    reloj = Reloj()
    await _correr(ConReintentos([ProveedorQueFalla("m1", [cuota])], dormir=reloj))
    assert reloj.esperas == [2]


def test_lista_de_respaldo_y_fabrica():
    assert lista_respaldo(None, ("a", "b"), "a") == ["b"]                  # sin repetir el principal
    assert lista_respaldo("x, y ,x,,", ("a",), "p") == ["x", "y"]
    assert lista_respaldo("Ninguno", ("a",), "p") == []
    gemini = crear_proveedor("gemini", api_key="x")
    assert isinstance(gemini, ConReintentos) and gemini.modelo == "gemini-3.8-flash"
    assert gemini.respaldos == list(RESPALDO_POR_DEFECTO) == ["gemini-3.6-flash", "gemini-3.5-flash-lite",
                                                              "gemini-3.1-flash-lite"]
    assert gemini.esperas == [2] and gemini.enfriamiento == 300            # con respaldo: 1 reintento
    assert crear_proveedor("gemini", api_key="x", modelos_respaldo="ninguno").esperas == [2, 4, 8]
    assert len({id(p._cliente) for p in gemini.proveedores}) == 1          # un solo cliente HTTP
    anthropic = crear_proveedor("anthropic", api_key="x", modelos_respaldo="claude-haiku-5-5", reintentos=1)
    assert anthropic.respaldos == ["claude-haiku-5-5"] and anthropic.esperas == [2]
    cliente = anthropic.proveedores[0]._cliente
    assert cliente.max_retries == 0 and cliente.timeout == 30              # el SDK no reintenta por su cuenta
    assert crear_proveedor("anthropic", api_key="x").respaldos == []


# ------------------------------------------------- errores reintentables de cada adaptador

def _cliente_gemini_que_falla(error):
    class Modelos:
        async def generate_content_stream(self, **_):
            raise error

    return type("C", (), {"aio": type("Aio", (), {"models": Modelos()})()})()


@pytest.mark.anyio
async def test_gemini_503_y_429_son_reintentables():
    alta_demanda = errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE",
                                                      "message": "This model is currently experiencing high demand"}})
    with pytest.raises(ErrorLLM) as info:
        await _correr(ProveedorGemini(modelo="gemini-3.8-flash", cliente=_cliente_gemini_que_falla(alta_demanda)))
    assert info.value.saturado and "saturado" in info.value.mensaje and "high demand" in info.value.mensaje

    cuota = errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota",
                                               "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                            "retryDelay": "41s"}]}})
    assert espera_sugerida(cuota) == 41
    with pytest.raises(ErrorLLM) as info:
        await _correr(ProveedorGemini(cliente=_cliente_gemini_que_falla(cuota)))
    assert info.value.reintentable and not info.value.saturado and info.value.espera_sugerida == 41

    otro = errors.ServerError(500, {"error": {"code": 500, "status": "INTERNAL", "message": "x"}})
    with pytest.raises(ErrorLLM) as info:
        await _correr(ProveedorGemini(cliente=_cliente_gemini_que_falla(otro)))
    assert not info.value.reintentable


def _cliente_anthropic_que_falla(error):
    class Mensajes:
        def stream(self, **_):
            raise error

    return type("C", (), {"beta": type("Beta", (), {"messages": Mensajes()})()})()


@pytest.mark.anyio
async def test_anthropic_529_y_overloaded_a_mitad_del_stream_son_reintentables():
    solicitud = httpx2.Request("POST", "https://x")
    sobrecarga = OverloadedError("overloaded", response=httpx2.Response(529, request=solicitud,
                                                                        headers={"retry-after": "3"}), body=None)
    with pytest.raises(ErrorLLM) as info:
        await _correr(ProveedorAnthropic(cliente=_cliente_anthropic_que_falla(sobrecarga)))
    assert info.value.saturado and info.value.espera_sugerida == 3 and "overloaded" in info.value.mensaje

    # Evento `error` dentro del stream: llega con HTTP 200 y el tipo en el cuerpo.
    en_stream = APIStatusError("overloaded", response=httpx2.Response(200, request=solicitud),
                               body={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    with pytest.raises(ErrorLLM) as info:
        await _correr(ProveedorAnthropic(cliente=_cliente_anthropic_que_falla(en_stream)))
    assert info.value.reintentable

    invalida = APIStatusError("bad", response=httpx2.Response(400, request=solicitud),
                              body={"type": "error", "error": {"type": "invalid_request_error"}})
    with pytest.raises(ErrorLLM) as info:
        await _correr(ProveedorAnthropic(cliente=_cliente_anthropic_que_falla(invalida)))
    assert not info.value.reintentable


# ------------------------------------------------------------------- en el agente

@pytest.mark.anyio
async def test_el_agente_emite_reintento_en_vez_de_error(servicio, tmp_path):
    proveedor = ConReintentos([ProveedorQueFalla("m1", [SATURADO])], dormir=Reloj())
    agente = Agente(proveedor, HistorialStore(tmp_path / "chat.db"))
    async with ClienteMCP.en_proceso(crear_servidor(servicio)) as mcp:
        eventos = [e async for e in agente.conversar(mcp, "ana", "hola")]
    tipos = [e["evento"] for e in eventos]
    assert "reintento" in tipos and "error" not in tipos
    reintento = next(e["datos"] for e in eventos if e["evento"] == "reintento")
    duracion = reintento.pop("duracion")
    assert reintento == {"modelo": "m1", "intento": 1, "espera": 2.0, "motivo": "El modelo está saturado.",
                         "cambio_de_modelo": False, "modelo_fallido": "m1"} and duracion >= 0
    assert eventos[-1]["evento"] == "fin"
