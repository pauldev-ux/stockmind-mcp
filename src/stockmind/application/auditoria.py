"""Log de auditoría de escrituras en formato JSONL (una línea JSON por evento)."""

import json
import threading
from pathlib import Path
from typing import Any

from stockmind.application.confirmaciones import Reloj, ahora_utc


class Auditoria:
    """Eventos de confirmación: preparada, confirmada, rechazada, cancelada.
    Eventos de la bandeja de salida: encolada, exportada, registrada, descartada.

    Del token solo se guarda un prefijo de su hash: alcanza para seguir una
    operación de punta a punta sin poder reutilizarlo.
    """

    def __init__(self, ruta: Path, reloj: Reloj = ahora_utc):
        self.ruta = Path(ruta)
        self.reloj = reloj
        self._lock = threading.Lock()

    def registrar(
        self,
        evento: str,
        operacion: str,
        *,
        conector: str,
        origen: str,
        datos: dict[str, Any] | None = None,
        resultado: dict[str, Any] | None = None,
        error: str | None = None,
        token_hash: str | None = None,
        referencia: str | None = None,
    ) -> dict[str, Any]:
        registro = {
            "ts": self.reloj().isoformat(timespec="seconds"),
            "evento": evento,
            "operacion": operacion,
            "conector": conector,
            "origen": origen,
            "token": token_hash[:12] if token_hash else None,
            "referencia": referencia,
            "datos": datos,
            "resultado": resultado,
            "error": error,
        }
        linea = json.dumps(registro, ensure_ascii=False, default=_serializar)
        with self._lock:
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
            with self.ruta.open("a", encoding="utf-8") as f:
                f.write(linea + "\n")
        return registro

    def leer(self) -> list[dict[str, Any]]:
        if not self.ruta.exists():
            return []
        with self.ruta.open(encoding="utf-8") as f:
            return [json.loads(linea) for linea in f if linea.strip()]


def _serializar(valor: Any) -> Any:
    if hasattr(valor, "isoformat"):  # date, datetime
        return valor.isoformat()
    return str(valor)
