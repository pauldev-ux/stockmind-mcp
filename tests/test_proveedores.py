"""Adaptadores de Anthropic y Gemini: conversión de formatos y streaming con clientes simulados (sin red)."""

import httpx2
import pytest
from anthropic import RateLimitError
from anthropic.types.beta import BetaMessage
from google.genai import errors, types

from stockmind.agente.llm import ErrorLLM
from stockmind.agente.modelos import BloqueLlamada, BloqueResultado, BloqueTexto, Herramienta, Mensaje, RespuestaLLM
from stockmind.agente.proveedores import crear_proveedor
from stockmind.agente.proveedores.claude import (
    ProveedorAnthropic,
    contenido_para_historial,
    mensajes_a_anthropic,
    parametros_thinking,
    respuesta_a_neutral,
)
from stockmind.agente.proveedores.gemini import (
    ProveedorGemini,
    detalle_error,
    nivel_thinking,
    mensajes_a_gemini,
    partes_a_neutral,
)

HERRAMIENTA = Herramienta(nombre="obtener_producto", descripcion="Un producto",
                          esquema={"type": "object", "properties": {"id_o_codigo": {"type": "string"}},
                                   "required": ["id_o_codigo"]})


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _turno_con_tools() -> list[Mensaje]:
    return [
        Mensaje(rol="usuario", bloques=[BloqueTexto(texto="¿stock de CON-001?")]),
        Mensaje(rol="asistente", bloques=[BloqueLlamada(id="t1", nombre="obtener_producto",
                                                        argumentos={"id_o_codigo": "CON-001"})]),
        Mensaje(rol="usuario", bloques=[BloqueResultado(id="t1", nombre="obtener_producto", contenido='{"ok": true}')]),
        Mensaje(rol="usuario", nota_sistema=True, bloques=[BloqueTexto(texto="[Nota del sistema] …")]),
        Mensaje(rol="usuario", bloques=[BloqueTexto(texto="gracias")]),
    ]


# ---------------------------------------------------------------- Anthropic

def test_modelo_por_defecto_y_fabrica():
    assert crear_proveedor("anthropic", api_key="x").modelo == "claude-sonnet-5-5"
    assert crear_proveedor("anthropic", api_key="x", modelo="claude-opus-5-5").modelo == "claude-opus-5-5"
    assert crear_proveedor("gemini", api_key="x").modelo == "gemini-3.8-flash"
    with pytest.raises(ValueError, match="LLM_PROVIDER desconocido"):
        crear_proveedor("openai")


def test_mensajes_a_anthropic_fusiona_turnos_y_pone_tool_result_primero():
    salida = mensajes_a_anthropic(_turno_con_tools())
    assert [m["role"] for m in salida] == ["user", "assistant", "user"]
    assert salida[1]["content"] == [{"type": "tool_use", "id": "t1", "name": "obtener_producto",
                                     "input": {"id_o_codigo": "CON-001"}}]
    assert [b["type"] for b in salida[2]["content"]] == ["tool_result", "text", "text"]


def test_crudo_de_anthropic_se_reenvia_sin_cambios():
    crudo = [{"type": "thinking", "thinking": "", "signature": "firma-opaca"}, {"type": "text", "text": "Hola"}]
    mensaje = Mensaje(rol="asistente", bloques=[BloqueTexto(texto="Hola")],
                      crudo={"proveedor": "anthropic", "modelo": "claude-sonnet-5-5", "contenido": crudo})
    assert mensajes_a_anthropic([Mensaje(rol="usuario", bloques=[BloqueTexto(texto="hola")]), mensaje])[1]["content"] == crudo
    otro = mensaje.model_copy(update={"crudo": {"proveedor": "gemini", "contenido": {}}})
    assert mensajes_a_anthropic([otro])[0]["content"] == [{"type": "text", "text": "Hola"}]   # otro proveedor: neutro


def test_regla_de_fallback_para_el_historial():
    contenido = [{"type": "thinking", "thinking": "", "signature": "a"}, {"type": "text", "text": "parcial"},
                 {"type": "tool_use", "id": "x", "name": "n", "input": {}}, {"type": "fallback"},
                 {"type": "thinking", "thinking": "", "signature": "b"}, {"type": "text", "text": "final"}]
    assert [b["type"] + b.get("signature", "") for b in contenido_para_historial(contenido)] == [
        "text", "thinkingb", "text"]
    sin_fallback = [{"type": "text", "text": "x"}]
    assert contenido_para_historial(sin_fallback) == sin_fallback


def _mensaje_beta(**cambios) -> BetaMessage:
    base = {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "text", "text": "Busco el producto."},
                    {"type": "tool_use", "id": "toolu_1", "name": "obtener_producto", "input": {"id_o_codigo": "CON-001"}}],
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 1000,
                  "cache_creation_input_tokens": 0},
    }
    return BetaMessage.model_validate(base | cambios)


def test_respuesta_de_anthropic_a_neutral():
    r = respuesta_a_neutral(_mensaje_beta())
    assert r.motivo == "tools" and r.mensaje.texto == "Busco el producto."
    assert r.mensaje.llamadas[0].argumentos == {"id_o_codigo": "CON-001"}
    assert r.mensaje.crudo["contenido"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}
    assert (r.uso.entrada, r.uso.salida, r.uso.cache_lectura) == (1200, 80, 1000)
    rechazo = respuesta_a_neutral(_mensaje_beta(stop_reason="refusal", content=[]))
    assert rechazo.motivo == "rechazo"


class _StreamFalso:
    def __init__(self, final, textos):
        self._final, self._textos = final, textos

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for t in self._textos:
                yield type("Evento", (), {"type": "text", "text": t})()
        return gen()

    async def get_final_message(self):
        return self._final


class _ClienteAnthropicFalso:
    def __init__(self, final=None, error=None):
        self.parametros = None
        cliente = self

        class Mensajes:
            def stream(self, **parametros):
                cliente.parametros = parametros
                if error:
                    raise error
                return _StreamFalso(final, ["Busco ", "el producto."])

        self.beta = type("Beta", (), {"messages": Mensajes()})()


@pytest.mark.anyio
async def test_streaming_anthropic_y_parametros():
    cliente = _ClienteAnthropicFalso(_mensaje_beta())
    proveedor = ProveedorAnthropic(cliente=cliente)              # thinking "bajo" por defecto
    partes = [p async for p in proveedor.responder("sistema", _turno_con_tools()[:1], [HERRAMIENTA])]
    assert [p.texto for p in partes[:-1]] == ["Busco ", "el producto."] and isinstance(partes[-1], RespuestaLLM)
    p = cliente.parametros
    assert p["model"] == "claude-sonnet-5-5" and p["thinking"] == {"type": "adaptive"}
    assert p["output_config"] == {"effort": "low"} and p["cache_control"] == {"type": "ephemeral"}
    assert p["fallbacks"] == "default" and p["betas"] == ["server-side-fallback-2026-07-01"]
    assert p["tools"][0]["eager_input_streaming"] is True and p["tools"][0]["input_schema"] == HERRAMIENTA.esquema
    sin_fallback = _ClienteAnthropicFalso(_mensaje_beta())
    [_ async for _ in ProveedorAnthropic(cliente=sin_fallback, fallbacks=False).responder("s", [], [])]
    assert "fallbacks" not in sin_fallback.parametros and "tools" not in sin_fallback.parametros


@pytest.mark.anyio
async def test_errores_de_anthropic_en_espanol():
    limite = RateLimitError("rate limited", response=httpx2.Response(429, request=httpx2.Request("POST", "https://x")),
                            body=None)
    proveedor = ProveedorAnthropic(cliente=_ClienteAnthropicFalso(error=limite))
    with pytest.raises(ErrorLLM, match="límite de uso"):
        [_ async for _ in proveedor.responder("s", [], [])]


# ------------------------------------------------------------------- Gemini

def test_mensajes_a_gemini():
    contenidos = mensajes_a_gemini(_turno_con_tools())
    assert [c.role for c in contenidos] == ["user", "model", "user"]
    llamada = contenidos[1].parts[0].function_call
    assert (llamada.id, llamada.name, llamada.args) == ("t1", "obtener_producto", {"id_o_codigo": "CON-001"})
    respuesta = contenidos[2].parts[0].function_response
    assert respuesta.name == "obtener_producto" and respuesta.response == {"output": {"ok": True}}


def test_partes_de_gemini_se_guardan_y_reenvian_con_firma():
    partes = [types.Part(text="pensando…", thought=True),
              types.Part(text="Busco el producto."),
              types.Part(function_call=types.FunctionCall(id="c1", name="obtener_producto",
                                                          args={"id_o_codigo": "CON-001"}),
                         thought_signature=b"\x01firma")]
    from stockmind.agente.modelos import Uso

    r = partes_a_neutral(partes, "gemini-3.8-flash", Uso(proveedor="gemini", modelo="gemini-3.8-flash"), "STOP")
    assert r.motivo == "tools" and r.mensaje.texto == "Busco el producto."      # el razonamiento no se muestra
    assert r.mensaje.llamadas[0].id == "c1"
    reenviado = mensajes_a_gemini([Mensaje.model_validate_json(r.mensaje.model_dump_json())])[0]
    assert reenviado.parts[2].thought_signature == b"\x01firma" and reenviado.parts[0].thought
    rechazo = partes_a_neutral([], "m", Uso(proveedor="gemini", modelo="m"), "SAFETY")
    assert rechazo.motivo == "rechazo"


@pytest.mark.anyio
async def test_streaming_gemini_y_configuracion():
    capturado = {}

    class Modelos:
        async def generate_content_stream(self, *, model, contents, config):
            capturado.update(model=model, contents=contents, config=config)

            async def gen():
                yield types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
                    role="model", parts=[types.Part(text="Hola ")]))])
                yield types.GenerateContentResponse(
                    candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text="Ana")]),
                                                finish_reason=types.FinishReason.STOP)],
                    usage_metadata=types.GenerateContentResponseUsageMetadata(
                        prompt_token_count=50, candidates_token_count=5, thoughts_token_count=7))
            return gen()

    cliente = type("C", (), {"aio": type("Aio", (), {"models": Modelos()})()})()
    proveedor = ProveedorGemini(cliente=cliente)
    partes = [p async for p in proveedor.responder("sistema", _turno_con_tools()[:1], [HERRAMIENTA])]
    assert [p.texto for p in partes[:-1]] == ["Hola ", "Ana"]
    final = partes[-1]
    assert final.motivo == "fin" and final.mensaje.texto == "Hola Ana"
    assert (final.uso.entrada, final.uso.salida) == (50, 12)
    config = capturado["config"]
    assert capturado["model"] == "gemini-3.8-flash" and config.automatic_function_calling.disable is True
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW          # "bajo" por defecto
    assert config.tools[0].function_declarations[0].parameters_json_schema == HERRAMIENTA.esquema


def _error_gemini(codigo: int, estado: str, mensaje: str, razon: str | None = None) -> errors.ClientError:
    cuerpo = {"error": {"code": codigo, "status": estado, "message": mensaje,
                        "details": [{"reason": razon}] if razon else []}}
    return errors.ClientError(codigo, cuerpo)


def _proveedor_que_falla(error: Exception) -> ProveedorGemini:
    class Modelos:
        async def generate_content_stream(self, **_):
            raise error

    return ProveedorGemini(api_key="AIzaClaveSecretaDePrueba1234567890abc",
                           cliente=type("C", (), {"aio": type("Aio", (), {"models": Modelos()})()})())


@pytest.mark.anyio
async def test_errores_de_gemini_traen_el_detalle_sin_la_clave():
    # Gemini responde 400 (no 401) a una clave inválida: se reconoce igual.
    clave_mala = _error_gemini(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.",
                               "API_KEY_INVALID")
    with pytest.raises(ErrorLLM, match=r"API key de Gemini es inválida.*400 INVALID_ARGUMENT: API key not valid"):
        [_ async for _ in _proveedor_que_falla(clave_mala).responder("s", [], [])]

    esquema = _error_gemini(400, "INVALID_ARGUMENT", "Invalid JSON payload received. Unknown name \"$defs\" "
                                                      "at 'tools[0]'; url ?key=AIzaClaveSecretaDePrueba1234567890abc")
    with pytest.raises(ErrorLLM) as info:
        [_ async for _ in _proveedor_que_falla(esquema).responder("s", [], [])]
    mensaje = str(info.value)
    assert mensaje.startswith("Gemini rechazó la petición: 400 INVALID_ARGUMENT: Invalid JSON payload")
    assert "AIza" not in mensaje and "***" in mensaje
    assert detalle_error(_error_gemini(429, "RESOURCE_EXHAUSTED", "cuota k3y"), "k3y") == "429 RESOURCE_EXHAUSTED: cuota ***"


def test_niveles_de_thinking_por_modelo():
    # Anthropic: effort por nivel; "apagado" depende del modelo (según la guía de migración).
    assert parametros_thinking("claude-sonnet-5-5", "bajo") == {"thinking": {"type": "adaptive"},
                                                                "output_config": {"effort": "low"}}
    assert parametros_thinking("claude-sonnet-5-5", "alto")["output_config"] == {"effort": "high"}
    assert parametros_thinking("claude-sonnet-5-5", "apagado")["thinking"] == {"type": "between_tools"}
    assert parametros_thinking("claude-haiku-5-5", "apagado")["thinking"] == {"type": "disabled"}
    assert parametros_thinking("claude-opus-5-5", "apagado") == {"thinking": {"type": "adaptive"},
                                                                 "output_config": {"effort": "low"}}
    # Gemini: thinking_level; 3.8-flash no acepta MINIMAL.
    assert nivel_thinking("gemini-3.8-flash", "bajo") == "LOW"
    assert nivel_thinking("gemini-3.8-flash", "medio") == "MEDIUM"
    assert nivel_thinking("gemini-3.8-flash", "apagado") == "LOW"
    assert nivel_thinking("gemini-3.6-flash", "apagado") == "MINIMAL"
    assert ProveedorGemini(api_key="x", modelo="gemini-3.5-flash-lite", thinking="apagado")._config(
        "s", []).thinking_config.thinking_level == types.ThinkingLevel.MINIMAL


@pytest.mark.anyio
async def test_timeout_de_gemini_es_saturacion():
    error = httpx2.ReadTimeout("sin datos")
    with pytest.raises(ErrorLLM) as info:
        [_ async for _ in _proveedor_que_falla(error).responder("s", [], [])]
    assert info.value.saturado and "no respondió en 30 s" in info.value.mensaje
    assert ProveedorGemini(api_key="x", timeout=45)._cliente._api_client._http_options.timeout == 45000
