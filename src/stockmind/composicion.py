"""Raíz de composición: arma el servicio con el conector elegido en .env.

Es el único lugar que conoce a la vez los adaptadores y la aplicación. El
servidor MCP y el futuro backend web llaman a construir_servicio().

Conectores:
- Incluidos: los de CONECTORES (hoy, csv).
- Opcionales: cualquier paquete stockmind.adapters.<nombre> que exponga
  crear_conector(settings). Así un conector para un ERP real puede vivir fuera
  del repositorio (privado) sin que el código público dependa de él.
"""

import importlib
import re
from collections.abc import Callable
from datetime import timedelta

from stockmind.adapters.csv_excel import CsvExcelConnector
from stockmind.application.auditoria import Auditoria
from stockmind.application.bandeja import SqliteBandejaStore
from stockmind.application.confirmaciones import GestorConfirmaciones, SqliteConfirmacionStore
from stockmind.application.servicio import InventarioService
from stockmind.config.settings import Settings, get_settings
from stockmind.ports.inventario import InventarioConnector

CONECTORES: dict[str, Callable[[Settings], InventarioConnector]] = {
    "csv": lambda s: CsvExcelConnector(s.data_dir, s.formato_datos),
}

_NOMBRE_VALIDO = re.compile(r"[a-z][a-z0-9_]*")


def _conector_opcional(nombre: str) -> Callable[[Settings], InventarioConnector]:
    modulo_nombre = f"stockmind.adapters.{nombre}"
    try:
        modulo = importlib.import_module(modulo_nombre)
    except ModuleNotFoundError as e:
        if e.name and (e.name == modulo_nombre or modulo_nombre.startswith(e.name + ".")):
            raise ValueError(
                f"El conector '{nombre}' no está disponible en esta instalación. Los conectores para "
                f"ERPs reales son privados y no se incluyen en el repositorio. "
                f"Disponibles: {', '.join(CONECTORES)}."
            ) from None
        raise ValueError(
            f"Al conector '{nombre}' le falta la dependencia '{e.name}'. "
            'Si usa MySQL, instala los extras: pip install -e ".[mysql]"'
        ) from None
    fabrica = getattr(modulo, "crear_conector", None)
    if not callable(fabrica):
        raise ValueError(f"El módulo {modulo_nombre} no define crear_conector(settings).")
    return fabrica


def crear_conector(settings: Settings) -> InventarioConnector:
    nombre = settings.connector
    if nombre in CONECTORES:
        return CONECTORES[nombre](settings)
    if not _NOMBRE_VALIDO.fullmatch(nombre):
        raise ValueError(f"Nombre de conector inválido: '{nombre}'. Disponibles: {', '.join(CONECTORES)}.")
    return _conector_opcional(nombre)(settings)


def construir_servicio(settings: Settings | None = None, *, origen: str = "mcp") -> InventarioService:
    settings = settings or get_settings()
    return InventarioService(
        conector=crear_conector(settings),
        confirmaciones=GestorConfirmaciones(
            SqliteConfirmacionStore(settings.confirmaciones_db),
            ttl=timedelta(minutes=settings.token_ttl_minutos),
        ),
        auditoria=Auditoria(settings.auditoria_log),
        # Misma base local que los tokens: así el servidor MCP y el backend web la comparten.
        bandeja=SqliteBandejaStore(settings.confirmaciones_db),
        exports_dir=settings.exports_dir,
        origen=origen,
    )
