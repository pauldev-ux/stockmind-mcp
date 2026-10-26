"""Validación de datos de escritura antes de pedir confirmación.

Los datos llegan como dict desde la IA (o del futuro backend web) y pueden traer
cualquier cosa: textos con coma decimal, campos vacíos, campos que no existen.
Aquí se convierten al modelo común y, si algo falla, se lanza ErrorValidacion
con la lista en español de lo que falta y lo que es inválido, para que la IA
se lo pida al usuario.

Los campos desconocidos se rechazan en lugar de descartarse en silencio.
"""

import math
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from stockmind.domain.errores import CodigoAmbiguo, ErrorValidacion, ProductoNoEncontrado
from stockmind.domain.modelos import (
    CampoRequerido,
    ErrorCampo,
    MovimientoInventario,
    Producto,
    ProductoNuevo,
    TipoMovimiento,
)
from stockmind.ports.inventario import InventarioConnector

_EPS = 1e-9

CAMPOS_COMUNES_PRODUCTO: tuple[CampoRequerido, ...] = (
    CampoRequerido(nombre="nombre", tipo="texto", descripcion="Nombre del producto.", max_largo=191),
    CampoRequerido(nombre="codigo", tipo="texto", descripcion="Código o SKU.", obligatorio=False, max_largo=50),
    CampoRequerido(nombre="categoria", tipo="texto", descripcion="Categoría.", obligatorio=False),
    CampoRequerido(nombre="unidad", tipo="texto", descripcion="Unidad de medida.", obligatorio=False),
    CampoRequerido(nombre="precio_venta", tipo="numero", descripcion="Precio de venta.", obligatorio=False, minimo=0),
    CampoRequerido(nombre="costo", tipo="numero", descripcion="Costo unitario.", obligatorio=False, minimo=0),
    CampoRequerido(nombre="stock_minimo", tipo="numero", descripcion="Stock mínimo para alertas.",
                   obligatorio=False, minimo=0),
    CampoRequerido(nombre="proveedor", tipo="texto", descripcion="Proveedor habitual.", obligatorio=False),
    CampoRequerido(nombre="almacen", tipo="texto", descripcion="Almacén.", obligatorio=False),
)

_NO_PERMITIDOS_PRODUCTO = {
    "id": "lo asigna el sistema",
    "stock_actual": "crear un producto no crea stock; después registra una entrada con registrar_movimiento",
    "stock": "crear un producto no crea stock; después registra una entrada con registrar_movimiento",
}

CAMPOS_MOVIMIENTO: dict[str, CampoRequerido] = {
    c.nombre: c
    for c in (
        CampoRequerido(nombre="producto_id", tipo="texto", descripcion="Id o código del producto."),
        CampoRequerido(nombre="tipo", tipo="opcion", descripcion="Tipo de movimiento.",
                       opciones=tuple(t.value for t in TipoMovimiento)),
        CampoRequerido(nombre="cantidad", tipo="numero", descripcion="Cantidad, mayor que 0."),
        CampoRequerido(nombre="sentido", tipo="opcion", obligatorio=False, opciones=("+", "-"),
                       descripcion="En un ajuste: '+' suma stock, '-' resta stock."),
        CampoRequerido(nombre="motivo", tipo="texto", obligatorio=False, max_largo=500,
                       descripcion="Motivo (obligatorio en salidas y ajustes): merma, rotura, conteo físico…"),
        CampoRequerido(nombre="fecha", tipo="fecha", obligatorio=False,
                       descripcion="Fecha del movimiento (AAAA-MM-DD); por defecto hoy."),
        CampoRequerido(nombre="almacen", tipo="texto", obligatorio=False, descripcion="Almacén."),
        CampoRequerido(nombre="lote", tipo="texto", obligatorio=False, descripcion="Lote."),
        CampoRequerido(nombre="fecha_vencimiento", tipo="fecha", obligatorio=False,
                       descripcion="Vencimiento del lote."),
        CampoRequerido(nombre="costo_unitario", tipo="numero", obligatorio=False, minimo=0,
                       descripcion="Costo unitario de la entrada."),
        CampoRequerido(nombre="documento", tipo="texto", obligatorio=False,
                       descripcion="Documento de respaldo (factura, nota…)."),
    )
}

_ALIAS_MOVIMIENTO = {"producto": "producto_id", "codigo": "producto_id", "id_producto": "producto_id"}
_ALIAS_SENTIDO = {"positivo": "+", "negativo": "-", "suma": "+", "resta": "-", "mas": "+", "menos": "-"}


# ---------------------------------------------------------------- utilidades

def _clave(nombre: Any) -> str:
    return str(nombre).strip().lower().replace(" ", "_")


def _vacio(valor: Any) -> bool:
    return valor is None or (isinstance(valor, str) and not valor.strip())


def _a_numero(valor: Any) -> float:
    if isinstance(valor, bool):
        raise ValueError
    if isinstance(valor, (int, float)):
        numero = float(valor)
    else:
        numero = float(str(valor).strip().replace(" ", "").replace(",", "."))
    if not math.isfinite(numero):
        raise ValueError
    return numero


def _a_fecha(valor: Any) -> date:
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = str(valor).strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            pass
    raise ValueError


def convertir(campo: CampoRequerido, valor: Any) -> Any:
    """Convierte un valor según el tipo del campo. Lanza ValueError con el mensaje en español."""
    match campo.tipo:
        case "texto":
            if isinstance(valor, (dict, list)):
                raise ValueError("debe ser un texto")
            texto = str(valor).strip()
            if campo.max_largo and len(texto) > campo.max_largo:
                raise ValueError(f"no puede tener más de {campo.max_largo} caracteres")
            return texto
        case "numero" | "entero":
            try:
                numero = _a_numero(valor)
            except (ValueError, TypeError):
                raise ValueError(f"debe ser un número (recibido: {valor!r})") from None
            if campo.tipo == "entero" and not numero.is_integer():
                raise ValueError("debe ser un número entero")
            if campo.minimo is not None and numero < campo.minimo:
                raise ValueError(f"debe ser mayor o igual a {campo.minimo:g}")
            return int(numero) if campo.tipo == "entero" else numero
        case "fecha":
            try:
                return _a_fecha(valor)
            except (ValueError, TypeError):
                raise ValueError(f"debe ser una fecha AAAA-MM-DD o DD/MM/AAAA (recibido: {valor!r})") from None
        case "opcion":
            texto = str(valor).strip()
            for opcion in campo.opciones or ():
                if texto.casefold() == opcion.casefold():
                    return opcion
            raise ValueError(f"debe ser uno de: {', '.join(campo.opciones or ())} (recibido: {valor!r})")
    raise ValueError(f"tipo de campo desconocido: {campo.tipo}")


def _convertir_todo(
    datos: dict[str, Any], esquema: dict[str, CampoRequerido], no_permitidos: dict[str, str],
    alias: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[ErrorCampo]]:
    valores: dict[str, Any] = {}
    invalidos: list[ErrorCampo] = []
    for nombre_original, valor in datos.items():
        nombre = _clave(nombre_original)
        nombre = (alias or {}).get(nombre, nombre)
        if nombre in no_permitidos:
            invalidos.append(ErrorCampo(campo=nombre, mensaje=f"no se puede indicar: {no_permitidos[nombre]}"))
        elif nombre not in esquema:
            invalidos.append(ErrorCampo(
                campo=str(nombre_original),
                mensaje=f"campo no reconocido; los campos válidos son: {', '.join(esquema)}",
            ))
        elif not _vacio(valor):
            try:
                valores[nombre] = convertir(esquema[nombre], valor)
            except ValueError as e:
                invalidos.append(ErrorCampo(campo=nombre, mensaje=str(e)))
    return valores, invalidos


def _faltantes(esquema: dict[str, CampoRequerido], valores: dict[str, Any],
               invalidos: list[ErrorCampo]) -> list[CampoRequerido]:
    con_error = {e.campo for e in invalidos}
    return [c for c in esquema.values() if c.obligatorio and c.nombre not in valores and c.nombre not in con_error]


# ----------------------------------------------------------------- producto

def esquema_producto(conector: InventarioConnector) -> dict[str, CampoRequerido]:
    """Campos comunes más los del conector; los del conector mandan (obligatoriedad, opciones…)."""
    esquema = {c.nombre: c for c in CAMPOS_COMUNES_PRODUCTO}
    for campo in conector.campos_obligatorios_producto():
        esquema[campo.nombre] = campo
    return esquema


def validar_producto(datos: dict[str, Any], conector: InventarioConnector) -> tuple[ProductoNuevo, list[str]]:
    """Devuelve el producto listo para crear y una lista de advertencias (no bloquean)."""
    if not isinstance(datos, dict):
        raise ErrorValidacion(invalidos=[ErrorCampo(campo="datos", mensaje="debe ser un objeto con los campos")])
    esquema = esquema_producto(conector)
    valores, invalidos = _convertir_todo(datos, esquema, _NO_PERMITIDOS_PRODUCTO)
    faltantes = _faltantes(esquema, valores, invalidos)
    if faltantes or invalidos:
        raise ErrorValidacion(faltantes, invalidos)

    comunes = set(ProductoNuevo.model_fields) - {"atributos"}
    nuevo = ProductoNuevo(
        **{k: v for k, v in valores.items() if k in comunes},
        atributos={k: (v.isoformat() if isinstance(v, date) else v) for k, v in valores.items() if k not in comunes},
    )
    if errores := conector.validar_producto_nuevo(nuevo):
        raise ErrorValidacion(invalidos=errores)

    advertencias = []
    if nuevo.precio_venta is not None and nuevo.costo is not None and nuevo.precio_venta < nuevo.costo:
        advertencias.append(f"El precio de venta ({nuevo.precio_venta:g}) es menor que el costo ({nuevo.costo:g}).")
    return nuevo, advertencias


# --------------------------------------------------------------- movimiento

def validar_movimiento(
    datos: dict[str, Any], conector: InventarioConnector, hoy: date,
    comprometido: Callable[[Producto, str | None], float] | None = None,
) -> tuple[MovimientoInventario, Producto, list[str]]:
    """Devuelve el movimiento listo para registrar, el producto y las advertencias.

    Rechaza movimientos que dejarían el stock negativo. `comprometido(producto,
    almacén)` da el stock ya reservado por salidas pendientes en la bandeja.
    """
    movimiento, producto = construir_movimiento(datos, conector, hoy)
    advertencias = revisar_stock(movimiento, producto, movimiento.almacen, comprometido)
    return movimiento, producto, advertencias


def construir_movimiento(
    datos: dict[str, Any], conector: InventarioConnector, hoy: date
) -> tuple[MovimientoInventario, Producto]:
    """Primera parte: campos, tipos, fechas y producto. Todavía no mira el stock."""
    if not isinstance(datos, dict):
        raise ErrorValidacion(invalidos=[ErrorCampo(campo="datos", mensaje="debe ser un objeto con los campos")])
    datos = {k: (_ALIAS_SENTIDO.get(_clave(v), v) if _clave(k) == "sentido" and isinstance(v, str) else v)
             for k, v in datos.items()}
    valores, invalidos = _convertir_todo(datos, CAMPOS_MOVIMIENTO, {"saldo": "lo calcula el sistema",
                                                                    "id": "lo asigna el sistema"},
                                         _ALIAS_MOVIMIENTO)
    faltantes = _faltantes(CAMPOS_MOVIMIENTO, valores, invalidos)

    tipo = TipoMovimiento(valores["tipo"]) if "tipo" in valores else None
    if tipo == TipoMovimiento.AJUSTE and "sentido" not in valores:
        faltantes.append(CAMPOS_MOVIMIENTO["sentido"])
    if tipo in (TipoMovimiento.SALIDA, TipoMovimiento.AJUSTE) and "motivo" not in valores:
        faltantes.append(CAMPOS_MOVIMIENTO["motivo"])
    implicito = {TipoMovimiento.ENTRADA: "+", TipoMovimiento.SALIDA: "-"}
    if tipo in implicito and valores.get("sentido", implicito[tipo]) != implicito[tipo]:
        invalidos.append(ErrorCampo(campo="sentido", mensaje=f"una {tipo.value} siempre es '{implicito[tipo]}'; "
                                    "para corregir stock usa tipo 'ajuste'"))
    if "cantidad" in valores and valores["cantidad"] <= 0:
        invalidos.append(ErrorCampo(campo="cantidad", mensaje="debe ser mayor que 0"))
    if "fecha" in valores and valores["fecha"] > hoy:
        invalidos.append(ErrorCampo(campo="fecha", mensaje=f"no puede ser futura (hoy es {hoy.isoformat()})"))

    producto = None
    if "producto_id" in valores:
        try:
            producto = conector.obtener_producto(valores["producto_id"])
        except ProductoNoEncontrado as e:
            invalidos.append(ErrorCampo(campo="producto_id", mensaje=e.mensaje))
        except CodigoAmbiguo as e:
            opciones = ", ".join(f"{c['id']} ({c['nombre']})" for c in e.candidatos)
            invalidos.append(ErrorCampo(campo="producto_id", mensaje=f"{e.mensaje} Opciones: {opciones}"))
        else:
            if not producto.activo:
                invalidos.append(ErrorCampo(campo="producto_id", mensaje=f"el producto '{producto.nombre}' está inactivo"))

    if faltantes or invalidos:
        raise ErrorValidacion(faltantes, invalidos)

    movimiento = MovimientoInventario(
        **{k: v for k, v in valores.items() if k not in ("producto_id", "fecha")},
        producto_id=producto.id,
        fecha=valores.get("fecha", hoy),
    )
    return movimiento, producto


def revisar_stock(
    movimiento: MovimientoInventario, producto: Producto, almacen: str | None,
    comprometido: Callable[[Producto, str | None], float] | None = None,
) -> list[str]:
    """Segunda parte: no permite stock negativo (descontando lo comprometido) y avisa si queda
    bajo el mínimo. `almacen` puede ser el que eligió el ERP cuando el usuario no indicó uno."""
    reservado = comprometido(producto, almacen) if comprometido and movimiento.delta < 0 else 0.0
    disponible = producto.stock_en(almacen) - reservado
    resultante = disponible + movimiento.delta
    if resultante < -_EPS:
        unidad = f" {producto.unidad}" if producto.unidad else ""
        detalle = (f" ({reservado:g} ya comprometidas en operaciones pendientes de la bandeja)"
                   if reservado > _EPS else "")
        raise ErrorValidacion(invalidos=[ErrorCampo(
            campo="cantidad",
            mensaje=f"stock insuficiente de '{producto.nombre}': hay {disponible:g}{unidad} disponibles"
                    f"{detalle} y se piden {movimiento.cantidad:g}. No se permite stock negativo",
        )])

    advertencias = []
    if movimiento.delta < 0 and resultante < producto.stock_minimo:
        advertencias.append(
            f"El stock quedará en {resultante:g}, por debajo del mínimo ({producto.stock_minimo:g})."
        )
    return advertencias
