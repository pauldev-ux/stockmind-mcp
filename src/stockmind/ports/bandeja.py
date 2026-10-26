"""Puerto: lo que necesita la bandeja de salida de un ERP que no admite escritura directa.

Un conector con modo_escritura = BANDEJA puede implementar esta interfaz para
exportar en el formato de importación del ERP, dar una guía de registro
precisa y conciliar con consultas de solo lectura. Si no la implementa, se usa
la integración genérica de application/bandeja.py.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from stockmind.domain.bandeja import Coincidencia, OperacionBandeja


class IntegracionBandeja(ABC):
    @abstractmethod
    def guia(self, operacion: OperacionBandeja) -> list[str]:
        """Pasos para registrar la operación en el ERP: menú, pantalla y qué llenar."""

    @abstractmethod
    def exportar(self, operaciones: list[OperacionBandeja], tipo: str, carpeta: Path,
                 momento: datetime) -> list[Path]:
        """Escribe los archivos de las operaciones (todas del mismo `tipo`) y devuelve sus rutas."""

    @abstractmethod
    def verificar(self, operacion: OperacionBandeja) -> list[Coincidencia]:
        """Busca la operación en el ERP con consultas de SOLO LECTURA.

        Una coincidencia exacta marca la operación como registrada; las demás
        solo se informan para que el usuario decida.
        """
