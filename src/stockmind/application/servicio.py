"""Casos de uso de StockMind. Los usan el servidor MCP y, más adelante, el backend web.

Lecturas: pasan directo al conector, con límites razonables para no saturar a la IA.
Escrituras: validar → vista previa + token → confirmar (revalida) → según el modo del conector:
  DIRECTA  → se escribe en el ERP;
  BANDEJA  → queda PENDIENTE en la bandeja de salida para registrarla luego en el ERP;
  NINGUNA  → se rechaza antes de validar.
Todo cambio queda en la auditoría.
"""

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from stockmind.application.auditoria import Auditoria
from stockmind.application.bandeja import BandejaStore, IntegracionGenerica, normalizar
from stockmind.application.confirmaciones import GestorConfirmaciones, OperacionPendiente, ahora_utc, hash_token
from stockmind.application.exportar_reportes import exportar_reporte
from stockmind.application.reportes import ServicioReportes
from stockmind.application.validacion import construir_movimiento, esquema_producto, revisar_stock, validar_producto
from stockmind.domain.bandeja import EstadoBandeja, OperacionBandeja, parsear_referencia, referencia
from stockmind.domain.reportes import Reporte
from stockmind.domain.errores import (
    BandejaNoDisponible,
    EscrituraNoHabilitada,
    ErrorValidacion,
    OperacionNoEncontrada,
    StockMindError,
    TokenInvalido,
    TransicionInvalida,
)
from stockmind.domain.modelos import (
    CampoRequerido,
    ErrorCampo,
    ModoEscritura,
    MovimientoInventario,
    Producto,
    ProductoNuevo,
    ResumenVentasProducto,
)
from stockmind.ports.bandeja import IntegracionBandeja
from stockmind.ports.inventario import InventarioConnector

OP_CREAR_PRODUCTO = "crear_producto"
OP_REGISTRAR_MOVIMIENTO = "registrar_movimiento"
TIPOS_BANDEJA = {"productos": OP_CREAR_PRODUCTO, "movimientos": OP_REGISTRAR_MOVIMIENTO}

_INSTRUCCION_CONFIRMAR = (
    "Muestra esta vista previa al usuario y pídele una confirmación explícita. "
    "Solo si acepta, llama a confirmar_operacion con este token; si no, llama a cancelar_operacion. "
    "El token vence en {minutos} minutos y sirve una sola vez."
)
_INSTRUCCION_BANDEJA = (
    " IMPORTANTE: {erp} no admite escritura directa. Al confirmar, la operación NO se registrará en {erp}: "
    "quedará PENDIENTE en la bandeja de salida y habrá que registrarla en {erp} siguiendo la guía. "
    "Díselo al usuario antes de que confirme."
)


def _limitar(valor: int, minimo: int, maximo: int) -> int:
    return max(minimo, min(int(valor), maximo))


def _local(momento: datetime | None) -> str | None:
    return momento.astimezone().isoformat(timespec="seconds") if momento else None


class InventarioService:
    def __init__(
        self,
        conector: InventarioConnector,
        confirmaciones: GestorConfirmaciones,
        auditoria: Auditoria,
        *,
        bandeja: BandejaStore | None = None,
        exports_dir: Path | None = None,
        moneda_base: str = "BOB",
        origen: str = "mcp",
        hoy: Callable[[], date] = date.today,
        reloj: Callable[[], datetime] = ahora_utc,
    ):
        self.conector = conector
        self.confirmaciones = confirmaciones
        self.auditoria = auditoria
        self.bandeja = bandeja
        self.exports_dir = Path(exports_dir) if exports_dir else Path("exports")
        self.origen = origen
        self.hoy = hoy
        self.reloj = reloj
        self.reportes = ServicioReportes(conector, hoy=hoy, reloj=reloj, moneda_base=moneda_base)
        self.integracion: IntegracionBandeja = (
            conector if isinstance(conector, IntegracionBandeja) else IntegracionGenerica(conector.nombre_visible)
        )

    @property
    def modo(self) -> ModoEscritura:
        return self.conector.modo_escritura

    @property
    def _en_bandeja(self) -> bool:
        return self.modo == ModoEscritura.BANDEJA

    # ------------------------------------------------------------- lectura

    def buscar_productos(self, texto: str = "", limite: int = 20) -> list[Producto]:
        return self.conector.buscar_productos(texto or "", _limitar(limite, 1, 100))

    def obtener_producto(self, id_o_codigo: str) -> Producto:
        return self.conector.obtener_producto(id_o_codigo)

    def listar_bajo_stock(self) -> list[Producto]:
        return self.conector.listar_bajo_stock()

    def listar_movimientos(
        self, producto: str, desde: date | None = None, hasta: date | None = None, limite: int = 100
    ) -> tuple[Producto, list[MovimientoInventario], int]:
        """Devuelve el producto, los últimos `limite` movimientos del rango y el total del rango."""
        _validar_rango(desde, hasta)
        p = self.conector.obtener_producto(producto)
        movimientos = self.conector.listar_movimientos(p.id, desde, hasta)
        return p, movimientos[-_limitar(limite, 1, 1000):], len(movimientos)

    def ventas_por_producto(
        self, desde: date | None = None, hasta: date | None = None, limite: int = 50, almacen: str | None = None
    ) -> list[ResumenVentasProducto]:
        _validar_rango(desde, hasta)
        return self.conector.ventas_por_producto(desde, hasta, almacen)[: _limitar(limite, 1, 1000)]

    def exportar_reporte(self, reporte: Reporte) -> Path:
        """Guarda el reporte en .xlsx dentro de exports/ y devuelve la ruta."""
        return exportar_reporte(reporte, self.exports_dir, self.reloj())

    def campos_producto(self) -> dict[str, Any]:
        esquema = esquema_producto(self.conector)
        campos = [c.model_dump(exclude_none=True) for c in esquema.values()]
        return {
            "conector": self.conector.nombre,
            "modo_escritura": self.modo.value,
            "obligatorios": [c for c in campos if c["obligatorio"]],
            "opcionales": [c for c in campos if not c["obligatorio"]],
        }

    # ---------------------------------------------------------- validación

    def _exigir_escritura(self) -> None:
        if self.modo == ModoEscritura.NINGUNA:
            raise EscrituraNoHabilitada(self.conector.nombre_visible)
        if self._en_bandeja and self.bandeja is None:
            raise BandejaNoDisponible("El conector trabaja con bandeja de salida, pero la bandeja no está configurada.")

    def _validar_producto(self, datos: dict[str, Any]) -> tuple[ProductoNuevo, list[str]]:
        nuevo, advertencias = validar_producto(datos, self.conector)
        if self._en_bandeja:
            for op in self.bandeja.listar(EstadoBandeja.PENDIENTE, OP_CREAR_PRODUCTO):
                if normalizar(str(op.datos.get("nombre", ""))) == normalizar(nuevo.nombre):
                    raise ErrorValidacion(invalidos=[ErrorCampo(
                        campo="nombre",
                        mensaje=f"ya hay un producto con ese nombre pendiente de registrar ({op.referencia}); "
                                "regístralo o descártalo primero",
                    )])
        return nuevo, advertencias + self.conector.advertencias_producto_nuevo(nuevo)

    def _pendientes_de(self, producto_id: str) -> list[OperacionBandeja]:
        if not (self._en_bandeja and self.bandeja):
            return []
        return [op for op in self.bandeja.listar(EstadoBandeja.PENDIENTE, OP_REGISTRAR_MOVIMIENTO)
                if str(op.datos.get("producto_id")) == producto_id]

    def _comprometido(self, producto: Producto, almacen: str | None) -> float:
        """Salidas pendientes del producto (y del almacén, si se indica) que el ERP aún no descontó."""
        total = 0.0
        for op in self._pendientes_de(producto.id):
            if op.datos.get("sentido") != "-":
                continue
            almacen_op = op.detalle_erp.get("almacen") or op.datos.get("almacen")
            if almacen and almacen_op and normalizar(str(almacen_op)) != normalizar(almacen):
                continue
            total += float(op.datos["cantidad"])
        return total

    def _validar_movimiento(
        self, datos: dict[str, Any]
    ) -> tuple[MovimientoInventario, Producto, list[str], dict[str, Any], float]:
        movimiento, producto = construir_movimiento(datos, self.conector, self.hoy())
        # Las reglas del ERP van antes del stock: un almacén inexistente debe decir eso, no "hay 0".
        revision = self.conector.validar_movimiento_nuevo(movimiento, producto, self._pendientes_de(producto.id))
        if revision.faltantes or revision.errores:
            raise ErrorValidacion(revision.faltantes, revision.errores)
        almacen = movimiento.almacen or revision.detalle.get("almacen")
        comprometido = self._comprometido if self._en_bandeja else None
        advertencias = revisar_stock(movimiento, producto, almacen, comprometido)
        reservado = self._comprometido(producto, almacen) if self._en_bandeja and movimiento.delta < 0 else 0.0
        return movimiento, producto, advertencias + revision.advertencias, revision.detalle, reservado

    @staticmethod
    def _vista_movimiento(movimiento: MovimientoInventario, producto: Producto, detalle: dict[str, Any],
                          reservado: float) -> dict[str, Any]:
        disponible = producto.stock_en(movimiento.almacen or detalle.get("almacen"))
        vista = {
            "producto": {"id": producto.id, "codigo": producto.codigo, "nombre": producto.nombre,
                         "unidad": producto.unidad},
            **movimiento.model_dump(mode="json", exclude_none=True, exclude={"producto_id", "atributos"}),
            "stock_actual": disponible,
            "stock_resultante": round(disponible - reservado + movimiento.delta, 4),
        }
        if reservado:
            vista["stock_comprometido_en_bandeja"] = reservado
        if detalle:
            vista["detalle_erp"] = detalle
        return vista

    # ---------------------------------------------------------- escritura

    def preparar_crear_producto(self, datos: dict[str, Any]) -> dict[str, Any]:
        # Antes de validar: no tiene sentido pedirle datos al usuario ni emitir
        # un token si el conector no puede guardar.
        self._exigir_escritura()
        nuevo, advertencias = self._validar_producto(datos)
        return self._emitir(OP_CREAR_PRODUCTO, nuevo.a_datos_planos(), nuevo.a_datos_planos(), advertencias)

    def preparar_movimiento(self, datos: dict[str, Any]) -> dict[str, Any]:
        self._exigir_escritura()
        movimiento, producto, advertencias, detalle, reservado = self._validar_movimiento(datos)
        vista = self._vista_movimiento(movimiento, producto, detalle, reservado)
        datos_op = movimiento.model_dump(mode="json", exclude_none=True, exclude={"atributos"})
        return self._emitir(OP_REGISTRAR_MOVIMIENTO, datos_op, vista, advertencias)

    def _emitir(self, tipo: str, datos_op: dict[str, Any], vista: dict[str, Any],
                advertencias: list[str]) -> dict[str, Any]:
        token, operacion = self.confirmaciones.emitir(tipo, datos_op)
        self._auditar("preparada", operacion, datos=datos_op)
        minutos = int(self.confirmaciones.ttl.total_seconds() // 60)
        instruccion = _INSTRUCCION_CONFIRMAR.format(minutos=minutos)
        if self._en_bandeja:
            instruccion += _INSTRUCCION_BANDEJA.format(erp=self.conector.nombre_visible)
        return {
            "ok": True,
            "requiere_confirmacion": True,
            "operacion": tipo,
            "modo_escritura": self.modo.value,
            "token": token,
            "vence": operacion.vence.astimezone().isoformat(timespec="seconds"),
            "vista_previa": vista,
            "advertencias": advertencias,
            "instruccion": instruccion,
        }

    def confirmar(self, token: str) -> dict[str, Any]:
        try:
            operacion = self.confirmaciones.consumir(token)
        except TokenInvalido as e:
            self._auditar_rechazo_token(token, e)
            raise

        try:
            if self._en_bandeja:
                respuesta = self._encolar(operacion)
            else:
                respuesta = {"ok": True, "operacion": operacion.tipo, "resultado": self._ejecutar(operacion),
                             "mensaje": "Operación guardada correctamente."}
        except Exception as e:
            mensaje = e.mensaje if isinstance(e, StockMindError) else f"{type(e).__name__}: {e}"
            self._auditar("rechazada", operacion, datos=operacion.datos, error=mensaje)
            raise

        if self._en_bandeja:
            self._auditar("encolada", operacion, datos=operacion.datos, referencia=respuesta["referencia"],
                          resultado={"estado": EstadoBandeja.PENDIENTE.value})
        else:
            self._auditar("confirmada", operacion, datos=operacion.datos, resultado=respuesta["resultado"])
        return respuesta

    def _ejecutar(self, operacion: OperacionPendiente) -> dict[str, Any]:
        # Se vuelve a validar: entre la vista previa y la confirmación otro
        # proceso pudo cambiar el stock o crear un producto con el mismo nombre.
        self._exigir_escritura()
        if operacion.tipo == OP_CREAR_PRODUCTO:
            nuevo, _ = self._validar_producto(operacion.datos)
            return self.conector.crear_producto(nuevo).model_dump(mode="json")
        if operacion.tipo == OP_REGISTRAR_MOVIMIENTO:
            movimiento, *_ = self._validar_movimiento(operacion.datos)
            return self.conector.registrar_movimiento(movimiento).model_dump(mode="json")
        raise StockMindError(f"Operación desconocida: {operacion.tipo}")

    def _encolar(self, operacion: OperacionPendiente) -> dict[str, Any]:
        """Modo bandeja: revalida y deja la operación PENDIENTE en vez de escribir en el ERP."""
        self._exigir_escritura()
        if operacion.tipo == OP_CREAR_PRODUCTO:
            nuevo, advertencias = self._validar_producto(operacion.datos)
            vista, detalle = nuevo.a_datos_planos(), {}
        elif operacion.tipo == OP_REGISTRAR_MOVIMIENTO:
            movimiento, producto, advertencias, detalle, reservado = self._validar_movimiento(operacion.datos)
            vista = self._vista_movimiento(movimiento, producto, detalle, reservado)
        else:
            raise StockMindError(f"Operación desconocida: {operacion.tipo}")

        ahora = self.reloj()
        guardada = self.bandeja.agregar(OperacionBandeja(
            id=0, tipo=operacion.tipo, conector=self.conector.nombre, origen=self.origen, datos=operacion.datos,
            vista_previa=vista, detalle_erp=detalle, creada_en=ahora, actualizada_en=ahora,
        ))
        guia = self.integracion.guia(guardada)
        self.bandeja.actualizar_guia(guardada.id, guia)
        erp = self.conector.nombre_visible
        return {
            "ok": True,
            "operacion": operacion.tipo,
            "estado": "pendiente_en_erp",
            "referencia": guardada.referencia,
            "vista_previa": vista,
            "advertencias": advertencias,
            "guia": guia,
            "mensaje": (
                f"La operación NO se registró todavía en {erp}: quedó PENDIENTE en la bandeja de salida con la "
                f"referencia {guardada.referencia}. Para completarla, regístrala en {erp} siguiendo la guía "
                "(puedes exportar las pendientes con exportar_pendientes) y después ejecuta verificar_pendientes "
                "o marcar_registrada."
            ),
        }

    def cancelar(self, token: str) -> dict[str, Any]:
        operacion = self.confirmaciones.cancelar(token)
        self._auditar("cancelada", operacion, datos=operacion.datos)
        return {"ok": True, "operacion": operacion.tipo, "mensaje": "Operación cancelada; no se guardó nada."}

    # --------------------------------------------------- bandeja de salida

    def _exigir_bandeja(self) -> None:
        if not self._en_bandeja:
            como = ("escribe directamente al confirmar" if self.modo == ModoEscritura.DIRECTA
                    else "no acepta escrituras")
            raise BandejaNoDisponible(
                f"El conector activo ({self.conector.nombre_visible}) {como}; no usa la bandeja de salida.")
        if self.bandeja is None:
            raise BandejaNoDisponible("La bandeja de salida no está configurada.")

    def _tipo_bandeja(self, tipo: str | None) -> str | None:
        if tipo in (None, "", "todos"):
            return None
        if tipo not in TIPOS_BANDEJA:
            raise ErrorValidacion(invalidos=[ErrorCampo(
                campo="tipo", mensaje="debe ser uno de: productos, movimientos, todos")])
        return TIPOS_BANDEJA[tipo]

    def _resumen(self, op: OperacionBandeja) -> dict[str, Any]:
        if op.tipo == OP_CREAR_PRODUCTO:
            descripcion = f"Crear producto «{op.datos.get('nombre')}»"
        else:
            producto = op.vista_previa.get("producto", {})
            almacen = op.detalle_erp.get("almacen") or op.datos.get("almacen")
            descripcion = (f"{op.datos.get('tipo', 'movimiento').capitalize()} de {op.datos.get('cantidad')} "
                           f"({op.datos.get('sentido')}) de «{producto.get('nombre', op.datos.get('producto_id'))}»"
                           + (f" en {almacen}" if almacen else ""))
        resumen = {
            "referencia": op.referencia, "tipo": op.tipo, "descripcion": descripcion, "estado": op.estado.value,
            "conector": op.conector, "creada_en": _local(op.creada_en), "actualizada_en": _local(op.actualizada_en),
            "observacion": op.observacion, "registrada_por": op.registrada_por, "id_externo": op.id_externo,
            "exportada_en": _local(op.exportada_en), "archivo": op.archivo,
        }
        if op.estado == EstadoBandeja.PENDIENTE:
            resumen["guia"] = op.guia
        return {k: v for k, v in resumen.items() if v is not None}

    def listar_pendientes(self, estado: str = "pendiente", tipo: str | None = None) -> dict[str, Any]:
        if self.bandeja is None:
            raise BandejaNoDisponible("La bandeja de salida no está configurada.")
        if estado in ("todas", "todos", ""):
            filtro = None
        else:
            try:
                filtro = EstadoBandeja(estado)
            except ValueError:
                raise ErrorValidacion(invalidos=[ErrorCampo(
                    campo="estado", mensaje="debe ser uno de: pendiente, registrada, descartada, todas")]) from None
        operaciones = self.bandeja.listar(filtro, self._tipo_bandeja(tipo))
        return {"ok": True, "total": len(operaciones), "operaciones": [self._resumen(op) for op in operaciones]}

    def _pendientes_del_conector(self, tipo_op: str | None = None) -> list[OperacionBandeja]:
        return [op for op in self.bandeja.listar(EstadoBandeja.PENDIENTE, tipo_op) if op.conector == self.conector.nombre]

    def exportar_pendientes(self, tipo: str = "todos") -> dict[str, Any]:
        self._exigir_bandeja()
        tipo_op = self._tipo_bandeja(tipo)
        ahora = self.reloj()
        archivos: list[str] = []
        exportadas: list[str] = []
        for op_tipo in ([tipo_op] if tipo_op else list(TIPOS_BANDEJA.values())):
            operaciones = self._pendientes_del_conector(op_tipo)
            if not operaciones:
                continue
            rutas = [str(r) for r in self.integracion.exportar(operaciones, op_tipo, self.exports_dir, ahora)]
            self.bandeja.marcar_exportadas([op.id for op in operaciones], ahora, rutas[0])
            for op in operaciones:
                self._auditar_bandeja("exportada", op, datos={"archivos": rutas})
            archivos.extend(rutas)
            exportadas.extend(op.referencia for op in operaciones)
        if not exportadas:
            return {"ok": True, "archivos": [], "operaciones": [], "mensaje": "No hay operaciones pendientes para exportar."}
        erp = self.conector.nombre_visible
        return {
            "ok": True, "archivos": archivos, "operaciones": exportadas,
            "mensaje": (f"Se exportaron {len(exportadas)} operaciones. Siguen PENDIENTES hasta que las registres "
                        f"en {erp}; después ejecuta verificar_pendientes o marcar_registrada."),
        }

    def verificar_pendientes(self) -> dict[str, Any]:
        """Concilia con el ERP (solo lectura): las coincidencias exactas se marcan como registradas;
        las posibles solo se informan para que el usuario decida."""
        self._exigir_bandeja()
        registradas, posibles, siguen = [], [], []
        for op in self._pendientes_del_conector():
            coincidencias = self.integracion.verificar(op)
            exacta = next((c for c in coincidencias if c.exacta), None)
            if exacta:
                hecha = self._transicionar(op.id, EstadoBandeja.REGISTRADA, exacta.descripcion,
                                           registrada_por="conciliacion", id_externo=exacta.id_externo)
                registradas.append({"referencia": hecha.referencia, "descripcion": exacta.descripcion,
                                    "id_externo": exacta.id_externo})
                continue
            siguen.append(op.referencia)
            if coincidencias:
                posibles.append({"referencia": op.referencia, "coincidencias": [
                    c.model_dump(exclude={"exacta"}, exclude_none=True) for c in coincidencias]})
        mensaje = f"{len(registradas)} registradas por conciliación; {len(siguen)} siguen pendientes."
        if posibles:
            mensaje += (" Hay posibles coincidencias que NO se marcaron solas: revísalas y usa marcar_registrada "
                        "si corresponden.")
        return {"ok": True, "registradas": registradas, "posibles": posibles, "siguen_pendientes": siguen,
                "mensaje": mensaje}

    def marcar_registrada(self, id_operacion: str | int, observacion: str | None = None) -> dict[str, Any]:
        self._exigir_bandeja_existente()
        op = self._transicionar(_id(id_operacion), EstadoBandeja.REGISTRADA,
                                (observacion or "").strip() or None, registrada_por="manual")
        return {"ok": True, "operacion": self._resumen(op),
                "mensaje": f"{op.referencia} quedó marcada como registrada en {self.conector.nombre_visible}."}

    def descartar_pendiente(self, id_operacion: str | int, motivo: str | None = None) -> dict[str, Any]:
        self._exigir_bandeja_existente()
        if not motivo or not str(motivo).strip():
            raise ErrorValidacion(faltantes=[CampoRequerido(
                nombre="motivo", tipo="texto", descripcion="Por qué se descarta la operación.")])
        op = self._transicionar(_id(id_operacion), EstadoBandeja.DESCARTADA, str(motivo).strip())
        return {"ok": True, "operacion": self._resumen(op),
                "mensaje": f"{op.referencia} quedó descartada; no se registrará en {self.conector.nombre_visible}."}

    def _exigir_bandeja_existente(self) -> None:
        if self.bandeja is None:
            raise BandejaNoDisponible("La bandeja de salida no está configurada.")

    def _transicionar(self, id_operacion: int, estado: EstadoBandeja, observacion: str | None,
                      registrada_por: str | None = None, id_externo: str | None = None) -> OperacionBandeja:
        actual = self.bandeja.obtener(id_operacion)
        if actual is None:
            raise OperacionNoEncontrada(referencia(id_operacion))
        hecha = self.bandeja.transicionar(id_operacion, estado, self.reloj(), observacion, registrada_por, id_externo)
        if hecha is None:
            actual = self.bandeja.obtener(id_operacion)
            raise TransicionInvalida(
                f"{actual.referencia} ya está {actual.estado.value} desde {_local(actual.actualizada_en)}; "
                "solo una operación pendiente puede cambiar de estado.")
        self._auditar_bandeja(estado.value, hecha, datos={
            "de": EstadoBandeja.PENDIENTE.value, "a": estado.value, "observacion": observacion,
            "registrada_por": registrada_por, "id_externo": id_externo})
        return hecha

    # ---------------------------------------------------------- auditoría

    def _auditar(self, evento: str, operacion: OperacionPendiente, **extra: Any) -> None:
        self.auditoria.registrar(evento, operacion.tipo, conector=self.conector.nombre, origen=self.origen,
                                 token_hash=operacion.token_hash, **extra)

    def _auditar_bandeja(self, evento: str, operacion: OperacionBandeja, datos: dict[str, Any]) -> None:
        self.auditoria.registrar(evento, operacion.tipo, conector=operacion.conector, origen=self.origen,
                                 referencia=operacion.referencia,
                                 datos={k: v for k, v in datos.items() if v is not None})

    def _auditar_rechazo_token(self, token: Any, error: TokenInvalido) -> None:
        token_hash = hash_token(token) if isinstance(token, str) and token.strip() else None
        self.auditoria.registrar("rechazada", "confirmar_operacion", conector=self.conector.nombre,
                                 origen=self.origen, token_hash=token_hash, error=error.mensaje)


def _id(id_operacion: str | int) -> int:
    try:
        return parsear_referencia(id_operacion)
    except ValueError as e:
        raise ErrorValidacion(invalidos=[ErrorCampo(campo="id", mensaje=str(e))]) from None


def _validar_rango(desde: date | None, hasta: date | None) -> None:
    if desde and hasta and desde > hasta:
        raise ErrorValidacion(invalidos=[ErrorCampo(campo="desde", mensaje="no puede ser posterior a 'hasta'")])
