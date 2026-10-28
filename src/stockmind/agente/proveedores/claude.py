"""Proveedor Anthropic (SDK oficial `anthropic`), con streaming y tools.

Decisiones (según la documentación de la API de Claude):
- Modelo por defecto: claude-sonnet-5-5 (se cambia con LLM_MODEL).
- Thinking según LLM_THINKING (bajo por defecto): adaptativo con `output_config.effort`; "apagado"
  usa `between_tools` en Sonnet 5.5 y `disabled` donde se acepta (Opus 5.5 no lo permite: queda en low).
- Fallback del lado del servidor activado por defecto (`fallbacks: "default"`); en un
  modelo que no lo soporta la petición corre igual, sin fallback.
- Caché de prompt automática: el prompt de sistema y las tools no cambian entre turnos.
- `eager_input_streaming` en cada tool; el servidor MCP valida los argumentos.
- El historial se reenvía sin editar (bloques de thinking incluidos). Tras un fallback a
  mitad de respuesta se omiten los thinking/tool_use previos al último bloque `fallback`.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from stockmind.agente.llm import ErrorLLM, LLMProvider, fusionar_consecutivos
from stockmind.agente.modelos import (
    BloqueLlamada,
    BloqueResultado,
    BloqueTexto,
    EventoTexto,
    Herramienta,
    Mensaje,
    RespuestaLLM,
    Uso,
)

log = logging.getLogger(__name__)

MODELO_POR_DEFECTO = "claude-sonnet-5-5"
BETA_FALLBACK = "server-side-fallback-2026-07-01"
# Sin respaldo por defecto: la saturación se maneja con reintentos (y `fallbacks` cubre los rechazos).
RESPALDO_POR_DEFECTO: tuple[str, ...] = ()
_MOTIVOS = {"end_turn": "fin", "stop_sequence": "fin", "tool_use": "tools", "max_tokens": "limite_tokens",
            "refusal": "rechazo"}
_REINTENTOS_JSON = 2
_EFFORT = {"bajo": "low", "medio": "medium", "alto": "high", "apagado": "low"}
_SIN_APAGAR = ("claude-opus-5-5", "claude-fable-5")     # thinking siempre activo: effort es el único control


def parametros_thinking(modelo: str, thinking: str) -> dict[str, Any]:
    efecto = {"output_config": {"effort": _EFFORT.get(thinking, "low")}}
    if thinking != "apagado" or modelo.startswith(_SIN_APAGAR):
        return {"thinking": {"type": "adaptive"}, **efecto}
    if modelo.startswith("claude-sonnet-5-5"):
        return {"thinking": {"type": "between_tools"}, **efecto}   # disabled da 400 en Sonnet 5.5
    return {"thinking": {"type": "disabled"}, **efecto}


def _saturado(e: anthropic.APIStatusError) -> bool:
    """529/503, o un evento `error` de tipo overloaded_error a mitad del stream (llega con HTTP 200)."""
    if e.status_code in (503, 529):
        return True
    cuerpo = e.body if isinstance(e.body, dict) else {}
    error = cuerpo.get("error") if isinstance(cuerpo.get("error"), dict) else cuerpo
    return error.get("type") == "overloaded_error"


def _retry_after(e: anthropic.APIStatusError) -> float | None:
    try:
        return float(e.response.headers.get("retry-after"))
    except (TypeError, ValueError, AttributeError):
        return None


def herramientas_a_anthropic(herramientas: list[Herramienta]) -> list[dict[str, Any]]:
    return [{"name": h.nombre, "description": h.descripcion, "input_schema": h.esquema,
             "eager_input_streaming": True} for h in herramientas]


def _contenido(mensaje: Mensaje) -> list[dict[str, Any]]:
    if mensaje.rol == "asistente" and mensaje.crudo and mensaje.crudo.get("proveedor") == "anthropic":
        return list(mensaje.crudo["contenido"])
    contenido: list[dict[str, Any]] = []
    for b in mensaje.bloques:
        if isinstance(b, BloqueTexto) and b.texto:
            contenido.append({"type": "text", "text": b.texto})
        elif isinstance(b, BloqueLlamada):
            contenido.append({"type": "tool_use", "id": b.id, "name": b.nombre, "input": b.argumentos})
        elif isinstance(b, BloqueResultado):
            contenido.append({"type": "tool_result", "tool_use_id": b.id, "content": b.contenido,
                              "is_error": b.es_error})
    return contenido


def mensajes_a_anthropic(mensajes: list[Mensaje]) -> list[dict[str, Any]]:
    """Formato de la API. Los mensajes seguidos del mismo rol van en un solo turno
    (p. ej. resultados de tools + nota del sistema + nueva pregunta)."""
    salida = []
    for grupo in fusionar_consecutivos(mensajes):
        contenido = [bloque for m in grupo for bloque in _contenido(m)]
        if contenido:
            salida.append({"role": "user" if grupo[0].rol == "usuario" else "assistant", "content": contenido})
    return salida


def contenido_para_historial(contenido: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tras un fallback a mitad de respuesta, omitir thinking, redacted_thinking y tool_use anteriores
    al último bloque `fallback` (el bloque `fallback` es solo una marca de auditoría: se descarta)."""
    marcas = [i for i, b in enumerate(contenido) if b.get("type") == "fallback"]
    if not marcas:
        return contenido
    ultima = marcas[-1]
    omitir = {"thinking", "redacted_thinking", "tool_use"}
    return [b for i, b in enumerate(contenido)
            if b.get("type") != "fallback" and not (i < ultima and b.get("type") in omitir)]


def respuesta_a_neutral(final: Any) -> RespuestaLLM:
    contenido = contenido_para_historial([b.to_dict(mode="json", exclude_none=True) for b in final.content])
    bloques: list = []
    for b in contenido:
        if b.get("type") == "text" and b.get("text"):
            bloques.append(BloqueTexto(texto=b["text"]))
        elif b.get("type") == "tool_use":
            entrada = b.get("input")
            bloques.append(BloqueLlamada(id=b["id"], nombre=b["name"],
                                         argumentos=entrada if isinstance(entrada, dict) else {"_invalido": entrada}))
    uso = final.usage
    detalle = None
    if final.stop_reason == "refusal" and getattr(final, "stop_details", None):
        detalle = getattr(final.stop_details, "explanation", None)
    return RespuestaLLM(
        mensaje=Mensaje(rol="asistente", bloques=bloques,
                        crudo={"proveedor": "anthropic", "modelo": final.model, "contenido": contenido}),
        uso=Uso(proveedor="anthropic", modelo=final.model, entrada=uso.input_tokens, salida=uso.output_tokens,
                cache_lectura=uso.cache_read_input_tokens or 0, cache_escritura=uso.cache_creation_input_tokens or 0),
        motivo=_MOTIVOS.get(final.stop_reason or "", "otro"),
        detalle=detalle,
    )


class ProveedorAnthropic(LLMProvider):
    contexto_ligado = True      # preserved thinking: los bloques valen solo con el mismo prefijo
    nombre = "anthropic"

    def __init__(self, api_key: str | None = None, modelo: str | None = None, *, thinking: str = "bajo",
                 fallbacks: bool = True, max_tokens: int = 16000, timeout: float = 30.0, cliente: Any = None):
        self.modelo = modelo or MODELO_POR_DEFECTO
        self.thinking = thinking
        self.timeout = timeout
        self.fallbacks = fallbacks
        self.max_tokens = max_tokens
        # Sin api_key, el SDK resuelve las credenciales del entorno (ANTHROPIC_API_KEY, perfil de `ant`…).
        # max_retries=0: los reintentos los hace ConReintentos, que además avisa al usuario.
        # timeout: tiempo máximo sin recibir datos del stream.
        self._cliente = cliente or anthropic.AsyncAnthropic(api_key=api_key or None, max_retries=0, timeout=timeout)

    def _parametros(self, sistema: str, mensajes: list[Mensaje], herramientas: list[Herramienta]) -> dict[str, Any]:
        parametros: dict[str, Any] = {
            "model": self.modelo,
            "max_tokens": self.max_tokens,
            "system": sistema,
            "messages": mensajes_a_anthropic(mensajes),
            **parametros_thinking(self.modelo, self.thinking),
            "cache_control": {"type": "ephemeral"},
        }
        if herramientas:
            parametros["tools"] = herramientas_a_anthropic(herramientas)
        if self.fallbacks:
            parametros["fallbacks"] = "default"
            parametros["betas"] = [BETA_FALLBACK]
        return parametros

    async def responder(self, sistema: str, mensajes: list[Mensaje],
                        herramientas: list[Herramienta]) -> AsyncIterator[EventoTexto | RespuestaLLM]:
        parametros = self._parametros(sistema, mensajes, herramientas)
        for intento in range(_REINTENTOS_JSON + 1):
            emitio_texto = False
            try:
                async with self._cliente.beta.messages.stream(**parametros) as stream:
                    async for evento in stream:
                        if evento.type == "text" and evento.text:
                            emitio_texto = True
                            yield EventoTexto(texto=evento.text)
                    final = await stream.get_final_message()
                break
            except ValueError:
                # JSON de una tool que el SDK no pudo leer (eager_input_streaming): se repite el turno,
                # salvo que ya se haya mostrado texto al usuario.
                if emitio_texto or intento == _REINTENTOS_JSON:
                    raise ErrorLLM("El modelo devolvió una llamada a herramienta ilegible; intenta de nuevo.") from None
                log.warning("JSON ilegible en una tool; se repite el turno (%s)", intento + 1)
            except anthropic.AuthenticationError:
                raise ErrorLLM("La API key de Anthropic es inválida o falta (LLM_API_KEY).") from None
            except anthropic.PermissionDeniedError:
                raise ErrorLLM("La API key de Anthropic no tiene permiso para este modelo.") from None
            except anthropic.NotFoundError:
                raise ErrorLLM(f"El modelo «{self.modelo}» no existe o no está disponible (LLM_MODEL).") from None
            except anthropic.RateLimitError as e:
                raise ErrorLLM("Se alcanzó el límite de uso de Anthropic; intenta en unos segundos.",
                               reintentable=True, espera_sugerida=_retry_after(e)) from None
            except anthropic.APIStatusError as e:
                if _saturado(e):
                    raise ErrorLLM(f"El modelo «{self.modelo}» de Anthropic está saturado (overloaded).",
                                   saturado=True, espera_sugerida=_retry_after(e)) from None
                raise ErrorLLM(f"Error de Anthropic ({e.status_code}); intenta de nuevo.") from None
            except anthropic.APITimeoutError:
                raise ErrorLLM(f"El modelo «{self.modelo}» de Anthropic no respondió en {self.timeout:.0f} s.",
                               saturado=True) from None
            except anthropic.APIConnectionError:
                raise ErrorLLM("No se pudo conectar con Anthropic; revisa la conexión a internet.") from None
        yield respuesta_a_neutral(final)
