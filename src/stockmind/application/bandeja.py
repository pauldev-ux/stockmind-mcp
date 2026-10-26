"""Bandeja de salida: operaciones confirmadas que quedan PENDIENTES de registrar en el ERP.

Se usa con conectores en modo BANDEJA (ERP de solo lectura). Cada operación
pasa de `pendiente` a `registrada` o `descartada`, una sola vez, con fecha y
observación. El almacenamiento va detrás de BandejaStore: SQLite en
data/stockmind.db (compartido entre procesos) o memoria para los tests.
"""

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font

from stockmind.domain.bandeja import Coincidencia, EstadoBandeja, OperacionBandeja
from stockmind.domain.texto import normalizar
from stockmind.ports.bandeja import IntegracionBandeja

__all__ = ["BandejaStore", "IntegracionGenerica", "MemoriaBandejaStore", "SqliteBandejaStore", "normalizar"]


class BandejaStore(ABC):
    @abstractmethod
    def agregar(self, operacion: OperacionBandeja) -> OperacionBandeja:
        """Guarda una operación nueva (se ignora su id) y la devuelve con el id asignado."""

    @abstractmethod
    def obtener(self, id_operacion: int) -> OperacionBandeja | None: ...

    @abstractmethod
    def listar(self, estado: EstadoBandeja | None = None, tipo: str | None = None) -> list[OperacionBandeja]:
        """En orden de creación."""

    @abstractmethod
    def actualizar_guia(self, id_operacion: int, guia: list[str]) -> None: ...

    @abstractmethod
    def transicionar(
        self, id_operacion: int, nuevo_estado: EstadoBandeja, ahora: datetime, observacion: str | None,
        registrada_por: str | None = None, id_externo: str | None = None,
    ) -> OperacionBandeja | None:
        """De forma atómica: si sigue pendiente, la pasa a `nuevo_estado` y la devuelve.
        Si no está pendiente (o no existe), no cambia nada y devuelve None."""

    @abstractmethod
    def marcar_exportadas(self, ids: list[int], ahora: datetime, archivo: str) -> None: ...


class MemoriaBandejaStore(BandejaStore):
    def __init__(self) -> None:
        self._operaciones: dict[int, OperacionBandeja] = {}
        self._siguiente = 1
        self._lock = threading.Lock()

    def agregar(self, operacion: OperacionBandeja) -> OperacionBandeja:
        with self._lock:
            guardada = operacion.model_copy(update={"id": self._siguiente}, deep=True)
            self._operaciones[guardada.id] = guardada
            self._siguiente += 1
            return guardada.model_copy(deep=True)

    def obtener(self, id_operacion: int) -> OperacionBandeja | None:
        with self._lock:
            op = self._operaciones.get(id_operacion)
            return op.model_copy(deep=True) if op else None

    def listar(self, estado: EstadoBandeja | None = None, tipo: str | None = None) -> list[OperacionBandeja]:
        with self._lock:
            return [op.model_copy(deep=True) for op in self._operaciones.values()
                    if (estado is None or op.estado == estado) and (tipo is None or op.tipo == tipo)]

    def actualizar_guia(self, id_operacion: int, guia: list[str]) -> None:
        with self._lock:
            self._operaciones[id_operacion].guia = list(guia)

    def transicionar(self, id_operacion, nuevo_estado, ahora, observacion, registrada_por=None, id_externo=None):
        with self._lock:
            op = self._operaciones.get(id_operacion)
            if op is None or op.estado != EstadoBandeja.PENDIENTE:
                return None
            op.estado, op.actualizada_en, op.observacion = nuevo_estado, ahora, observacion
            op.registrada_por, op.id_externo = registrada_por, id_externo
            return op.model_copy(deep=True)

    def marcar_exportadas(self, ids: list[int], ahora: datetime, archivo: str) -> None:
        with self._lock:
            for i in ids:
                self._operaciones[i].exportada_en, self._operaciones[i].archivo = ahora, archivo


def _iso(momento: datetime | None) -> str | None:
    return momento.astimezone(UTC).isoformat(timespec="microseconds") if momento else None


def _fecha(texto: str | None) -> datetime | None:
    return datetime.fromisoformat(texto) if texto else None


class SqliteBandejaStore(BandejaStore):
    _ESQUEMA = """
        CREATE TABLE IF NOT EXISTS bandeja_operaciones (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo           TEXT NOT NULL,
            conector       TEXT NOT NULL,
            origen         TEXT NOT NULL,
            datos          TEXT NOT NULL,
            vista_previa   TEXT NOT NULL,
            detalle_erp    TEXT NOT NULL,
            guia           TEXT NOT NULL,
            estado         TEXT NOT NULL DEFAULT 'pendiente',
            creada_en      TEXT NOT NULL,
            actualizada_en TEXT NOT NULL,
            observacion    TEXT,
            registrada_por TEXT,
            id_externo     TEXT,
            exportada_en   TEXT,
            archivo        TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_bandeja_estado ON bandeja_operaciones (estado, tipo);
    """
    _COLUMNAS = ("id, tipo, conector, origen, datos, vista_previa, detalle_erp, guia, estado, creada_en, "
                 "actualizada_en, observacion, registrada_por, id_externo, exportada_en, archivo")

    def __init__(self, ruta: Path):
        self.ruta = Path(ruta)
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conectar()) as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(self._ESQUEMA)

    def _conectar(self) -> sqlite3.Connection:
        return sqlite3.connect(self.ruta, timeout=10, isolation_level=None)

    @staticmethod
    def _desde_fila(fila: tuple) -> OperacionBandeja:
        (id_, tipo, conector, origen, datos, vista, detalle, guia, estado, creada, actualizada,
         observacion, registrada_por, id_externo, exportada, archivo) = fila
        return OperacionBandeja(
            id=id_, tipo=tipo, conector=conector, origen=origen, datos=json.loads(datos),
            vista_previa=json.loads(vista), detalle_erp=json.loads(detalle), guia=json.loads(guia),
            estado=EstadoBandeja(estado), creada_en=_fecha(creada), actualizada_en=_fecha(actualizada),
            observacion=observacion, registrada_por=registrada_por, id_externo=id_externo,
            exportada_en=_fecha(exportada), archivo=archivo,
        )

    def agregar(self, operacion: OperacionBandeja) -> OperacionBandeja:
        def js(valor: Any) -> str:
            return json.dumps(valor, ensure_ascii=False, default=str)

        with closing(self._conectar()) as con:
            (id_,) = con.execute(
                """INSERT INTO bandeja_operaciones
                   (tipo, conector, origen, datos, vista_previa, detalle_erp, guia, estado, creada_en, actualizada_en)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (operacion.tipo, operacion.conector, operacion.origen, js(operacion.datos),
                 js(operacion.vista_previa), js(operacion.detalle_erp), js(operacion.guia),
                 operacion.estado.value, _iso(operacion.creada_en), _iso(operacion.actualizada_en)),
            ).fetchone()
        return self.obtener(id_)

    def obtener(self, id_operacion: int) -> OperacionBandeja | None:
        with closing(self._conectar()) as con:
            fila = con.execute(f"SELECT {self._COLUMNAS} FROM bandeja_operaciones WHERE id = ?",
                               (id_operacion,)).fetchone()
        return self._desde_fila(fila) if fila else None

    def listar(self, estado: EstadoBandeja | None = None, tipo: str | None = None) -> list[OperacionBandeja]:
        with closing(self._conectar()) as con:
            filas = con.execute(
                f"""SELECT {self._COLUMNAS} FROM bandeja_operaciones
                    WHERE (? IS NULL OR estado = ?) AND (? IS NULL OR tipo = ?) ORDER BY id""",
                (estado and estado.value, estado and estado.value, tipo, tipo),
            ).fetchall()
        return [self._desde_fila(f) for f in filas]

    def actualizar_guia(self, id_operacion: int, guia: list[str]) -> None:
        with closing(self._conectar()) as con:
            con.execute("UPDATE bandeja_operaciones SET guia = ? WHERE id = ?",
                        (json.dumps(guia, ensure_ascii=False), id_operacion))

    def transicionar(self, id_operacion, nuevo_estado, ahora, observacion, registrada_por=None, id_externo=None):
        with closing(self._conectar()) as con:
            filas = con.execute(
                f"""UPDATE bandeja_operaciones
                    SET estado = ?, actualizada_en = ?, observacion = ?, registrada_por = ?, id_externo = ?
                    WHERE id = ? AND estado = 'pendiente'
                    RETURNING {self._COLUMNAS}""",
                (nuevo_estado.value, _iso(ahora), observacion, registrada_por, id_externo, id_operacion),
            ).fetchall()
        return self._desde_fila(filas[0]) if filas else None

    def marcar_exportadas(self, ids: list[int], ahora: datetime, archivo: str) -> None:
        with closing(self._conectar()) as con:
            con.executemany("UPDATE bandeja_operaciones SET exportada_en = ?, archivo = ? WHERE id = ?",
                            [(_iso(ahora), archivo, i) for i in ids])


class IntegracionGenerica(IntegracionBandeja):
    """Para conectores en modo bandeja que no traen una integración propia:
    exporta un Excel simple con los datos, da una guía genérica y no concilia."""

    def __init__(self, nombre_erp: str):
        self.nombre_erp = nombre_erp

    def guia(self, operacion: OperacionBandeja) -> list[str]:
        que = "el producto" if operacion.tipo == "crear_producto" else "el movimiento"
        return [
            "Exporta las operaciones pendientes con exportar_pendientes para tener los datos en Excel.",
            f"Registra {que} en {self.nombre_erp} con los datos de la vista previa.",
            f"Anota la referencia {operacion.referencia} en las observaciones del registro, si el ERP lo permite.",
            f"Cuando quede registrado, marca {operacion.referencia} con marcar_registrada.",
        ]

    def exportar(self, operaciones: list[OperacionBandeja], tipo: str, carpeta: Path,
                 momento: datetime) -> list[Path]:
        claves: list[str] = []
        for op in operaciones:
            claves.extend(k for k in op.datos if k not in claves)
        libro = Workbook()
        hoja = libro.active
        hoja.title = "Pendientes"
        hoja.append(["referencia", "creada_en", *claves])
        for op in operaciones:
            hoja.append([op.referencia, op.creada_en.astimezone().strftime("%Y-%m-%d %H:%M"),
                         *[_celda(op.datos.get(k)) for k in claves]])
        guia = libro.create_sheet("Guía")
        guia.append(["referencia", "paso", "instrucción"])
        for op in operaciones:
            for n, paso in enumerate(op.guia, 1):
                guia.append([op.referencia, n, paso])
        for h in (hoja, guia):
            for celda in h[1]:
                celda.font = Font(bold=True)
        ruta = Path(carpeta) / f"{tipo}_pendientes_{momento.astimezone().strftime('%Y%m%d_%H%M%S')}.xlsx"
        ruta.parent.mkdir(parents=True, exist_ok=True)
        libro.save(ruta)
        return [ruta]

    def verificar(self, operacion: OperacionBandeja) -> list[Coincidencia]:
        return []


def _celda(valor: Any) -> Any:
    if isinstance(valor, (dict, list)):
        return json.dumps(valor, ensure_ascii=False)
    return valor
