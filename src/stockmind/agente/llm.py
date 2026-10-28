"""Puerto: lo que el agente necesita de un proveedor de IA (Anthropic, Gemini, un falso para tests…)."""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from stockmind.agente.modelos import EventoReintento, EventoTexto, Herramienta, Mensaje, RespuestaLLM

log = logging.getLogger(__name__)


class ErrorLLM(Exception):
    """Falla del proveedor (credenciales, cuota, red). El mensaje va en español para el usuario.

    `reintentable`: saturación o límite de uso (503, 429, 529/overloaded, timeout); conviene esperar.
    `saturado`: el modelo no da abasto (503, 529/overloaded, timeout): conviene pasar a otro modelo.
    `espera_sugerida`: segundos que pidió el proveedor (Retry-After / RetryInfo), si los informó.
    """

    def __init__(self, mensaje: str, *, reintentable: bool = False, saturado: bool = False,
                 espera_sugerida: float | None = None):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.reintentable = reintentable or saturado
        self.saturado = saturado
        self.espera_sugerida = espera_sugerida


class LLMProvider(ABC):
    nombre: str = "desconocido"
    modelo: str = ""
    # True si los bloques de razonamiento quedan ligados al contexto exacto en que se generaron
    # (Anthropic): al recortar el historial, los turnos previos se envían sin su `crudo`.
    contexto_ligado: bool = False

    @abstractmethod
    def responder(
        self, sistema: str, mensajes: list[Mensaje], herramientas: list[Herramienta]
    ) -> AsyncIterator[EventoTexto | RespuestaLLM | EventoReintento]:
        """Un turno del modelo, en streaming: cero o más EventoTexto y, al final, una RespuestaLLM.

        Lanza ErrorLLM si el proveedor falla. `ConReintentos` puede intercalar EventoReintento.
        """


class ConReintentos(LLMProvider):
    """Reintentos, modelos de respaldo y circuit breaker.

    - Un modelo SATURADO (503/529/overloaded o timeout) queda «en enfriamiento» `enfriamiento`
      segundos: mientras tanto se usa directamente el siguiente de la lista, sin reintentarlo. El
      estado vive en esta instancia, así que vale para todas las vueltas del agente y los mensajes.
    - Sin respaldo, se reintenta con espera exponencial (2, 4, 8 s). Con respaldo, el primer modelo
      se reintenta como máximo una vez (solo errores no de saturación, p. ej. un 429 breve) y los
      respaldos se prueban una vez cada uno.
    - Si el proveedor pide esperar más que la espera prevista (p. ej. cuota del día agotada) y hay
      otro modelo, se enfría ese tiempo (con tope `enfriamiento`) y se pasa al siguiente.
    - Si todos están en enfriamiento, se prueban igual, empezando por el que se libera antes.
    - Nunca se reintenta si ya se mostró texto al usuario: se duplicaría lo ya mostrado.
    """

    def __init__(self, proveedores: Sequence[LLMProvider], *, reintentos: int = 3, espera_inicial: float = 2.0,
                 enfriamiento: float = 300.0, dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 reloj: Callable[[], float] = time.monotonic):
        if not proveedores:
            raise ValueError("ConReintentos necesita al menos un proveedor.")
        self.proveedores = list(proveedores)
        self.nombre = self.proveedores[0].nombre
        self.modelo = self.proveedores[0].modelo
        self.contexto_ligado = any(p.contexto_ligado for p in self.proveedores)
        esperas = [espera_inicial * 2 ** i for i in range(max(0, reintentos))]   # 2, 4, 8
        self.esperas = esperas[:1] if len(self.proveedores) > 1 else esperas
        self.enfriamiento = enfriamiento
        self._dormir, self._reloj = dormir, reloj
        self._enfriado_hasta: dict[str, float] = {}

    @property
    def respaldos(self) -> list[str]:
        return [p.modelo for p in self.proveedores[1:]]

    def en_enfriamiento(self) -> dict[str, float]:
        """Modelos saturados hace poco → segundos que faltan para volver a probarlos."""
        ahora = self._reloj()
        return {m: hasta - ahora for m, hasta in self._enfriado_hasta.items() if hasta > ahora}

    def _candidatos(self) -> list[LLMProvider]:
        enfriados = self.en_enfriamiento()
        libres = [p for p in self.proveedores if p.modelo not in enfriados]
        return libres or sorted(self.proveedores, key=lambda p: enfriados[p.modelo])

    def _enfriar(self, modelo: str, e: ErrorLLM) -> float:
        duracion = self.enfriamiento if e.saturado or e.espera_sugerida is None \
            else min(e.espera_sugerida, self.enfriamiento)
        self._enfriado_hasta[modelo] = self._reloj() + duracion
        return duracion

    async def responder(self, sistema: str, mensajes: list[Mensaje],
                        herramientas: list[Herramienta]) -> AsyncIterator[EventoTexto | RespuestaLLM | EventoReintento]:
        candidatos = self._candidatos()
        for indice, proveedor in enumerate(candidatos):
            siguiente = candidatos[indice + 1].modelo if indice + 1 < len(candidatos) else None
            esperas = self.esperas if indice == 0 else []
            intento = 0
            while True:
                emitio_texto, inicio = False, self._reloj()
                try:
                    async for parte in proveedor.responder(sistema, mensajes, herramientas):
                        if isinstance(parte, EventoTexto) and parte.texto:
                            emitio_texto = True
                        yield parte
                    self._enfriado_hasta.pop(proveedor.modelo, None)
                    return
                except ErrorLLM as e:
                    if not e.reintentable or emitio_texto:
                        raise
                    fallo = {"modelo_fallido": proveedor.modelo, "duracion": self._reloj() - inicio}
                    pide_mucho = e.espera_sugerida is not None and e.espera_sugerida > (esperas[-1] if esperas else 0)
                    if siguiente and (e.saturado or pide_mucho):
                        duracion = self._enfriar(proveedor.modelo, e)
                        log.warning("%s saturado (%s); en enfriamiento %.0f s, se usa %s", proveedor.modelo,
                                    e.mensaje, duracion, siguiente)
                        yield EventoReintento(modelo=siguiente, intento=1, espera=0, motivo=e.mensaje,
                                              cambio_de_modelo=True, **fallo)
                        break
                    if intento < len(esperas):
                        espera = esperas[intento]
                        intento += 1
                        log.warning("%s: %s; reintento %s en %.0f s", proveedor.modelo, e.mensaje, intento, espera)
                        yield EventoReintento(modelo=proveedor.modelo, intento=intento, espera=espera,
                                              motivo=e.mensaje, **fallo)
                        await self._dormir(espera)
                        continue
                    if siguiente:
                        log.warning("%s sigue fallando; se prueba %s", proveedor.modelo, siguiente)
                        yield EventoReintento(modelo=siguiente, intento=1, espera=0, motivo=e.mensaje,
                                              cambio_de_modelo=True, **fallo)
                        break
                    if len(candidatos) == 1 and not esperas:
                        raise
                    probados = ", ".join(p.modelo for p in candidatos)
                    raise ErrorLLM(f"{e.mensaje} Se agotaron los reintentos (modelos probados: {probados}).",
                                   reintentable=True, saturado=e.saturado) from None


def fusionar_consecutivos(mensajes: list[Mensaje]) -> list[list[Mensaje]]:
    """Agrupa mensajes seguidos del mismo rol (p. ej. resultados de tools + nota del sistema + pregunta).

    Los proveedores esperan turnos alternados; cada grupo se envía como un solo turno.
    """
    grupos: list[list[Mensaje]] = []
    for m in mensajes:
        if grupos and grupos[-1][0].rol == m.rol:
            grupos[-1].append(m)
        else:
            grupos.append([m])
    return grupos
