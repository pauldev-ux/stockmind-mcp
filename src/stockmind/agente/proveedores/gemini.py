"""Proveedor Gemini (SDK oficial `google-genai`), con streaming y function calling.

- Modelo por defecto: gemini-3.8-flash (estable, en el nivel gratuito, recomendado por
  Google para agentes; soporta function calling y thinking low/medium/high).
- Se usa la API sin estado (`generate_content_stream`) con la llamada automática de
  funciones DESACTIVADA: el agente decide qué se ejecuta (human in the loop).
- Todas las `Part` del modelo se guardan y reenvían sin cambios, incluidas las que solo
  traen `thought_signature`: los modelos con thinking las necesitan en los turnos siguientes.
"""

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai import errors, types

try:
    import httpx
except ImportError:          # pragma: no cover
    httpx = None
try:
    import httpx2
except ImportError:          # pragma: no cover
    httpx2 = None

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

MODELO_POR_DEFECTO = "gemini-3.8-flash"
# Respaldo si el principal sigue saturado (LLM_MODELOS_RESPALDO). Según la documentación oficial
# (oct. 2026): estables, en el nivel gratuito y con function calling. Los 2.5 quedan fuera porque
# solo los pueden usar las cuentas que ya los usaban.
RESPALDO_POR_DEFECTO = ("gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
_PATRON_CLAVE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}|(?i:key=)[^&\s\"']+")
# LLM_THINKING → thinking_level. Gemini 3 no permite apagarlo del todo: "apagado" es MINIMAL,
# o LOW en los modelos que no aceptan MINIMAL (según la tabla de la guía de thinking).
NIVELES_THINKING = {"bajo": "LOW", "medio": "MEDIUM", "alto": "HIGH", "apagado": "MINIMAL"}
_SIN_MINIMAL = ("gemini-3.8-flash", "gemini-3.1-pro", "gemini-3-pro")
_TIMEOUTS = tuple(m.TimeoutException for m in (httpx, httpx2) if m is not None)


def nivel_thinking(modelo: str, thinking: str) -> str:
    nivel = NIVELES_THINKING.get(thinking, "LOW")
    if nivel == "MINIMAL" and modelo.startswith(_SIN_MINIMAL):
        return "LOW"
    return nivel


_RECHAZOS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "RECITATION"}


def herramientas_a_gemini(herramientas: list[Herramienta]) -> list[types.Tool]:
    if not herramientas:
        return []
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=h.nombre, description=h.descripcion, parameters_json_schema=h.esquema)
        for h in herramientas
    ])]


def _partes(mensaje: Mensaje) -> list[types.Part]:
    if mensaje.rol == "asistente" and mensaje.crudo and mensaje.crudo.get("proveedor") == "gemini":
        return list(types.Content.model_validate(mensaje.crudo["contenido"]).parts or [])
    partes: list[types.Part] = []
    for b in mensaje.bloques:
        if isinstance(b, BloqueTexto) and b.texto:
            partes.append(types.Part(text=b.texto))
        elif isinstance(b, BloqueLlamada):
            partes.append(types.Part(function_call=types.FunctionCall(id=b.id, name=b.nombre, args=b.argumentos)))
        elif isinstance(b, BloqueResultado):
            try:
                respuesta = json.loads(b.contenido)
            except json.JSONDecodeError:
                respuesta = b.contenido
            clave = "error" if b.es_error else "output"
            partes.append(types.Part(function_response=types.FunctionResponse(
                id=b.id, name=b.nombre, response={clave: respuesta})))
    return partes


def mensajes_a_gemini(mensajes: list[Mensaje]) -> list[types.Content]:
    contenidos = []
    for grupo in fusionar_consecutivos(mensajes):
        partes = [p for m in grupo for p in _partes(m)]
        if partes:
            contenidos.append(types.Content(role="user" if grupo[0].rol == "usuario" else "model", parts=partes))
    return contenidos


def partes_a_neutral(partes: list[types.Part], modelo: str, uso: Uso, razon: str | None) -> RespuestaLLM:
    contenido = types.Content(role="model", parts=partes)
    bloques: list = []
    texto = "".join(p.text for p in partes if p.text and not p.thought)
    if texto:
        bloques.append(BloqueTexto(texto=texto))
    for i, p in enumerate(partes):
        if p.function_call:
            bloques.append(BloqueLlamada(id=p.function_call.id or f"gemini_{i}", nombre=p.function_call.name or "",
                                         argumentos=dict(p.function_call.args or {})))
    if any(isinstance(b, BloqueLlamada) for b in bloques):
        motivo = "tools"
    elif razon in _RECHAZOS:
        motivo = "rechazo"
    elif razon == "MAX_TOKENS":
        motivo = "limite_tokens"
    elif razon in (None, "STOP", "FINISH_REASON_UNSPECIFIED"):
        motivo = "fin"
    else:
        motivo = "otro"
    return RespuestaLLM(
        mensaje=Mensaje(rol="asistente", bloques=bloques, crudo={
            "proveedor": "gemini", "modelo": modelo,
            "contenido": contenido.model_dump(mode="json", exclude_none=True)}),
        uso=uso, motivo=motivo, detalle=razon if motivo == "rechazo" else None,
    )


def detalle_error(e: errors.APIError, api_key: str | None = None) -> str:
    """«400 INVALID_ARGUMENT: <mensaje de Gemini>», sin la API key aunque viniera en el texto."""
    texto = f"{e.code} {e.status or ''}".strip() + (f": {e.message}" if e.message else "")
    if api_key:
        texto = texto.replace(api_key, "***")
    return _PATRON_CLAVE.sub("***", texto)[:800]


def espera_sugerida(e: errors.APIError) -> float | None:
    """`RetryInfo.retryDelay` (p. ej. "41s") que Gemini manda con los 429, si está."""
    cuerpo = e.details.get("error", e.details) if isinstance(e.details, dict) else {}
    for d in cuerpo.get("details") or []:
        if isinstance(d, dict) and str(d.get("retryDelay", "")).endswith("s"):
            try:
                return float(d["retryDelay"][:-1])
            except ValueError:
                return None
    return None


class ProveedorGemini(LLMProvider):
    nombre = "gemini"

    def __init__(self, api_key: str | None = None, modelo: str | None = None, *, thinking: str = "bajo",
                 max_tokens: int = 16000, timeout: float = 30.0, cliente: Any = None):
        # Los reintentos los hace ConReintentos (el SDK no reintenta por defecto).
        self.modelo = modelo or MODELO_POR_DEFECTO
        self.thinking = thinking
        self.timeout = timeout
        self.max_tokens = max_tokens
        # Sin api_key, el SDK usa GEMINI_API_KEY / GOOGLE_API_KEY del entorno.
        self._api_key = api_key or None
        # Sin timeout, el SDK espera indefinidamente: con el modelo saturado, una llamada tardó 10 min.
        # Es el tiempo máximo sin recibir datos (y el plazo que se le informa al servidor).
        self._cliente = cliente or genai.Client(api_key=self._api_key,
                                                http_options=types.HttpOptions(timeout=int(timeout * 1000)))

    def _config(self, sistema: str, herramientas: list[Herramienta]) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=sistema,
            tools=herramientas_a_gemini(herramientas) or None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(thinking_level=nivel_thinking(self.modelo, self.thinking)),
            max_output_tokens=self.max_tokens,
        )

    async def responder(self, sistema: str, mensajes: list[Mensaje],
                        herramientas: list[Herramienta]) -> AsyncIterator[EventoTexto | RespuestaLLM]:
        partes: list[types.Part] = []
        razon: str | None = None
        metadatos = None
        try:
            stream = await self._cliente.aio.models.generate_content_stream(
                model=self.modelo, contents=mensajes_a_gemini(mensajes), config=self._config(sistema, herramientas))
            async for fragmento in stream:
                if fragmento.usage_metadata:
                    metadatos = fragmento.usage_metadata
                for candidato in fragmento.candidates or []:
                    if candidato.finish_reason:
                        razon = candidato.finish_reason.name if hasattr(candidato.finish_reason, "name") \
                            else str(candidato.finish_reason)
                    for parte in (candidato.content.parts if candidato.content else None) or []:
                        partes.append(parte)
                        if parte.text and not parte.thought:
                            yield EventoTexto(texto=parte.text)
        except errors.ClientError as e:
            codigo, detalle = getattr(e, "code", None), detalle_error(e, self._api_key)
            # Una clave inválida llega como 400 INVALID_ARGUMENT (reason API_KEY_INVALID), no como 401.
            if codigo in (401, 403) or "API_KEY_INVALID" in str(e.details) or "API key not valid" in (e.message or ""):
                raise ErrorLLM(f"La API key de Gemini es inválida o falta (LLM_API_KEY). [{detalle}]") from None
            if codigo == 404:
                raise ErrorLLM(f"El modelo «{self.modelo}» no existe o no está disponible (LLM_MODEL). "
                               f"[{detalle}]") from None
            if codigo == 429:
                raise ErrorLLM(f"Se alcanzó el límite de uso de Gemini (nivel gratuito); intenta en un momento. "
                               f"[{detalle}]", reintentable=True, espera_sugerida=espera_sugerida(e)) from None
            raise ErrorLLM(f"Gemini rechazó la petición: {detalle}") from None
        except errors.ServerError as e:
            if getattr(e, "code", None) == 503:
                raise ErrorLLM(f"El modelo «{self.modelo}» de Gemini está saturado. [{detalle_error(e, self._api_key)}]",
                               saturado=True) from None
            raise ErrorLLM(f"Error de Gemini; intenta de nuevo. [{detalle_error(e, self._api_key)}]") from None
        except errors.APIError as e:
            raise ErrorLLM(f"Error al llamar a Gemini: {detalle_error(e, self._api_key)}") from None
        except _TIMEOUTS:
            raise ErrorLLM(f"El modelo «{self.modelo}» de Gemini no respondió en {self.timeout:.0f} s.",
                           saturado=True) from None

        uso = Uso(
            proveedor="gemini", modelo=self.modelo,
            entrada=(metadatos.prompt_token_count or 0) if metadatos else 0,
            salida=((metadatos.candidates_token_count or 0) + (metadatos.thoughts_token_count or 0)) if metadatos else 0,
            cache_lectura=(metadatos.cached_content_token_count or 0) if metadatos else 0,
        )
        yield partes_a_neutral(partes, self.modelo, uso, razon)
