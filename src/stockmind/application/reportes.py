"""Reportes de inventario, calculados solo con métodos del puerto (sirven con cualquier conector).

Reglas comunes:
- Período: desde/hasta o "últimos N días" (que terminan hoy), nunca los dos a la vez.
- Filtros: categoría (sobre Producto.categoria) y almacén (stock por almacén y
  ventas/movimientos filtrados por el conector).
- Los montos de ventas van en la moneda base que informa el conector; costo y
  precio, en la moneda de cada producto. Los totales nunca mezclan monedas.
- Sin datos suficientes, el valor queda en None, `sin_datos` en True y un aviso
  lo explica. Nunca se inventan ceros.
"""

import math
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta

from stockmind.application.confirmaciones import ahora_utc
from stockmind.domain.errores import ErrorValidacion
from stockmind.domain.modelos import ErrorCampo, Producto, ResumenVentasProducto
from stockmind.domain.reportes import (
    FilaABC,
    FilaBajoStock,
    FilaRotacion,
    FilaSinMovimiento,
    FilaValorizacion,
    FilaVendido,
    Periodo,
    ProductoSinVentas,
    ReporteABC,
    ReporteBajoStock,
    ReporteMasVendidos,
    ReporteRotacion,
    ReporteSinMovimiento,
    ResumenClase,
    ResumenEjecutivo,
)
from stockmind.domain.texto import normalizar
from stockmind.ports.inventario import InventarioConnector

_EPS = 1e-9
SIN_CATEGORIA = "Sin categoría"


# ------------------------------------------------------------------ formatos

def numero(valor: float | None, decimales: int = 2) -> str:
    """12345.6 → '12.345,6' (formato boliviano)."""
    if valor is None:
        return "—"
    texto = f"{valor:,.{decimales}f}".rstrip("0").rstrip(".") if decimales else f"{valor:,.0f}"
    return texto.replace(",", "§").replace(".", ",").replace("§", ".")


def dinero(valor: float | None, moneda: str) -> str:
    return f"{moneda} {numero(valor)}" if valor is not None else "—"


def fecha(d: date | None) -> str:
    return d.strftime("%d/%m/%Y") if d else "—"


def _r(valor: float | None, decimales: int = 2) -> float | None:
    return None if valor is None else round(valor, decimales)


# ------------------------------------------------------------------ servicio

class ServicioReportes:
    def __init__(
        self,
        conector: InventarioConnector,
        *,
        hoy: Callable[[], date] = date.today,
        reloj: Callable[[], datetime] = ahora_utc,
        moneda_base: str = "BOB",
    ):
        self.conector = conector
        self.hoy = hoy
        self.reloj = reloj
        self.moneda_base = moneda_base

    # ---- utilidades

    def periodo(self, desde: date | None, hasta: date | None, ultimos_dias: int | None,
                por_defecto: int) -> Periodo:
        hoy = self.hoy()
        if ultimos_dias is not None and (desde or hasta):
            raise ErrorValidacion(invalidos=[ErrorCampo(
                campo="ultimos_dias", mensaje="usa desde/hasta o ultimos_dias, no los dos")])
        if ultimos_dias is not None:
            if int(ultimos_dias) < 1:
                raise ErrorValidacion(invalidos=[ErrorCampo(campo="ultimos_dias", mensaje="debe ser al menos 1")])
            hasta, desde = hoy, hoy - timedelta(days=int(ultimos_dias) - 1)
        else:
            hasta = hasta or hoy
            desde = desde or hasta - timedelta(days=por_defecto - 1)
        if hasta > hoy:
            raise ErrorValidacion(invalidos=[ErrorCampo(
                campo="hasta", mensaje=f"no puede ser futura (hoy es {fecha(hoy)})")])
        if desde > hasta:
            raise ErrorValidacion(invalidos=[ErrorCampo(campo="desde", mensaje="no puede ser posterior a 'hasta'")])
        return Periodo(desde=desde, hasta=hasta, dias=(hasta - desde).days + 1)

    def _ventana(self, dias: int, campo: str) -> Periodo:
        if int(dias) < 1:
            raise ErrorValidacion(invalidos=[ErrorCampo(campo=campo, mensaje="debe ser al menos 1")])
        hoy = self.hoy()
        return Periodo(desde=hoy - timedelta(days=int(dias) - 1), hasta=hoy, dias=int(dias))

    def _moneda(self, producto: Producto) -> str:
        return producto.moneda or self.moneda_base

    @staticmethod
    def _categoria(producto: Producto | None) -> str:
        return (producto.categoria if producto and producto.categoria else SIN_CATEGORIA)

    @staticmethod
    def _filtros(**filtros) -> dict:
        return {k: v for k, v in filtros.items() if v is not None}

    def _catalogo(self) -> dict[str, Producto]:
        return {p.id: p for p in self.conector.listar_productos()}

    def _en_categoria(self, producto: Producto | None, categoria: str | None) -> bool:
        return categoria is None or (producto is not None and normalizar(self._categoria(producto)) == normalizar(categoria))

    def _activos(self, catalogo: dict[str, Producto], categoria: str | None) -> list[Producto]:
        return [p for p in catalogo.values() if p.activo and self._en_categoria(p, categoria)]

    def _aviso_categoria(self, catalogo: dict[str, Producto], categoria: str | None) -> list[str]:
        if categoria is None or any(self._en_categoria(p, categoria) for p in catalogo.values()):
            return []
        disponibles = sorted({self._categoria(p) for p in catalogo.values()})
        return [f"No hay productos en la categoría «{categoria}». Categorías: {', '.join(disponibles)}."]

    def _ventas(self, periodo: Periodo, almacen: str | None) -> dict[str, ResumenVentasProducto]:
        return {v.producto_id: v for v in self.conector.ventas_por_producto(periodo.desde, periodo.hasta, almacen)
                if v.cantidad_total > _EPS or v.monto_total > _EPS}

    @staticmethod
    def _texto_periodo(periodo: Periodo) -> str:
        return f"del {fecha(periodo.desde)} al {fecha(periodo.hasta)} ({periodo.dias} días)"

    @staticmethod
    def _texto_filtros(categoria: str | None, almacen: str | None) -> str:
        partes = ([f"categoría «{categoria}»"] if categoria else []) + ([f"almacén «{almacen}»"] if almacen else [])
        return f" ({', '.join(partes)})" if partes else ""

    # ------------------------------------------------------- 1. más vendidos

    def mas_vendidos(self, desde: date | None = None, hasta: date | None = None, ultimos_dias: int | None = None,
                     top: int = 10, categoria: str | None = None, almacen: str | None = None) -> ReporteMasVendidos:
        periodo = self.periodo(desde, hasta, ultimos_dias, 30)
        top = max(1, min(int(top), 100))
        catalogo = self._catalogo()
        ventas = [v for v in self._ventas(periodo, almacen).values()
                  if self._en_categoria(catalogo.get(v.producto_id), categoria)]
        total_u = sum(v.cantidad_total for v in ventas)
        total_m = sum(v.monto_total for v in ventas)
        filas = [FilaVendido(
            producto_id=v.producto_id, codigo=v.codigo, nombre=v.nombre,
            categoria=self._categoria(catalogo.get(v.producto_id)),
            cantidad=_r(v.cantidad_total, 4), monto=_r(v.monto_total),
            participacion_cantidad=_r(v.cantidad_total / total_u * 100 if total_u else 0),
            participacion_monto=_r(v.monto_total / total_m * 100 if total_m else 0),
            numero_ventas=v.numero_ventas,
        ) for v in ventas]
        reporte = ReporteMasVendidos(
            tipo="mas_vendidos", titulo="Productos más vendidos", periodo=periodo,
            filtros=self._filtros(categoria=categoria, almacen=almacen, top=top), generado_en=self.reloj(),
            total_unidades=_r(total_u, 4), total_monto=_r(total_m), productos_con_ventas=len(filas),
            por_cantidad=sorted(filas, key=lambda f: (-f.cantidad, f.nombre))[:top],
            por_monto=sorted(filas, key=lambda f: (-f.monto, f.nombre))[:top],
            metodo=[
                f"Fuente: ventas por producto registradas {self._texto_periodo(periodo)}"
                f"{self._texto_filtros(categoria, almacen)}.",
                f"Se ordenan por unidades vendidas y, aparte, por monto vendido ({self.moneda_base}); top {top}.",
                f"Participación = valor del producto / total del período × 100 "
                f"(total: {numero(total_u)} unidades y {dinero(total_m, self.moneda_base)}).",
            ],
            avisos=self._aviso_categoria(catalogo, categoria),
        )
        if not filas:
            reporte.sin_datos = True
            reporte.avisos.append(f"No hay ventas registradas {self._texto_periodo(periodo)}"
                                  f"{self._texto_filtros(categoria, almacen)}: no hay ranking de más vendidos.")
        return reporte

    # ---------------------------------------------------------- 2. bajo stock

    def bajo_stock(self, dias_ventas: int = 30, dias_objetivo: int = 30, cobertura_menor_a: float | None = None,
                   categoria: str | None = None, almacen: str | None = None) -> ReporteBajoStock:
        ventana = self._ventana(dias_ventas, "dias_ventas")
        if int(dias_objetivo) < 1:
            raise ErrorValidacion(invalidos=[ErrorCampo(campo="dias_objetivo", mensaje="debe ser al menos 1")])
        catalogo = self._catalogo()
        ventas = self._ventas(ventana, almacen)
        hay_ventas = bool(ventas)
        filas = []
        for p in self._activos(catalogo, categoria):
            stock = p.stock_en(almacen)
            vendido = ventas[p.id].cantidad_total if p.id in ventas else 0.0
            venta_diaria = vendido / ventana.dias if hay_ventas else None
            cobertura = stock / venta_diaria if venta_diaria else None
            bajo_minimo = stock < p.stock_minimo - _EPS
            cobertura_baja = (cobertura_menor_a is not None and cobertura is not None
                              and cobertura < cobertura_menor_a)
            if not (bajo_minimo or cobertura_baja):
                continue
            objetivo = max(p.stock_minimo, (venta_diaria or 0.0) * int(dias_objetivo))
            filas.append(FilaBajoStock(
                producto_id=p.id, codigo=p.codigo, nombre=p.nombre, categoria=self._categoria(p), unidad=p.unidad,
                stock_actual=_r(stock, 4), stock_minimo=_r(p.stock_minimo, 4),
                vendido_periodo=_r(vendido, 4) if hay_ventas else None,
                venta_diaria=_r(venta_diaria, 4), cobertura_dias=_r(cobertura, 1),
                objetivo=_r(objetivo, 4), sugerido_reponer=float(max(0, math.ceil(objetivo - stock - _EPS))),
                motivo="bajo_minimo" if bajo_minimo else "cobertura_baja",
            ))
        filas.sort(key=lambda f: (f.cobertura_dias if f.cobertura_dias is not None else math.inf,
                                  f.stock_actual / f.stock_minimo if f.stock_minimo else math.inf))
        reporte = ReporteBajoStock(
            tipo="bajo_stock", titulo="Productos bajo stock", periodo=ventana, dias_ventas=ventana.dias,
            dias_objetivo=int(dias_objetivo), generado_en=self.reloj(), productos=filas,
            filtros=self._filtros(categoria=categoria, almacen=almacen, cobertura_menor_a=cobertura_menor_a),
            metodo=[
                "Bajo stock: stock actual < stock mínimo"
                + (f", o cobertura menor a {numero(cobertura_menor_a)} días" if cobertura_menor_a is not None else "")
                + (f"; stock del almacén «{almacen}»" if almacen else "") + ".",
                f"Venta diaria = unidades vendidas {self._texto_periodo(ventana)} / {ventana.dias}.",
                "Cobertura (días) = stock actual / venta diaria; sin ventas del producto queda indefinida.",
                f"Sugerido a reponer = máx(0, ⌈máx(stock mínimo, venta diaria × {dias_objetivo} días) − stock actual⌉). "
                "No incluye tiempo de entrega ni stock de seguridad.",
            ],
            avisos=self._aviso_categoria(catalogo, categoria),
        )
        if not hay_ventas:
            reporte.sin_datos = True
            reporte.avisos.append(f"No hay ventas registradas {self._texto_periodo(ventana)}: la venta diaria y la "
                                  "cobertura quedan sin calcular y el sugerido solo repone hasta el stock mínimo.")
        if not filas:
            reporte.avisos.append("Ningún producto está bajo el stock mínimo" + self._texto_filtros(categoria, almacen)
                                  + ".")
        return reporte

    # ------------------------------------------------------ 3. sin movimiento

    def sin_movimiento(self, dias: int = 90, categoria: str | None = None, almacen: str | None = None,
                       solo_con_stock: bool = True) -> ReporteSinMovimiento:
        ventana = self._ventana(dias, "dias")
        hoy = self.hoy()
        catalogo = self._catalogo()
        ventas = self._ventas(ventana, almacen)
        en_ventana = {r.producto_id: r for r in self.conector.resumen_movimientos(ventana.desde, hoy, almacen)}
        historico = {r.producto_id: r for r in self.conector.resumen_movimientos(None, None, almacen)}
        filas, sin_costo = [], 0
        for p in self._activos(catalogo, categoria):
            stock = p.stock_en(almacen)
            if solo_con_stock and stock <= _EPS:
                continue
            salidas = en_ventana[p.id].salidas if p.id in en_ventana else 0.0
            if p.id in ventas or salidas > _EPS:
                continue
            ultima = historico[p.id].ultima_salida if p.id in historico else None
            capital = stock * p.costo if p.costo is not None else None
            sin_costo += capital is None
            filas.append(FilaSinMovimiento(
                producto_id=p.id, codigo=p.codigo, nombre=p.nombre, categoria=self._categoria(p),
                stock=_r(stock, 4), costo=p.costo, moneda=self._moneda(p), capital_inmovilizado=_r(capital),
                ultima_salida=ultima, dias_sin_movimiento=(hoy - ultima).days if ultima else None,
            ))
        filas.sort(key=lambda f: (-(f.capital_inmovilizado or 0), f.nombre))
        capital_por_moneda: dict[str, float] = defaultdict(float)
        for f in filas:
            if f.capital_inmovilizado is not None:
                capital_por_moneda[f.moneda] += f.capital_inmovilizado
        reporte = ReporteSinMovimiento(
            tipo="sin_movimiento", titulo="Productos sin movimiento", periodo=ventana, dias=ventana.dias,
            generado_en=self.reloj(), productos=filas, capital_por_moneda={k: _r(v) for k, v in capital_por_moneda.items()},
            filtros=self._filtros(categoria=categoria, almacen=almacen, solo_con_stock=solo_con_stock),
            metodo=[
                f"Sin movimiento: productos activos sin ventas ni salidas de inventario {self._texto_periodo(ventana)}"
                + (", con stock mayor que 0" if solo_con_stock else "") + self._texto_filtros(categoria, almacen) + ".",
                "Capital inmovilizado = stock × costo unitario, sumado por moneda.",
                "Última salida: la más reciente de todo el historial de movimientos.",
            ],
            avisos=self._aviso_categoria(catalogo, categoria),
        )
        if not historico:
            reporte.avisos.append("El conector no informa movimientos de inventario: solo se consideran las ventas.")
        if sin_costo:
            reporte.avisos.append(f"{sin_costo} producto(s) sin costo: su capital inmovilizado no se puede calcular.")
        if not filas:
            reporte.avisos.append(f"Todos los productos{' con stock' if solo_con_stock else ''} tuvieron ventas o "
                                  f"salidas en los últimos {ventana.dias} días.")
        return reporte

    # ------------------------------------------------------------------ 4. ABC

    def abc(self, desde: date | None = None, hasta: date | None = None, ultimos_dias: int | None = None,
            porcentaje_a: float = 80, porcentaje_b: float = 15, categoria: str | None = None,
            almacen: str | None = None) -> ReporteABC:
        if not (0 < porcentaje_a < 100) or porcentaje_b < 0 or porcentaje_a + porcentaje_b > 100:
            raise ErrorValidacion(invalidos=[ErrorCampo(
                campo="porcentaje_a", mensaje="se necesita 0 < A < 100, B ≥ 0 y A + B ≤ 100")])
        periodo = self.periodo(desde, hasta, ultimos_dias, 90)
        catalogo = self._catalogo()
        ventas = sorted((v for v in self._ventas(periodo, almacen).values()
                         if v.monto_total > _EPS and self._en_categoria(catalogo.get(v.producto_id), categoria)),
                        key=lambda v: (-v.monto_total, v.nombre))
        total = sum(v.monto_total for v in ventas)
        corte_b = porcentaje_a + porcentaje_b
        filas, acumulado = [], 0.0
        for v in ventas:
            porcentaje = v.monto_total / total * 100
            previo, acumulado = acumulado, acumulado + porcentaje
            # La clase se decide con el acumulado ANTES del producto: el que cruza el umbral A sigue siendo A.
            clase = "A" if previo < porcentaje_a - _EPS else ("B" if previo < corte_b - _EPS else "C")
            filas.append(FilaABC(producto_id=v.producto_id, codigo=v.codigo, nombre=v.nombre,
                                 categoria=self._categoria(catalogo.get(v.producto_id)), monto=_r(v.monto_total),
                                 porcentaje=_r(porcentaje), porcentaje_acumulado=_r(acumulado), clase=clase))
        resumen = []
        for clase in ("A", "B", "C"):
            de_clase = [f for f in filas if f.clase == clase]
            monto = sum(v.monto_total for v, f in zip(ventas, filas) if f.clase == clase)
            resumen.append(ResumenClase(clase=clase, productos=len(de_clase), monto=_r(monto),
                                        porcentaje=_r(monto / total * 100 if total else 0)))
        vendidos = {f.producto_id for f in filas}
        sin_ventas = [ProductoSinVentas(producto_id=p.id, codigo=p.codigo, nombre=p.nombre, categoria=self._categoria(p))
                      for p in sorted(self._activos(catalogo, categoria), key=lambda p: p.nombre) if p.id not in vendidos]
        reporte = ReporteABC(
            tipo="abc", titulo="Clasificación ABC por monto vendido", periodo=periodo, generado_en=self.reloj(),
            porcentaje_a=porcentaje_a, porcentaje_b=porcentaje_b, total_monto=_r(total),
            resumen=resumen, productos=filas, sin_ventas=sin_ventas,
            filtros=self._filtros(categoria=categoria, almacen=almacen),
            metodo=[
                f"Monto vendido por producto {self._texto_periodo(periodo)}{self._texto_filtros(categoria, almacen)}, "
                f"de mayor a menor (total {dinero(total, self.moneda_base)}).",
                "Porcentaje = monto del producto / total × 100; acumulado = suma de los porcentajes hasta ese producto.",
                f"Clase según el acumulado ANTES del producto: menos de {numero(porcentaje_a)} % → A; menos de "
                f"{numero(corte_b)} % → B; el resto → C. Así el producto que cruza el umbral queda en la clase anterior.",
                f"Los {len(sin_ventas)} productos activos sin ventas en el período se listan aparte (serían C).",
            ],
            avisos=self._aviso_categoria(catalogo, categoria),
        )
        if not filas:
            reporte.sin_datos = True
            reporte.avisos.append(f"No hay ventas registradas {self._texto_periodo(periodo)}"
                                  f"{self._texto_filtros(categoria, almacen)}: no se puede hacer la clasificación ABC.")
        return reporte

    # --------------------------------------------- 5. rotación y valorización

    def rotacion(self, desde: date | None = None, hasta: date | None = None, ultimos_dias: int | None = None,
                 categoria: str | None = None, almacen: str | None = None) -> ReporteRotacion:
        periodo = self.periodo(desde, hasta, ultimos_dias, 90)
        hoy = self.hoy()
        catalogo = self._catalogo()
        ventas = self._ventas(periodo, almacen)
        desde_inicio = {r.producto_id: r for r in self.conector.resumen_movimientos(periodo.desde, None, almacen)}
        despues_fin = ({r.producto_id: r for r in self.conector.resumen_movimientos(
            periodo.hasta + timedelta(days=1), None, almacen)} if periodo.hasta < hoy else {})
        hay_ventas = any(p.id in ventas for p in self._activos(catalogo, categoria))

        valor: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
        rota: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
        sin_costo = sin_precio = inconsistentes = 0
        costo_cero: list[str] = []
        precio_cero: list[str] = []
        for p in self._activos(catalogo, categoria):
            moneda, grupo = self._moneda(p), self._categoria(p)
            stock = p.stock_en(almacen)
            v = valor[(grupo, moneda)]
            v["productos"] += 1
            v["unidades"] += stock
            if p.costo is not None:
                v["valor_costo"] += stock * p.costo
            else:
                sin_costo += 1
            if p.precio_venta is not None:
                v["valor_venta"] += stock * p.precio_venta
            else:
                sin_precio += 1
            # Un precio o costo en 0 con stock suele ser un dato sin cargar, no un valor real.
            if stock > _EPS and p.precio_venta is not None and p.precio_venta <= _EPS:
                precio_cero.append(p.nombre)
            if stock > _EPS and p.costo is not None and p.costo <= _EPS:
                costo_cero.append(p.nombre)

            neto_desde = desde_inicio[p.id].neto if p.id in desde_inicio else 0.0
            neto_despues = despues_fin[p.id].neto if p.id in despues_fin else 0.0
            inicial, final = stock - neto_desde, stock - neto_despues
            if inicial < -_EPS or final < -_EPS:
                inconsistentes += 1
            salidas = ((desde_inicio[p.id].salidas if p.id in desde_inicio else 0.0)
                       - (despues_fin[p.id].salidas if p.id in despues_fin else 0.0))
            vendidas = ventas[p.id].cantidad_total if p.id in ventas else 0.0
            r = rota[(grupo, moneda)]
            r["unidades_vendidas"] += vendidas
            r["otras_salidas"] += max(0.0, salidas - vendidas)
            if p.costo is not None:
                r["costo_vendido"] += vendidas * p.costo
                r["inventario_inicial"] += max(inicial, 0.0) * p.costo
                r["inventario_final"] += max(final, 0.0) * p.costo

        def fila_valor(grupo: str, moneda: str, d: dict[str, float]) -> FilaValorizacion:
            return FilaValorizacion(grupo=grupo, moneda=moneda, productos=int(d["productos"]),
                                    unidades=_r(d["unidades"], 4), valor_costo=_r(d["valor_costo"]),
                                    valor_venta=_r(d["valor_venta"]), margen_potencial=_r(d["valor_venta"] - d["valor_costo"]))

        def fila_rota(grupo: str, moneda: str, d: dict[str, float]) -> FilaRotacion:
            promedio = (d["inventario_inicial"] + d["inventario_final"]) / 2
            veces = d["costo_vendido"] / promedio if hay_ventas and promedio > _EPS else None
            return FilaRotacion(
                grupo=grupo, moneda=moneda, unidades_vendidas=_r(d["unidades_vendidas"], 4),
                otras_salidas=_r(d["otras_salidas"], 4), costo_vendido=_r(d["costo_vendido"]),
                inventario_inicial=_r(d["inventario_inicial"]), inventario_final=_r(d["inventario_final"]),
                inventario_promedio=_r(promedio), rotacion=_r(veces, 3),
                dias_inventario=_r(periodo.dias / veces, 1) if veces else None)

        def totales(por_grupo: dict) -> dict[str, dict[str, float]]:
            suma: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
            for (_, moneda), d in por_grupo.items():
                for k, x in d.items():
                    suma[moneda][k] += x
            return suma

        reporte = ReporteRotacion(
            tipo="rotacion", titulo="Rotación y valorización del inventario", periodo=periodo,
            generado_en=self.reloj(), filtros=self._filtros(categoria=categoria, almacen=almacen),
            valorizacion_total=[fila_valor("TOTAL", m, d) for m, d in sorted(totales(valor).items())],
            valorizacion=[fila_valor(g, m, d) for (g, m), d in sorted(valor.items())],
            rotacion_total=[fila_rota("TOTAL", m, d) for m, d in sorted(totales(rota).items())],
            rotacion=[fila_rota(g, m, d) for (g, m), d in sorted(rota.items())],
            metodo=[
                "Valorización = stock actual × costo (al costo) y × precio de venta (a precio de venta), por categoría "
                "y por moneda; nunca se suman monedas distintas.",
                f"Costo de lo vendido = unidades vendidas {self._texto_periodo(periodo)} × costo actual del producto "
                "(aproximación: no usa el costo histórico de cada venta).",
                "Inventario inicial = stock actual − neto de movimientos desde el inicio del período; inventario final = "
                "stock actual − neto de movimientos posteriores al período; ambos al costo actual.",
                "Rotación = costo de lo vendido / inventario promedio ((inicial + final) / 2). "
                f"Días de inventario = {periodo.dias} días / rotación.",
                "Otras salidas = salidas de inventario del período que no son ventas (producción, mermas, "
                "transferencias…); se informan aparte y no entran en la rotación.",
            ],
            avisos=self._aviso_categoria(catalogo, categoria),
        )
        if not hay_ventas:
            reporte.sin_datos = True
            reporte.avisos.append(f"No hay ventas registradas {self._texto_periodo(periodo)}"
                                  f"{self._texto_filtros(categoria, almacen)}: la rotación y los días de inventario "
                                  "no se pueden calcular. La valorización sí está disponible.")
        if sin_costo:
            reporte.avisos.append(f"{sin_costo} producto(s) sin costo: no entran en la valorización al costo ni en la rotación.")
        if sin_precio:
            reporte.avisos.append(f"{sin_precio} producto(s) sin precio de venta: no entran en la valorización a precio de venta.")
        for lista, que in ((precio_cero, "precio de venta"), (costo_cero, "costo")):
            if lista:
                ejemplos = ", ".join(f"«{n}»" for n in lista[:3]) + (f" y {len(lista) - 3} más" if len(lista) > 3 else "")
                reporte.avisos.append(f"{len(lista)} producto(s) con stock tienen {que} 0 ({ejemplos}): probablemente "
                                      f"falta cargarlo, y la valorización a {que} queda subestimada.")
        if inconsistentes:
            reporte.avisos.append(f"{inconsistentes} producto(s) dan un inventario reconstruido negativo: el kardex no "
                                  "cuadra con el stock actual; se tomaron como 0.")
        return reporte

    # ----------------------------------------------------- 6. resumen ejecutivo

    def resumen_ejecutivo(self, desde: date | None = None, hasta: date | None = None, ultimos_dias: int | None = None,
                          categoria: str | None = None, almacen: str | None = None) -> ResumenEjecutivo:
        periodo = self.periodo(desde, hasta, ultimos_dias, 30)
        rango = {"desde": periodo.desde, "hasta": periodo.hasta}
        vendidos = self.mas_vendidos(**rango, top=3, categoria=categoria, almacen=almacen)
        bajos = self.bajo_stock(categoria=categoria, almacen=almacen)
        quietos = self.sin_movimiento(categoria=categoria, almacen=almacen)
        clases = self.abc(**rango, categoria=categoria, almacen=almacen)
        rotacion = self.rotacion(**rango, categoria=categoria, almacen=almacen)
        mb = self.moneda_base

        hallazgos = []
        if vendidos.sin_datos:
            hallazgos.append(f"No hay ventas registradas {self._texto_periodo(periodo)}: no se pueden calcular el "
                             "ranking, la clasificación ABC ni la rotación.")
        else:
            hallazgos.append(f"Ventas {self._texto_periodo(periodo)}: {dinero(vendidos.total_monto, mb)} en "
                             f"{numero(vendidos.total_unidades)} unidades de {vendidos.productos_con_ventas} productos.")
            lider = vendidos.por_monto[0]
            hallazgos.append(f"El producto que más vendió es «{lider.nombre}»: {dinero(lider.monto, mb)} "
                             f"({numero(lider.participacion_monto)} % del total).")
            a = next(c for c in clases.resumen if c.clase == "A")
            hallazgos.append(f"{a.productos} productos (clase A) concentran el {numero(a.porcentaje)} % de lo vendido.")
        if bajos.productos:
            critico = bajos.productos[0]
            cobertura = (f"{numero(critico.cobertura_dias, 1)} días de cobertura" if critico.cobertura_dias is not None
                         else "cobertura indefinida")
            hallazgos.append(f"{len(bajos.productos)} productos están bajo el stock mínimo; el más crítico es "
                             f"«{critico.nombre}» ({numero(critico.stock_actual)} en stock, {cobertura}; "
                             f"sugerido reponer {numero(critico.sugerido_reponer)}).")
        else:
            hallazgos.append("Ningún producto está bajo el stock mínimo.")
        if quietos.productos:
            capital = ", ".join(dinero(v, m) for m, v in quietos.capital_por_moneda.items()) or "un capital sin costo"
            hallazgos.append(f"{len(quietos.productos)} productos con stock no se movieron en {quietos.dias} días e "
                             f"inmovilizan {capital}.")
        for total in rotacion.valorizacion_total:
            hallazgos.append(f"Inventario valorizado ({total.moneda}): {dinero(total.valor_costo, total.moneda)} al costo "
                             f"y {dinero(total.valor_venta, total.moneda)} a precio de venta.")
        for total in rotacion.rotacion_total:
            if total.rotacion is not None:
                hallazgos.append(f"Rotación ({total.moneda}): {numero(total.rotacion)} veces en {periodo.dias} días, "
                                 f"unos {numero(total.dias_inventario, 0)} días de inventario.")

        avisos: list[str] = []
        for parte in (vendidos, bajos, quietos, clases, rotacion):
            avisos.extend(a for a in parte.avisos if a not in avisos)
        return ResumenEjecutivo(
            tipo="resumen_ejecutivo", titulo="Resumen ejecutivo de inventario", periodo=periodo,
            generado_en=self.reloj(), filtros=self._filtros(categoria=categoria, almacen=almacen),
            sin_datos=vendidos.sin_datos, hallazgos=hallazgos, avisos=avisos,
            ventas={"monto_total": vendidos.total_monto, "unidades": vendidos.total_unidades,
                    "productos_con_ventas": vendidos.productos_con_ventas, "moneda": mb},
            top_por_monto=vendidos.por_monto, bajo_stock=bajos.productos[:3], productos_bajo_stock=len(bajos.productos),
            productos_sin_movimiento=len(quietos.productos), capital_inmovilizado=quietos.capital_por_moneda,
            abc=clases.resumen, valorizacion=rotacion.valorizacion_total, rotacion=rotacion.rotacion_total,
            metodo=[
                f"Combina: más vendidos y ABC {self._texto_periodo(periodo)}; bajo stock con la venta de los últimos "
                f"{bajos.dias_ventas} días; sin movimiento en {quietos.dias} días; rotación y valorización del período.",
                "Cada cifra sale del reporte correspondiente; los hallazgos se arman con reglas fijas, no con estimaciones.",
            ],
        )
