"""Configuración del backend del chat (mismo .env que el resto de StockMind)."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from stockmind.config.settings import PROJECT_ROOT


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore",
                                      env_ignore_empty=True)

    # Proveedor de IA
    llm_provider: Literal["anthropic", "gemini", "falso"] = "anthropic"
    llm_model: str | None = None            # vacío = el modelo por defecto del proveedor
    llm_api_key: SecretStr = SecretStr("")
    # Cuánto "piensa" el modelo antes de responder: más bajo = más rápido y barato.
    llm_thinking: Literal["bajo", "medio", "alto", "apagado"] = "bajo"
    llm_fallbacks: bool = True              # solo Anthropic: fallback del lado del servidor
    llm_max_tokens: int = 16000
    # Si el modelo sigue saturado tras los reintentos: separados por coma. Vacío = la lista del
    # proveedor (Gemini: modelos del nivel gratuito con function calling); "ninguno" = sin respaldo.
    llm_modelos_respaldo: str | None = None
    llm_reintentos: int = Field(3, ge=0, le=6)       # sin respaldo: esperas de 2, 4, 8 s…; con respaldo, 1
    llm_espera_inicial: float = Field(2.0, gt=0, le=30)
    llm_enfriamiento_segundos: float = Field(300.0, ge=0)   # un modelo saturado no se usa durante este tiempo
    llm_timeout_segundos: float = Field(30.0, gt=0)         # máximo sin recibir datos del modelo

    # API
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_token: SecretStr = SecretStr("")
    cors_origins: str = ""                  # separados por coma, p. ej. http://localhost:5173
    max_iteraciones_tools: int = 8
    # Menos tokens por llamada (0 = sin límite): turnos de historial que se envían al modelo (los
    # anteriores van resumidos) y filas por lista en los resultados de las tools.
    historial_turnos: int = Field(8, ge=0)
    max_filas_resultado: int = Field(15, ge=0)
    max_largo_mensaje: int = 4000

    # Token firmado por el ERP (opcional): HS256 con secreto compartido
    erp_jwt_secreto: SecretStr = SecretStr("")
    erp_jwt_emisor: str | None = None
    erp_jwt_audiencia: str | None = None

    # Servidor MCP (stockmind-mcp --http)
    mcp_url: str = "http://127.0.0.1:8765/mcp"
    mcp_token: SecretStr = SecretStr("")

    # Historial del chat (misma base local que los tokens y la bandeja)
    confirmaciones_db: Path = Path("data/stockmind.db")

    @field_validator("confirmaciones_db")
    @classmethod
    def _resolver(cls, ruta: Path) -> Path:
        return ruta if ruta.is_absolute() else (PROJECT_ROOT / ruta).resolve()

    def parametros_proveedor(self) -> dict:
        """Argumentos de `crear_proveedor` (los usan la API y la consola)."""
        return {"modelo": self.llm_model, "api_key": self.llm_api_key.get_secret_value() or None,
                "thinking": self.llm_thinking, "fallbacks": self.llm_fallbacks, "max_tokens": self.llm_max_tokens,
                "modelos_respaldo": self.llm_modelos_respaldo, "reintentos": self.llm_reintentos,
                "espera_inicial": self.llm_espera_inicial, "enfriamiento": self.llm_enfriamiento_segundos,
                "timeout": self.llm_timeout_segundos}

    @property
    def origenes_cors(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
