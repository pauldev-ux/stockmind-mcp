"""Errores del dominio. Los mensajes van en español porque la IA los muestra al usuario."""

from typing import Any

from stockmind.domain.modelos import CampoRequerido, ErrorCampo


class StockMindError(Exception):
    """Base de los errores de negocio de StockMind."""

    def __init__(self, mensaje: str):
        super().__init__(mensaje)
        self.mensaje = mensaje

    def a_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": type(self).__name__, "mensaje": self.mensaje}


class ErrorValidacion(StockMindError):
    def __init__(self, faltantes: list[CampoRequerido] | None = None, invalidos: list[ErrorCampo] | None = None):
        self.faltantes = faltantes or []
        self.invalidos = invalidos or []
        partes = []
        if self.faltantes:
            partes.append("Faltan datos: " + ", ".join(c.nombre for c in self.faltantes) + ".")
        if self.invalidos:
            partes.append("Datos inválidos: " + "; ".join(f"{e.campo}: {e.mensaje}" for e in self.invalidos) + ".")
        super().__init__(" ".join(partes) or "Datos inválidos.")

    def a_dict(self) -> dict[str, Any]:
        return {
            **super().a_dict(),
            "faltantes": [
                {"campo": c.nombre, "tipo": c.tipo, "descripcion": c.descripcion, "opciones": c.opciones}
                for c in self.faltantes
            ],
            "invalidos": [e.model_dump() for e in self.invalidos],
            "instruccion": "Pide al usuario los datos que faltan o corrige los inválidos y vuelve a intentar.",
        }


class ProductoNoEncontrado(StockMindError):
    def __init__(self, id_o_codigo: str):
        super().__init__(f"No existe ningún producto con id o código '{id_o_codigo}'.")
        self.id_o_codigo = id_o_codigo


class CodigoAmbiguo(StockMindError):
    """El código coincide con varios productos (en Canaan360 el código no es único)."""

    def __init__(self, codigo: str, candidatos: list[dict[str, Any]]):
        super().__init__(f"El código '{codigo}' corresponde a {len(candidatos)} productos; indica el id.")
        self.codigo = codigo
        self.candidatos = candidatos

    def a_dict(self) -> dict[str, Any]:
        return {**super().a_dict(), "candidatos": self.candidatos}


class StockInsuficiente(StockMindError):
    def __init__(self, producto: str, disponible: float, solicitado: float):
        super().__init__(
            f"Stock insuficiente de {producto}: disponible {disponible:g}, se pide {solicitado:g}. "
            "No se permite stock negativo."
        )
        self.disponible = disponible
        self.solicitado = solicitado


class TokenInvalido(StockMindError):
    """Token de confirmación inexistente, vencido, ya usado o cancelado."""


class ErrorAlmacenamiento(StockMindError):
    """No se pudo leer o escribir en el origen de datos (archivo abierto, BD caída…)."""


class EscrituraNoHabilitada(StockMindError):
    """El conector activo es de solo lectura."""

    def __init__(self, conector: str):
        super().__init__(
            f"Escritura no habilitada para {conector}: este conector es de solo lectura. "
            f"Registra la operación directamente en {conector}."
        )
        self.conector = conector
