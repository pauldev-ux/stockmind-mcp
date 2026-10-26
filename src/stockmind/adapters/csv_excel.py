"""Adaptador que usa archivos .csv o .xlsx como "ERP".

Tablas (una por archivo en DATA_DIR):
  productos, movimientos, ventas, ventas_detalle

Todo se lee como texto y se convierte aquí, para tolerar archivos editados a
mano en Excel. Las escrituras son atómicas por archivo (temporal + os.replace)
y se serializan entre procesos con un archivo de bloqueo, porque el servidor
MCP y el futuro backend web pueden escribir a la vez.
"""

import logging
import os
import re
import time
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Literal

import pandas as pd

from stockmind.domain.errores import (
    CodigoAmbiguo,
    ErrorAlmacenamiento,
    ErrorValidacion,
    ProductoNoEncontrado,
    StockInsuficiente,
)
from stockmind.domain.modelos import (
    CampoRequerido,
    ErrorCampo,
    MovimientoInventario,
    Producto,
    ProductoNuevo,
    ResumenMovimientosProducto,
    ResumenVentasProducto,
)
from stockmind.ports.inventario import InventarioConnector

log = logging.getLogger(__name__)

Formato = Literal["csv", "xlsx"]

COLUMNAS: dict[str, list[str]] = {
    "productos": [
        "id", "codigo", "nombre", "categoria", "unidad", "precio_venta", "costo",
        "stock_actual", "stock_minimo", "proveedor", "almacen", "activo",
    ],
    "movimientos": [
        "id", "producto_id", "tipo", "sentido", "cantidad", "fecha", "motivo", "saldo",
        "almacen", "lote", "fecha_vencimiento", "costo_unitario", "documento",
    ],
    "ventas": ["id", "fecha", "cliente", "almacen", "total"],
    "ventas_detalle": ["venta_id", "producto_id", "cantidad", "precio_unitario", "subtotal"],
}

ALMACEN_POR_DEFECTO = "Principal"
_EPS = 1e-9


# --------------------------------------------------------------------------- E/S

def ruta_tabla(data_dir: Path, tabla: str, formato: Formato) -> Path:
    return Path(data_dir) / f"{tabla}.{formato}"


def leer_tabla(ruta: Path, tabla: str) -> pd.DataFrame:
    """Lee una tabla completa como texto. Si el archivo no existe, devuelve una tabla vacía."""
    columnas = COLUMNAS[tabla]
    if not ruta.exists():
        return pd.DataFrame({c: pd.Series(dtype=str) for c in columnas})
    try:
        if ruta.suffix == ".xlsx":
            df = pd.read_excel(ruta, dtype=str, keep_default_na=False, engine="openpyxl")
        else:
            df = pd.read_csv(ruta, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except PermissionError as e:
        raise ErrorAlmacenamiento(f"No se pudo leer {ruta.name}: está bloqueado por otro programa.") from e
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df.reindex(columns=columnas).fillna("").astype(str)


def escribir_tabla(df: pd.DataFrame, ruta: Path, tabla: str) -> None:
    """Escribe la tabla de forma atómica: primero un temporal y luego os.replace."""
    df = df.reindex(columns=COLUMNAS[tabla], fill_value="").astype(object).fillna("")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_name(f".{ruta.stem}.tmp{ruta.suffix}")
    if ruta.suffix == ".xlsx":
        df.to_excel(temporal, index=False, engine="openpyxl")
    else:
        df.to_csv(temporal, index=False, encoding="utf-8-sig")
    try:
        os.replace(temporal, ruta)
    except PermissionError as e:
        temporal.unlink(missing_ok=True)
        raise ErrorAlmacenamiento(
            f"No se pudo guardar {ruta.name}: el archivo está abierto en otro programa (¿Excel?). "
            "Ciérralo e intenta de nuevo."
        ) from e


# --------------------------------------------------------------- conversiones

def fmt_num(valor: float | None) -> str:
    """Número como texto, sin ceros de sobra (12.5, no 12.5000)."""
    if valor is None:
        return ""
    texto = f"{valor:.4f}".rstrip("0").rstrip(".")
    return "0" if texto in ("-0", "") else texto


def _num(texto: str, defecto: float | None = None) -> float | None:
    texto = texto.strip().replace(",", ".")
    if not texto:
        return defecto
    try:
        return float(texto)
    except ValueError:
        return defecto


def _txt(texto: str) -> str | None:
    texto = texto.strip()
    return texto or None


def _fecha(texto: str) -> date | None:
    texto = texto.strip()
    return date.fromisoformat(texto[:10]) if texto else None


def _bool(texto: str) -> bool:
    return texto.strip().lower() not in ("0", "false", "no", "falso", "n")


def normalizar(texto: str) -> str:
    """Minúsculas y sin tildes, para buscar y comparar nombres."""
    sin_tildes = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sin_tildes if not unicodedata.combining(c)).casefold().strip()


def _numero_de_id(texto: str) -> int:
    digitos = re.findall(r"\d+", texto)
    return int(digitos[-1]) if digitos else 0


def siguiente_id(ids: pd.Series, prefijo: str, ancho: int) -> str:
    mayor = max((_numero_de_id(i) for i in ids), default=0)
    return f"{prefijo}{mayor + 1:0{ancho}d}"


def _fila_a_producto(fila: dict[str, str]) -> Producto:
    return Producto(
        id=fila["id"],
        codigo=_txt(fila["codigo"]),
        nombre=fila["nombre"],
        categoria=_txt(fila["categoria"]),
        unidad=_txt(fila["unidad"]),
        precio_venta=_num(fila["precio_venta"]),
        costo=_num(fila["costo"]),
        stock_actual=_num(fila["stock_actual"], 0.0),
        stock_minimo=_num(fila["stock_minimo"], 0.0),
        proveedor=_txt(fila["proveedor"]),
        almacen=_txt(fila["almacen"]),
        activo=_bool(fila["activo"]),
    )


def _fila_a_movimiento(fila: dict[str, str]) -> MovimientoInventario:
    return MovimientoInventario(
        id=fila["id"],
        producto_id=fila["producto_id"],
        tipo=fila["tipo"],
        sentido=_txt(fila["sentido"]),
        cantidad=_num(fila["cantidad"]),
        fecha=_fecha(fila["fecha"]),
        motivo=_txt(fila["motivo"]),
        saldo=_num(fila["saldo"]),
        almacen=_txt(fila["almacen"]),
        lote=_txt(fila["lote"]),
        fecha_vencimiento=_fecha(fila["fecha_vencimiento"]),
        costo_unitario=_num(fila["costo_unitario"]),
        documento=_txt(fila["documento"]),
    )


# ----------------------------------------------------------------- conector

class CsvExcelConnector(InventarioConnector):
    nombre = "csv"
    nombre_visible = "CSV/Excel"

    def __init__(self, data_dir: Path, formato: Formato = "csv"):
        self.data_dir = Path(data_dir)
        self.formato = formato
        self._cache: dict[str, tuple[tuple[int, int], pd.DataFrame]] = {}
        if not self._ruta("productos").exists():
            log.warning(
                "No existe %s. Genera datos con: python scripts/generar_datos.py", self._ruta("productos")
            )

    # ---- infraestructura

    def _ruta(self, tabla: str) -> Path:
        return ruta_tabla(self.data_dir, tabla, self.formato)

    def _leer(self, tabla: str) -> pd.DataFrame:
        """Lee con caché: solo vuelve a leer el archivo si cambió (mtime o tamaño)."""
        ruta = self._ruta(tabla)
        try:
            st = ruta.stat()
            clave = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            clave = (0, 0)
        guardado = self._cache.get(tabla)
        if guardado and guardado[0] == clave:
            return guardado[1]
        df = leer_tabla(ruta, tabla)
        self._cache[tabla] = (clave, df)
        return df

    def _escribir(self, tabla: str, df: pd.DataFrame) -> None:
        escribir_tabla(df, self._ruta(tabla), tabla)
        self._cache.pop(tabla, None)

    @contextmanager
    def _bloqueo(self, espera_s: float = 10.0, huerfano_s: float = 60.0) -> Iterator[None]:
        """Exclusión mutua entre procesos con un archivo .lock creado en modo exclusivo."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        ruta = self.data_dir / ".stockmind.lock"
        inicio = time.monotonic()
        while True:
            try:
                fd = os.open(ruta, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - ruta.stat().st_mtime > huerfano_s:
                        ruta.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() - inicio > espera_s:
                    raise ErrorAlmacenamiento("Otro proceso está escribiendo los datos; intenta de nuevo.")
                time.sleep(0.05)
        try:
            yield
        finally:
            os.close(fd)
            ruta.unlink(missing_ok=True)

    def _productos(self) -> list[Producto]:
        return [_fila_a_producto(f) for f in self._leer("productos").to_dict("records")]

    # ---- lectura

    def buscar_productos(self, texto: str, limite: int = 20) -> list[Producto]:
        palabras = normalizar(texto).split()
        resultados: list[tuple[int, str, Producto]] = []
        for p in self._productos():
            codigo = normalizar(p.codigo or "")
            nombre = normalizar(p.nombre)
            pajar = " ".join([codigo, normalizar(p.id), nombre, normalizar(p.categoria or ""),
                              normalizar(p.proveedor or "")])
            if not all(w in pajar for w in palabras):
                continue
            consulta = " ".join(palabras)
            prioridad = 0 if consulta and consulta in (codigo, normalizar(p.id)) else (
                1 if nombre.startswith(consulta) else 2)
            resultados.append((prioridad, nombre, p))
        resultados.sort(key=lambda r: (r[0], r[1]))
        return [p for _, _, p in resultados[: max(limite, 0)]]

    def obtener_producto(self, id_o_codigo: str) -> Producto:
        clave = id_o_codigo.strip()
        productos = self._productos()
        for p in productos:
            if p.id == clave:
                return p
        por_codigo = [p for p in productos if p.codigo and normalizar(p.codigo) == normalizar(clave)]
        if len(por_codigo) == 1:
            return por_codigo[0]
        if len(por_codigo) > 1:
            raise CodigoAmbiguo(clave, [{"id": p.id, "nombre": p.nombre} for p in por_codigo])
        raise ProductoNoEncontrado(clave)

    def listar_bajo_stock(self) -> list[Producto]:
        bajos = [p for p in self._productos() if p.activo and p.bajo_stock]
        return sorted(bajos, key=lambda p: p.stock_actual / p.stock_minimo if p.stock_minimo else 0)

    def listar_movimientos(
        self, producto_id: str, desde: date | None = None, hasta: date | None = None
    ) -> list[MovimientoInventario]:
        df = self._leer("movimientos")
        df = df[df["producto_id"] == producto_id]
        if desde:
            df = df[df["fecha"].str[:10] >= desde.isoformat()]
        if hasta:
            df = df[df["fecha"].str[:10] <= hasta.isoformat()]
        movimientos = [_fila_a_movimiento(f) for f in df.to_dict("records")]
        return sorted(movimientos, key=lambda m: (m.fecha, _numero_de_id(m.id or "")))

    def listar_productos(self) -> list[Producto]:
        return self._productos()

    def resumen_movimientos(
        self, desde: date | None = None, hasta: date | None = None, almacen: str | None = None
    ) -> list[ResumenMovimientosProducto]:
        df = self._leer("movimientos")
        fechas = df["fecha"].str[:10]
        filtro = pd.Series(True, index=df.index)
        if desde:
            filtro &= fechas >= desde.isoformat()
        if hasta:
            filtro &= fechas <= hasta.isoformat()
        if almacen:
            filtro &= df["almacen"].map(normalizar) == normalizar(almacen)
        df, fechas = df[filtro], fechas[filtro]
        if df.empty:
            return []
        cantidad = pd.to_numeric(df["cantidad"].str.replace(",", "."), errors="coerce").fillna(0)
        entra = df["sentido"].str.strip() == "+"
        tabla = pd.DataFrame({
            "producto_id": df["producto_id"],
            "entradas": cantidad.where(entra, 0.0),
            "salidas": cantidad.where(~entra, 0.0),
            "fecha_entrada": fechas.where(entra),
            "fecha_salida": fechas.where(~entra),
        })
        agrupado = tabla.groupby("producto_id").agg(
            entradas=("entradas", "sum"), salidas=("salidas", "sum"), numero=("entradas", "size"),
            ultima_entrada=("fecha_entrada", "max"), ultima_salida=("fecha_salida", "max"),
        )
        return [
            ResumenMovimientosProducto(
                producto_id=pid,
                entradas=round(float(f.entradas), 4),
                salidas=round(float(f.salidas), 4),
                numero_movimientos=int(f.numero),
                ultima_entrada=_fecha(f.ultima_entrada) if isinstance(f.ultima_entrada, str) else None,
                ultima_salida=_fecha(f.ultima_salida) if isinstance(f.ultima_salida, str) else None,
            )
            for pid, f in agrupado.iterrows()
        ]

    def ventas_por_producto(
        self, desde: date | None = None, hasta: date | None = None, almacen: str | None = None
    ) -> list[ResumenVentasProducto]:
        ventas = self._leer("ventas")[["id", "fecha", "almacen"]]
        if desde:
            ventas = ventas[ventas["fecha"].str[:10] >= desde.isoformat()]
        if hasta:
            ventas = ventas[ventas["fecha"].str[:10] <= hasta.isoformat()]
        if almacen:
            ventas = ventas[ventas["almacen"].map(normalizar) == normalizar(almacen)]
        detalle = self._leer("ventas_detalle").merge(ventas[["id", "fecha"]], left_on="venta_id", right_on="id")
        if detalle.empty:
            return []
        detalle = detalle.assign(
            cantidad=pd.to_numeric(detalle["cantidad"].str.replace(",", "."), errors="coerce").fillna(0),
            subtotal=pd.to_numeric(detalle["subtotal"].str.replace(",", "."), errors="coerce").fillna(0),
        )
        agrupado = detalle.groupby("producto_id").agg(
            cantidad_total=("cantidad", "sum"),
            monto_total=("subtotal", "sum"),
            numero_ventas=("venta_id", "nunique"),
        )
        nombres = {p.id: p for p in self._productos()}
        resumen = [
            ResumenVentasProducto(
                producto_id=pid,
                codigo=nombres[pid].codigo if pid in nombres else None,
                nombre=nombres[pid].nombre if pid in nombres else f"(producto {pid} no existe)",
                cantidad_total=round(float(fila.cantidad_total), 4),
                monto_total=round(float(fila.monto_total), 2),
                numero_ventas=int(fila.numero_ventas),
            )
            for pid, fila in agrupado.iterrows()
        ]
        return sorted(resumen, key=lambda r: -r.cantidad_total)

    def campos_obligatorios_producto(self) -> list[CampoRequerido]:
        return [
            CampoRequerido(nombre="nombre", tipo="texto", descripcion="Nombre del producto, único.",
                           max_largo=191),
            CampoRequerido(nombre="categoria", tipo="texto",
                           descripcion="Categoría, p. ej. Construcción, Plomería, Electricidad."),
            CampoRequerido(nombre="unidad", tipo="texto",
                           descripcion="Unidad de venta, p. ej. unidad, bolsa, metro, kg, litro, caja."),
            CampoRequerido(nombre="precio_venta", tipo="numero", descripcion="Precio de venta en Bs.",
                           minimo=0),
            CampoRequerido(nombre="costo", tipo="numero", descripcion="Costo unitario en Bs.", minimo=0),
        ]

    def validar_producto_nuevo(self, datos: ProductoNuevo) -> list[ErrorCampo]:
        errores = []
        productos = self._productos()
        if any(normalizar(p.nombre) == normalizar(datos.nombre) for p in productos):
            errores.append(ErrorCampo(campo="nombre", mensaje=f"ya existe un producto llamado '{datos.nombre}'"))
        if datos.codigo and any(p.codigo and normalizar(p.codigo) == normalizar(datos.codigo) for p in productos):
            errores.append(ErrorCampo(campo="codigo", mensaje=f"el código '{datos.codigo}' ya está en uso"))
        return errores

    # ---- escritura

    def crear_producto(self, datos: ProductoNuevo) -> Producto:
        with self._bloqueo():
            if errores := self.validar_producto_nuevo(datos):
                raise ErrorValidacion(invalidos=errores)
            df = self._leer("productos")
            producto = Producto(
                id=siguiente_id(df["id"], "P", 4),
                **datos.model_dump(exclude={"atributos", "almacen"}),
                almacen=datos.almacen or ALMACEN_POR_DEFECTO,
                stock_actual=0,
            )
            fila = {
                **{k: "" if v is None else str(v) for k, v in producto.model_dump(include=set(COLUMNAS["productos"])).items()},
                "precio_venta": fmt_num(producto.precio_venta),
                "costo": fmt_num(producto.costo),
                "stock_actual": "0",
                "stock_minimo": fmt_num(producto.stock_minimo),
                "activo": "1",
            }
            self._escribir("productos", pd.concat([df, pd.DataFrame([fila])], ignore_index=True))
        log.info("Producto creado: %s %s", producto.id, producto.nombre)
        return producto

    def registrar_movimiento(self, datos: MovimientoInventario) -> MovimientoInventario:
        with self._bloqueo():
            productos = self._leer("productos").copy()
            filas = productos.index[productos["id"] == datos.producto_id]
            if len(filas) == 0:
                raise ProductoNoEncontrado(datos.producto_id)
            i = filas[0]
            stock = _num(productos.at[i, "stock_actual"], 0.0)
            saldo = round(stock + datos.delta, 4)
            if saldo < -_EPS:
                raise StockInsuficiente(productos.at[i, "nombre"], stock, datos.cantidad)

            movimientos = self._leer("movimientos")
            registrado = datos.model_copy(update={
                "id": siguiente_id(movimientos["id"], "M", 6),
                "saldo": saldo,
                "almacen": datos.almacen or _txt(productos.at[i, "almacen"]) or ALMACEN_POR_DEFECTO,
            })
            fila = {
                "id": registrado.id,
                "producto_id": registrado.producto_id,
                "tipo": registrado.tipo.value,
                "sentido": registrado.sentido.value,
                "cantidad": fmt_num(registrado.cantidad),
                "fecha": registrado.fecha.isoformat(),
                "motivo": registrado.motivo or "",
                "saldo": fmt_num(saldo),
                "almacen": registrado.almacen,
                "lote": registrado.lote or "",
                "fecha_vencimiento": registrado.fecha_vencimiento.isoformat() if registrado.fecha_vencimiento else "",
                "costo_unitario": fmt_num(registrado.costo_unitario),
                "documento": registrado.documento or "",
            }
            productos.at[i, "stock_actual"] = fmt_num(saldo)
            self._escribir_ambas(
                ("movimientos", pd.concat([movimientos, pd.DataFrame([fila])], ignore_index=True)),
                ("productos", productos),
            )
        log.info("Movimiento %s: %s %s%s", registrado.id, registrado.producto_id,
                 registrado.sentido.value, registrado.cantidad)
        return registrado

    def _escribir_ambas(self, primera: tuple[str, pd.DataFrame], segunda: tuple[str, pd.DataFrame]) -> None:
        """Escribe dos tablas; si la segunda falla, deja la primera como estaba."""
        anterior = self._leer(primera[0]).copy()
        self._escribir(*primera)
        try:
            self._escribir(*segunda)
        except Exception:
            self._escribir(primera[0], anterior)
            raise
