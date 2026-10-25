"""Raíz de composición: arma el servicio con el conector elegido en .env.

Es el único lugar que conoce a la vez los adaptadores y la aplicación. El
servidor MCP y el futuro backend web llaman a construir_servicio().
"""

from collections.abc import Callable
from datetime import timedelta

from stockmind.adapters.csv_excel import CsvExcelConnector
from stockmind.application.auditoria import Auditoria
from stockmind.application.confirmaciones import GestorConfirmaciones, SqliteConfirmacionStore
from stockmind.application.servicio import InventarioService
from stockmind.config.settings import Settings, get_settings
from stockmind.ports.inventario import InventarioConnector

CONECTORES: dict[str, Callable[[Settings], InventarioConnector]] = {
    "csv": lambda s: CsvExcelConnector(s.data_dir, s.formato_datos),
}


def crear_conector(settings: Settings) -> InventarioConnector:
    try:
        fabrica = CONECTORES[settings.connector]
    except KeyError:
        raise ValueError(
            f"Conector desconocido: '{settings.connector}'. Disponibles: {', '.join(CONECTORES)}"
        ) from None
    return fabrica(settings)


def construir_servicio(settings: Settings | None = None, *, origen: str = "mcp") -> InventarioService:
    settings = settings or get_settings()
    return InventarioService(
        conector=crear_conector(settings),
        confirmaciones=GestorConfirmaciones(
            SqliteConfirmacionStore(settings.confirmaciones_db),
            ttl=timedelta(minutes=settings.token_ttl_minutos),
        ),
        auditoria=Auditoria(settings.auditoria_log),
        origen=origen,
    )
