"""Configuración de StockMind leída de variables de entorno y del archivo .env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
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
    )

    connector: str = "csv"

    data_dir: Path = Path("data")
    formato_datos: Literal["csv", "xlsx"] = "csv"

    confirmaciones_db: Path = Path("data/stockmind.db")
    token_ttl_minutos: int = 10

    auditoria_log: Path = Path("logs/auditoria.jsonl")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Conector Canaan360 (MySQL, solo lectura)
    canaan_db_host: str = "127.0.0.1"
    canaan_db_port: int = 3306
    canaan_db_name: str = "canaan_360"
    canaan_db_user: str | None = None
    canaan_db_password: SecretStr = SecretStr("")
    # Canaan360 es multiempresa: toda consulta filtra por esta empresa.
    canaan_empresa_id: int | None = None
    # true: no arranca si el usuario MySQL tiene privilegios de escritura; false: solo avisa.
    canaan_estricto: bool = False
    # Incluir en ventas_por_producto las ventas cargadas como históricas (es_historico = 1).
    canaan_incluir_historicas: bool = True

    @property
    def canaan_configurado(self) -> bool:
        return bool(self.canaan_db_user and self.canaan_empresa_id)

    @field_validator("data_dir", "confirmaciones_db", "auditoria_log")
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
