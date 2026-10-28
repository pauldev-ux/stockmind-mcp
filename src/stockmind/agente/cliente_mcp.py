"""Cliente MCP del agente (SDK oficial). El agente y la API usan StockMind SOLO a través de MCP.

- `conectar_http`: streamable HTTP contra `stockmind-mcp --http`, con `Authorization: Bearer <MCP_TOKEN>`.
- `en_proceso`: conexión en memoria a un servidor MCP (tests y el chat de consola); sigue siendo MCP.
- `ConexionPersistente`: una sola sesión para todo el proceso (la API), en vez de abrir una por petición.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Any

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from pydantic import BaseModel, Field

from stockmind.agente.modelos import Herramienta

log = logging.getLogger(__name__)
_MAX_REGISTRO = 500


class ErrorMCP(Exception):
    """No se pudo hablar con el servidor MCP."""

    def __init__(self, mensaje: str):
        super().__init__(mensaje)
        self.mensaje = mensaje


def _desenvolver(error: BaseException) -> BaseException:
    """El SDK (anyio) envuelve en ExceptionGroup lo que se lanza dentro de la conexión;
    se devuelve la excepción original para que los manejadores de error la reconozcan."""
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


class ResultadoMCP(BaseModel):
    datos: dict[str, Any] = Field(default_factory=dict)
    es_error: bool = False      # error de protocolo/ejecución (no un {"ok": false} de negocio)

    @property
    def ok(self) -> bool:
        return not self.es_error and self.datos.get("ok", True) is not False


class ClienteMCP:
    def __init__(self, cliente: Client):
        self._cliente = cliente
        self._herramientas: list[Herramienta] | None = None
        self.llamadas: list[tuple[str, dict[str, Any]]] = []   # registro de lo que se ejecutó
        self.roto = False   # falló el transporte: la conexión persistente debe reabrirse

    @classmethod
    @asynccontextmanager
    async def conectar_http(cls, url: str, token: str) -> AsyncIterator["ClienteMCP"]:
        try:
            async with cls._conexion_http(url, token) as cliente:
                yield cliente
        except BaseExceptionGroup as grupo:
            raise _desenvolver(grupo) from None

    @classmethod
    @asynccontextmanager
    async def _conexion_http(cls, url: str, token: str) -> AsyncIterator["ClienteMCP"]:
        async with AsyncExitStack() as pila:
            # Solo los errores al CONECTAR se traducen; los del código que usa la conexión pasan tal cual.
            try:
                http = await pila.enter_async_context(
                    create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}))
                cliente = await pila.enter_async_context(Client(streamable_http_client(url, http_client=http)))
            except Exception as e:     # incluye los ExceptionGroup que arma anyio
                causa = e.exceptions[0] if isinstance(e, ExceptionGroup) and e.exceptions else e
                raise ErrorMCP(f"No se pudo conectar con el servidor MCP en {url} "
                               f"({type(causa).__name__}). ¿Está corriendo `stockmind-mcp --http` "
                               "y coincide MCP_TOKEN?") from None
            yield cls(cliente)

    @classmethod
    @asynccontextmanager
    async def en_proceso(cls, servidor: Any) -> AsyncIterator["ClienteMCP"]:
        try:
            async with Client(servidor) as cliente:
                yield cls(cliente)
        except BaseExceptionGroup as grupo:
            raise _desenvolver(grupo) from None

    async def herramientas(self) -> list[Herramienta]:
        if self._herramientas is None:
            listado = await self._protegido(self._cliente.list_tools())
            self._herramientas = [Herramienta(nombre=t.name, descripcion=t.description or "",
                                              esquema=t.input_schema or {"type": "object", "properties": {}})
                                  for t in listado.tools]
        return self._herramientas

    async def llamar(self, nombre: str, argumentos: dict[str, Any]) -> ResultadoMCP:
        self.llamadas.append((nombre, dict(argumentos)))
        del self.llamadas[:-_MAX_REGISTRO]
        resultado = await self._protegido(self._cliente.call_tool(nombre, argumentos))
        datos = resultado.structured_content
        if datos is None:
            texto = "".join(getattr(c, "text", "") for c in resultado.content or [])
            try:
                datos = json.loads(texto) if texto else {}
            except json.JSONDecodeError:
                datos = {"ok": not resultado.is_error, "mensaje": texto}
        if not isinstance(datos, dict):
            datos = {"resultado": datos}
        return ResultadoMCP(datos=datos, es_error=bool(resultado.is_error))

    async def _protegido(self, llamada):
        # Los errores de negocio llegan como resultado (is_error); una excepción es del transporte.
        try:
            return await llamada
        except Exception:
            self.roto = True
            raise


class ConexionPersistente:
    """Una sesión MCP compartida por todas las peticiones del proceso (se abre una vez, no por pregunta).

    anyio exige abrir y cerrar la conexión en la misma tarea, así que la abre una tarea dueña que
    vive hasta `cerrar()`. Si la conexión se rompe (o el servidor MCP se reinicia), la siguiente
    petición la vuelve a abrir. Se usa igual que `ClienteMCP.conectar_http`: `async with conexion():`.
    """

    def __init__(self, abrir: Callable[[], AbstractAsyncContextManager[ClienteMCP]]):
        self._abrir = abrir
        self._cliente: ClienteMCP | None = None
        self._tarea: asyncio.Task | None = None
        self._cerrar: asyncio.Event | None = None
        self._error: BaseException | None = None
        self._lock = asyncio.Lock()
        self.aperturas = 0

    async def _duena(self, lista: asyncio.Event, cerrar: asyncio.Event) -> None:
        try:
            async with self._abrir() as cliente:
                self._cliente = cliente
                self.aperturas += 1
                lista.set()
                await cerrar.wait()
        except BaseException as e:      # noqa: BLE001 - se informa a quien espera la conexión
            self._error = e
        finally:
            self._cliente = None
            lista.set()

    async def _detener(self) -> None:
        if self._tarea is not None and not self._tarea.done():
            self._cerrar.set()
            await self._tarea
        self._tarea = None

    async def obtener(self) -> ClienteMCP:
        async with self._lock:
            if self._cliente is None or self._cliente.roto:
                if self._cliente is not None:
                    log.warning("La conexión MCP se rompió; se vuelve a abrir.")
                await self._detener()
                lista, self._cerrar, self._error = asyncio.Event(), asyncio.Event(), None
                self._tarea = asyncio.create_task(self._duena(lista, self._cerrar))
                await lista.wait()
                if self._cliente is None:
                    error = _desenvolver(self._error) if self._error else None
                    if isinstance(error, ErrorMCP):
                        raise error
                    raise ErrorMCP(f"No se pudo conectar con el servidor MCP ({type(error).__name__}).")
            return self._cliente

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[ClienteMCP]:
        yield await self.obtener()

    async def cerrar(self) -> None:
        async with self._lock:
            await self._detener()
