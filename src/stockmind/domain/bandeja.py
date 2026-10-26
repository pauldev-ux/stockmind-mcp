"""Operaciones de la bandeja de salida (escrituras pendientes de registrar en un ERP)."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EstadoBandeja(StrEnum):
    PENDIENTE = "pendiente"
    REGISTRADA = "registrada"
    DESCARTADA = "descartada"


class OperacionBandeja(BaseModel):
    id: int
    tipo: str                      # crear_producto | registrar_movimiento
    conector: str
    origen: str
    datos: dict[str, Any]          # datos validados de la operación (los mismos que se confirmaron)
    vista_previa: dict[str, Any] = Field(default_factory=dict)
    detalle_erp: dict[str, Any] = Field(default_factory=dict)
    guia: list[str] = Field(default_factory=list)
    estado: EstadoBandeja = EstadoBandeja.PENDIENTE
    creada_en: datetime
    actualizada_en: datetime
    observacion: str | None = None
    registrada_por: str | None = None   # manual | conciliacion
    id_externo: str | None = None       # id del registro en el ERP
    exportada_en: datetime | None = None
    archivo: str | None = None

    @property
    def referencia(self) -> str:
        return referencia(self.id)


class Coincidencia(BaseModel):
    """Lo que encontró el ERP al verificar una operación pendiente."""

    exacta: bool
    descripcion: str
    id_externo: str | None = None


def referencia(id_operacion: int) -> str:
    return f"SM-{id_operacion:06d}"


def parsear_referencia(texto: str | int) -> int:
    """'SM-000012', 'sm-12' o '12' → 12. Lanza ValueError si no es una referencia."""
    coincidencia = re.fullmatch(r"\s*(?:SM-?)?0*(\d+)\s*", str(texto), re.IGNORECASE)
    if not coincidencia or int(coincidencia.group(1)) == 0:
        raise ValueError(f"'{texto}' no es una referencia válida (ej.: SM-000012)")
    return int(coincidencia.group(1))
