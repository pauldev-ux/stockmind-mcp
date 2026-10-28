"""Confirmación en dos pasos de las operaciones de escritura.

1. La tool de escritura valida y guarda la operación pendiente; devuelve una
   vista previa y un token.
2. confirmar_operacion(token) la ejecuta.

Reglas: el token vence a los 10 minutos, se usa una sola vez y nunca se guarda
en claro (solo su SHA-256). El almacenamiento va detrás de ConfirmacionStore
para que el servidor MCP y el futuro backend web (otro proceso) compartan los
tokens en SQLite; en los tests se usa la versión en memoria.
"""

import hashlib
import json
import secrets
import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from stockmind.domain.errores import TokenInvalido

Reloj = Callable[[], datetime]


def ahora_utc() -> datetime:
    return datetime.now(UTC)


def exigir_zona(momento: datetime) -> datetime:
    """Todo instante se maneja con zona explícita y en UTC. Un datetime sin zona es un error de
    programación: `astimezone()` lo tomaría como hora local y correría el vencimiento 4 h en La Paz."""
    if momento.tzinfo is None or momento.utcoffset() is None:
        raise ValueError("Se esperaba un datetime con zona horaria (UTC), no uno sin zona.")
    return momento.astimezone(UTC)


def hora_local(momento: datetime) -> str:
    """Para mostrar al usuario: «14:45 (UTC-04:00)» en la zona del sistema."""
    local = exigir_zona(momento).astimezone()
    desfase = local.strftime("%z")
    return f"{local:%H:%M} (UTC{desfase[:3]}:{desfase[3:]})"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


class EstadoOperacion(StrEnum):
    PENDIENTE = "pendiente"
    USADA = "usada"
    CANCELADA = "cancelada"


@dataclass(frozen=True)
class OperacionPendiente:
    token_hash: str
    tipo: str
    datos: dict[str, Any]
    creada: datetime
    vence: datetime
    estado: EstadoOperacion = EstadoOperacion.PENDIENTE


class ConfirmacionStore(ABC):
    @abstractmethod
    def guardar(self, operacion: OperacionPendiente) -> None: ...

    @abstractmethod
    def tomar(self, token_hash: str, ahora: datetime, nuevo_estado: EstadoOperacion) -> OperacionPendiente | None:
        """De forma atómica: si la operación está pendiente y no venció, la pasa a
        `nuevo_estado` y la devuelve. Si no, devuelve None sin cambiar nada.

        Es lo que garantiza el uso único aunque dos procesos confirmen a la vez.
        """

    @abstractmethod
    def obtener(self, token_hash: str) -> OperacionPendiente | None: ...

    @abstractmethod
    def purgar(self, vencidas_antes_de: datetime) -> int:
        """Borra las operaciones que vencieron antes de la fecha dada."""


class MemoriaConfirmacionStore(ConfirmacionStore):
    def __init__(self) -> None:
        self._operaciones: dict[str, OperacionPendiente] = {}
        self._lock = threading.Lock()

    def guardar(self, operacion: OperacionPendiente) -> None:
        with self._lock:
            self._operaciones[operacion.token_hash] = operacion

    def tomar(self, token_hash: str, ahora: datetime, nuevo_estado: EstadoOperacion) -> OperacionPendiente | None:
        with self._lock:
            op = self._operaciones.get(token_hash)
            if op is None or op.estado != EstadoOperacion.PENDIENTE or op.vence <= ahora:
                return None
            self._operaciones[token_hash] = replace(op, estado=nuevo_estado)
            return op

    def obtener(self, token_hash: str) -> OperacionPendiente | None:
        with self._lock:
            return self._operaciones.get(token_hash)

    def purgar(self, vencidas_antes_de: datetime) -> int:
        with self._lock:
            viejas = [h for h, op in self._operaciones.items() if op.vence < vencidas_antes_de]
            for h in viejas:
                del self._operaciones[h]
            return len(viejas)


def _iso(momento: datetime) -> str:
    # Siempre UTC y con microsegundos: así las cadenas se comparan bien en SQL.
    return exigir_zona(momento).isoformat(timespec="microseconds")


def _leer_iso(texto: str) -> datetime:
    momento = datetime.fromisoformat(texto)
    # Filas sin zona (no deberían existir): se guardaron en UTC.
    return momento.replace(tzinfo=UTC) if momento.tzinfo is None else momento.astimezone(UTC)


class SqliteConfirmacionStore(ConfirmacionStore):
    _ESQUEMA = """
        CREATE TABLE IF NOT EXISTS confirmaciones (
            token_hash  TEXT PRIMARY KEY,
            tipo        TEXT NOT NULL,
            datos       TEXT NOT NULL,
            creada      TEXT NOT NULL,
            vence       TEXT NOT NULL,
            estado      TEXT NOT NULL DEFAULT 'pendiente',
            actualizada TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_confirmaciones_vence ON confirmaciones (vence);
    """

    def __init__(self, ruta: Path):
        self.ruta = Path(ruta)
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conectar()) as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(self._ESQUEMA)

    def _conectar(self) -> sqlite3.Connection:
        # isolation_level=None: autocommit; cada sentencia es su propia transacción.
        return sqlite3.connect(self.ruta, timeout=10, isolation_level=None)

    @staticmethod
    def _fila_a_operacion(token_hash: str, fila: tuple) -> OperacionPendiente:
        tipo, datos, creada, vence, estado = fila
        return OperacionPendiente(
            token_hash=token_hash, tipo=tipo, datos=json.loads(datos),
            creada=_leer_iso(creada), vence=_leer_iso(vence),
            estado=EstadoOperacion(estado),
        )

    def guardar(self, operacion: OperacionPendiente) -> None:
        with closing(self._conectar()) as con:
            con.execute(
                "INSERT INTO confirmaciones (token_hash, tipo, datos, creada, vence, estado) VALUES (?, ?, ?, ?, ?, ?)",
                (operacion.token_hash, operacion.tipo, json.dumps(operacion.datos, ensure_ascii=False),
                 _iso(operacion.creada), _iso(operacion.vence), operacion.estado.value),
            )

    def tomar(self, token_hash: str, ahora: datetime, nuevo_estado: EstadoOperacion) -> OperacionPendiente | None:
        with closing(self._conectar()) as con:
            filas = con.execute(
                """UPDATE confirmaciones SET estado = ?, actualizada = ?
                   WHERE token_hash = ? AND estado = 'pendiente' AND vence > ?
                   RETURNING tipo, datos, creada, vence, 'pendiente'""",
                (nuevo_estado.value, _iso(ahora), token_hash, _iso(ahora)),
            ).fetchall()
        return self._fila_a_operacion(token_hash, filas[0]) if filas else None

    def obtener(self, token_hash: str) -> OperacionPendiente | None:
        with closing(self._conectar()) as con:
            fila = con.execute(
                "SELECT tipo, datos, creada, vence, estado FROM confirmaciones WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return self._fila_a_operacion(token_hash, fila) if fila else None

    def purgar(self, vencidas_antes_de: datetime) -> int:
        with closing(self._conectar()) as con:
            return con.execute("DELETE FROM confirmaciones WHERE vence < ?", (_iso(vencidas_antes_de),)).rowcount


class GestorConfirmaciones:
    def __init__(self, store: ConfirmacionStore, ttl: timedelta = timedelta(minutes=10), reloj: Reloj = ahora_utc):
        self.store = store
        self.ttl = ttl
        self.reloj = reloj

    def emitir(self, tipo: str, datos: dict[str, Any]) -> tuple[str, OperacionPendiente]:
        """Guarda la operación pendiente y devuelve el token en claro (solo se muestra esta vez)."""
        ahora = exigir_zona(self.reloj())
        self.store.purgar(ahora - timedelta(days=1))
        token = secrets.token_urlsafe(24)
        operacion = OperacionPendiente(token_hash=hash_token(token), tipo=tipo, datos=datos,
                                       creada=ahora, vence=ahora + self.ttl)
        self.store.guardar(operacion)
        return token, operacion

    def consumir(self, token: str) -> OperacionPendiente:
        return self._tomar(token, EstadoOperacion.USADA)

    def cancelar(self, token: str) -> OperacionPendiente:
        return self._tomar(token, EstadoOperacion.CANCELADA)

    def _tomar(self, token: str, nuevo_estado: EstadoOperacion) -> OperacionPendiente:
        if not isinstance(token, str) or not token.strip():
            raise TokenInvalido("Falta el token de confirmación.")
        ahora = exigir_zona(self.reloj())
        token_hash = hash_token(token)
        operacion = self.store.tomar(token_hash, ahora, nuevo_estado)
        if operacion is not None:
            return operacion
        raise TokenInvalido(self._motivo_rechazo(token_hash, ahora))

    def _motivo_rechazo(self, token_hash: str, ahora: datetime) -> str:
        operacion = self.store.obtener(token_hash)
        if operacion is None:
            return "El token no existe. Prepara la operación de nuevo."
        if operacion.estado == EstadoOperacion.USADA:
            return "Esta operación ya fue confirmada; cada token se usa una sola vez."
        if operacion.estado == EstadoOperacion.CANCELADA:
            return "Esta operación fue cancelada."
        minutos = int(self.ttl.total_seconds() // 60)
        return (f"El token venció a las {hora_local(operacion.vence)} (dura {minutos} minutos). "
                "Prepara la operación de nuevo para ver una vista previa actualizada.")
