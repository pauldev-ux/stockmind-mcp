"""Historial del chat en SQLite (data/stockmind.db): conversaciones, mensajes, uso de tokens y
acciones pendientes de confirmación.

De cada acción pendiente se guarda solo el SHA-256 del token: alcanza para comprobar que el
token que manda el frontend pertenece a esa conversación y a ese usuario.
"""

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from stockmind.agente.modelos import Mensaje, Uso


def _ahora() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


class Conversacion(BaseModel):
    id: str
    usuario: str
    titulo: str
    creada: datetime
    actualizada: datetime


class Accion(BaseModel):
    id: str
    conversacion_id: str
    usuario: str
    operacion: str
    estado: str                      # pendiente | procesando | confirmada | cancelada | fallida
    resumen: str
    datos: dict[str, Any]
    creada: datetime
    resuelta: datetime | None = None
    resultado: dict[str, Any] | None = None


class HistorialStore:
    _ESQUEMA = """
        CREATE TABLE IF NOT EXISTS chat_conversaciones (
            id TEXT PRIMARY KEY, usuario TEXT NOT NULL, titulo TEXT NOT NULL,
            creada TEXT NOT NULL, actualizada TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_chat_conv_usuario ON chat_conversaciones (usuario, actualizada);
        CREATE TABLE IF NOT EXISTS chat_mensajes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, conversacion_id TEXT NOT NULL,
            mensaje TEXT NOT NULL, creado TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_chat_mensajes ON chat_mensajes (conversacion_id, id);
        CREATE TABLE IF NOT EXISTS chat_uso_llm (
            id INTEGER PRIMARY KEY AUTOINCREMENT, conversacion_id TEXT NOT NULL, proveedor TEXT NOT NULL,
            modelo TEXT NOT NULL, entrada INTEGER NOT NULL, salida INTEGER NOT NULL,
            cache_lectura INTEGER NOT NULL, cache_escritura INTEGER NOT NULL, creado TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chat_acciones (
            id TEXT PRIMARY KEY, conversacion_id TEXT NOT NULL, usuario TEXT NOT NULL, operacion TEXT NOT NULL,
            token_hash TEXT NOT NULL, estado TEXT NOT NULL, resumen TEXT NOT NULL, datos TEXT NOT NULL,
            creada TEXT NOT NULL, resuelta TEXT, resultado TEXT);
        CREATE INDEX IF NOT EXISTS ix_chat_acciones ON chat_acciones (conversacion_id, token_hash);
    """

    def __init__(self, ruta: Path):
        self.ruta = Path(ruta)
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conectar()) as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(self._ESQUEMA)

    def _conectar(self) -> sqlite3.Connection:
        return sqlite3.connect(self.ruta, timeout=10, isolation_level=None)

    # ---- conversaciones

    def crear(self, usuario: str, titulo: str) -> Conversacion:
        ahora = _ahora()
        conversacion = Conversacion(id=uuid.uuid4().hex, usuario=usuario, titulo=titulo.strip()[:80] or "Conversación",
                                    creada=ahora, actualizada=ahora)
        with closing(self._conectar()) as con:
            con.execute("INSERT INTO chat_conversaciones VALUES (?, ?, ?, ?, ?)",
                        (conversacion.id, usuario, conversacion.titulo, ahora, ahora))
        return conversacion

    def obtener(self, usuario: str, conversacion_id: str) -> Conversacion | None:
        """Solo devuelve la conversación si es de ese usuario."""
        with closing(self._conectar()) as con:
            fila = con.execute("SELECT id, usuario, titulo, creada, actualizada FROM chat_conversaciones "
                               "WHERE id = ? AND usuario = ?", (conversacion_id, usuario)).fetchone()
        return Conversacion(**dict(zip(("id", "usuario", "titulo", "creada", "actualizada"), fila))) if fila else None

    def listar(self, usuario: str) -> list[Conversacion]:
        with closing(self._conectar()) as con:
            filas = con.execute("SELECT id, usuario, titulo, creada, actualizada FROM chat_conversaciones "
                                "WHERE usuario = ? ORDER BY actualizada DESC", (usuario,)).fetchall()
        return [Conversacion(**dict(zip(("id", "usuario", "titulo", "creada", "actualizada"), f))) for f in filas]

    def borrar(self, usuario: str, conversacion_id: str) -> bool:
        with closing(self._conectar()) as con:
            borradas = con.execute("DELETE FROM chat_conversaciones WHERE id = ? AND usuario = ?",
                                   (conversacion_id, usuario)).rowcount
            if borradas:
                for tabla in ("chat_mensajes", "chat_uso_llm", "chat_acciones"):
                    con.execute(f"DELETE FROM {tabla} WHERE conversacion_id = ?", (conversacion_id,))
        return bool(borradas)

    # ---- mensajes y uso

    def agregar(self, conversacion_id: str, mensaje: Mensaje) -> None:
        ahora = _ahora()
        with closing(self._conectar()) as con:
            con.execute("INSERT INTO chat_mensajes (conversacion_id, mensaje, creado) VALUES (?, ?, ?)",
                        (conversacion_id, mensaje.model_dump_json(), ahora))
            con.execute("UPDATE chat_conversaciones SET actualizada = ? WHERE id = ?", (ahora, conversacion_id))

    def mensajes(self, conversacion_id: str) -> list[Mensaje]:
        with closing(self._conectar()) as con:
            filas = con.execute("SELECT mensaje FROM chat_mensajes WHERE conversacion_id = ? ORDER BY id",
                                (conversacion_id,)).fetchall()
        return [Mensaje.model_validate_json(f[0]) for f in filas]

    def registrar_uso(self, conversacion_id: str, uso: Uso) -> None:
        with closing(self._conectar()) as con:
            con.execute("INSERT INTO chat_uso_llm (conversacion_id, proveedor, modelo, entrada, salida, cache_lectura, "
                        "cache_escritura, creado) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (conversacion_id, uso.proveedor, uso.modelo, uso.entrada, uso.salida, uso.cache_lectura,
                         uso.cache_escritura, _ahora()))

    def uso(self, conversacion_id: str) -> list[dict[str, Any]]:
        """Totales por proveedor y modelo."""
        with closing(self._conectar()) as con:
            filas = con.execute(
                "SELECT proveedor, modelo, COUNT(*), SUM(entrada), SUM(salida), SUM(cache_lectura), SUM(cache_escritura) "
                "FROM chat_uso_llm WHERE conversacion_id = ? GROUP BY proveedor, modelo", (conversacion_id,)).fetchall()
        return [dict(zip(("proveedor", "modelo", "llamadas", "entrada", "salida", "cache_lectura", "cache_escritura"), f))
                for f in filas]

    # ---- acciones pendientes (human in the loop)

    def registrar_accion(self, conversacion_id: str, usuario: str, operacion: str, token: str, resumen: str,
                         datos: dict[str, Any]) -> Accion:
        accion = Accion(id=uuid.uuid4().hex, conversacion_id=conversacion_id, usuario=usuario, operacion=operacion,
                        estado="pendiente", resumen=resumen, datos=datos, creada=_ahora())
        with closing(self._conectar()) as con:
            con.execute("INSERT INTO chat_acciones (id, conversacion_id, usuario, operacion, token_hash, estado, resumen, "
                        "datos, creada) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (accion.id, conversacion_id, usuario, operacion, hash_token(token), accion.estado, resumen,
                         json.dumps(datos, ensure_ascii=False, default=str), accion.creada.isoformat()))
        return accion

    def tomar_accion(self, conversacion_id: str, usuario: str, token: str) -> Accion | None:
        """De forma atómica: si hay una acción pendiente con ese token en esa conversación y de ese
        usuario, la pasa a `procesando` y la devuelve. Si no, None (ya se resolvió, o no existe)."""
        with closing(self._conectar()) as con:
            filas = con.execute(
                "UPDATE chat_acciones SET estado = 'procesando' WHERE conversacion_id = ? AND usuario = ? "
                "AND token_hash = ? AND estado = 'pendiente' "
                "RETURNING id, conversacion_id, usuario, operacion, estado, resumen, datos, creada",
                (conversacion_id, usuario, hash_token(token))).fetchall()
        if not filas:
            return None
        f = filas[0]
        return Accion(id=f[0], conversacion_id=f[1], usuario=f[2], operacion=f[3], estado=f[4], resumen=f[5],
                      datos=json.loads(f[6]), creada=f[7])

    def resolver_accion(self, accion_id: str, estado: str, resultado: dict[str, Any]) -> None:
        with closing(self._conectar()) as con:
            con.execute("UPDATE chat_acciones SET estado = ?, resuelta = ?, resultado = ? WHERE id = ?",
                        (estado, _ahora(), json.dumps(resultado, ensure_ascii=False, default=str), accion_id))

    def acciones(self, conversacion_id: str) -> list[Accion]:
        with closing(self._conectar()) as con:
            filas = con.execute("SELECT id, conversacion_id, usuario, operacion, estado, resumen, datos, creada, resuelta, "
                                "resultado FROM chat_acciones WHERE conversacion_id = ? ORDER BY creada",
                                (conversacion_id,)).fetchall()
        return [Accion(id=f[0], conversacion_id=f[1], usuario=f[2], operacion=f[3], estado=f[4], resumen=f[5],
                       datos=json.loads(f[6]), creada=f[7], resuelta=f[8],
                       resultado=json.loads(f[9]) if f[9] else None) for f in filas]
