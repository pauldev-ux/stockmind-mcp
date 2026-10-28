"""Proveedor falso para tests y demos: responde según un guion, sin llamadas reales."""

from collections.abc import AsyncIterator, Callable
from typing import Any

from pydantic import BaseModel, Field

from stockmind.agente.llm import LLMProvider
from stockmind.agente.modelos import (
    BloqueLlamada,
    BloqueTexto,
    EventoTexto,
    Herramienta,
    Mensaje,
    RespuestaLLM,
    Uso,
)


class Paso(BaseModel):
    """Lo que "dice" el modelo en un turno."""

    texto: str = ""
    llamadas: list[tuple[str, dict[str, Any]]] = Field(default_factory=list)
    motivo: str | None = None   # forzar un motivo (p. ej. "rechazo")


class ProveedorFalso(LLMProvider):
    nombre = "falso"

    def __init__(self, guion: list[Paso] | Callable[[list[Mensaje], list[Herramienta]], Paso] | None = None,
                 modelo: str = "falso-1"):
        self.modelo = modelo
        self._guion = guion if guion is not None else []
        self.llamadas: list[dict[str, Any]] = []   # lo que recibió en cada turno, para los tests

    def _siguiente(self, mensajes: list[Mensaje], herramientas: list[Herramienta]) -> Paso:
        if callable(self._guion):
            return self._guion(mensajes, herramientas)
        if not self._guion:
            return Paso(texto="(sin guion)")
        return self._guion.pop(0)

    async def responder(self, sistema: str, mensajes: list[Mensaje],
                        herramientas: list[Herramienta]) -> AsyncIterator[EventoTexto | RespuestaLLM]:
        self.llamadas.append({"sistema": sistema, "mensajes": [m.model_copy(deep=True) for m in mensajes],
                              "herramientas": [h.nombre for h in herramientas]})
        paso = self._siguiente(mensajes, herramientas)
        bloques: list = []
        if paso.texto:
            for palabra in paso.texto.split(" "):
                yield EventoTexto(texto=palabra + " ")
            bloques.append(BloqueTexto(texto=paso.texto))
        n = len(self.llamadas)
        for i, (nombre, argumentos) in enumerate(paso.llamadas):
            bloques.append(BloqueLlamada(id=f"llamada_{n}_{i}", nombre=nombre, argumentos=argumentos))
        motivo = paso.motivo or ("tools" if paso.llamadas else "fin")
        yield RespuestaLLM(
            mensaje=Mensaje(rol="asistente", bloques=bloques),
            uso=Uso(proveedor=self.nombre, modelo=self.modelo, entrada=100 + 10 * len(mensajes), salida=20),
            motivo=motivo,
        )
