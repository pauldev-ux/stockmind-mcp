"""Formato neutro de la conversación, independiente del proveedor de IA.

Cada mensaje guarda su versión neutra (`bloques`) y, si lo produjo un modelo,
el contenido crudo del proveedor (`crudo`). El crudo se reenvía TAL CUAL al
mismo proveedor en los turnos siguientes: los modelos actuales exigen recibir
sus bloques de razonamiento y firmas sin cambios. Con otro proveedor se usa
solo la versión neutra.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class BloqueTexto(BaseModel):
    tipo: Literal["texto"] = "texto"
    texto: str


class BloqueLlamada(BaseModel):
    """El modelo pide ejecutar una tool."""

    tipo: Literal["llamada"] = "llamada"
    id: str
    nombre: str
    argumentos: dict[str, Any] = Field(default_factory=dict)


class BloqueResultado(BaseModel):
    """Resultado de una tool, tal como lo ve el modelo (sin tokens de confirmación)."""

    tipo: Literal["resultado"] = "resultado"
    id: str
    nombre: str
    contenido: str
    es_error: bool = False


Bloque = Annotated[BloqueTexto | BloqueLlamada | BloqueResultado, Field(discriminator="tipo")]


class Mensaje(BaseModel):
    rol: Literal["usuario", "asistente"]
    bloques: list[Bloque] = Field(default_factory=list)
    # {"proveedor": "...", "modelo": "...", "contenido": <bloques del proveedor>}
    crudo: dict[str, Any] | None = None
    # Notas que agrega el sistema (p. ej. "el usuario confirmó la operación"), no el usuario.
    nota_sistema: bool = False

    @property
    def texto(self) -> str:
        return "".join(b.texto for b in self.bloques if isinstance(b, BloqueTexto))

    @property
    def llamadas(self) -> list[BloqueLlamada]:
        return [b for b in self.bloques if isinstance(b, BloqueLlamada)]


class Herramienta(BaseModel):
    nombre: str
    descripcion: str
    esquema: dict[str, Any]


class Uso(BaseModel):
    proveedor: str
    modelo: str
    entrada: int = 0
    salida: int = 0
    cache_lectura: int = 0
    cache_escritura: int = 0

    def sumar(self, otro: "Uso") -> "Uso":
        return self.model_copy(update={
            "entrada": self.entrada + otro.entrada, "salida": self.salida + otro.salida,
            "cache_lectura": self.cache_lectura + otro.cache_lectura,
            "cache_escritura": self.cache_escritura + otro.cache_escritura,
        })


class EventoTexto(BaseModel):
    """Fragmento de texto del modelo, para mostrarlo a medida que llega."""

    texto: str


class EventoReintento(BaseModel):
    """El proveedor está saturado o limitado: se espera y se reintenta (o se pasa a un modelo de respaldo)."""

    modelo: str                   # modelo con el que se va a reintentar
    intento: int                  # 1 = primer reintento
    espera: float                 # segundos antes de reintentar
    motivo: str                   # mensaje del error, en español
    cambio_de_modelo: bool = False
    modelo_fallido: str = ""      # modelo del intento que falló
    duracion: float = 0.0         # segundos que tardó ese intento en fallar


class RespuestaLLM(BaseModel):
    """Fin de un turno del modelo."""

    mensaje: Mensaje
    uso: Uso
    # fin: terminó; tools: pidió herramientas; limite_tokens: se cortó; rechazo: el proveedor se negó
    motivo: Literal["fin", "tools", "limite_tokens", "rechazo", "otro"]
    detalle: str | None = None
