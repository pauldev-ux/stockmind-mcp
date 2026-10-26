"""Configuración de StockMind leída de variables de entorno y del archivo .env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/stockmind/config/settings.py -> raíz del proyecto.
# Claude Desktop lanza el servidor desde otra carpeta, así que nada puede
# depender del directorio de trabajo.
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Una variable vacía ("X=") cuenta como no definida, en vez de fallar al convertir.
        env_ignore_empty=True,
    )

    connector: str = "csv"

    data_dir: Path = Path("data")
    formato_datos: Literal["csv", "xlsx"] = "csv"

    confirmaciones_db: Path = Path("data/stockmind.db")
    token_ttl_minutos: int = 10

    auditoria_log: Path = Path("logs/auditoria.jsonl")

    # Archivos que genera exportar_pendientes (bandeja de salida) y los reportes
    exports_dir: Path = Path("exports")

    # Moneda de los productos que no informan una (los totales nunca mezclan monedas)
    moneda_base: str = "BOB"

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("data_dir", "confirmaciones_db", "auditoria_log", "exports_dir")
    @classmethod
    def _resolver_desde_raiz(cls, ruta: Path) -> Path:
        return ruta if ruta.is_absolute() else (PROJECT_ROOT / ruta).resolve()

    @field_validator("connector")
    @classmethod
    def _normalizar_conector(cls, valor: str) -> str:
        return valor.strip().lower()


@lru_cache
def get_settings() -> Settings:
    return Settings()
