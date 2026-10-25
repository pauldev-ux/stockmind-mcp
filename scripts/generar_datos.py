"""Genera datos sintéticos de una ferretería para probar StockMind.

    python scripts/generar_datos.py                 # CSV en data/
    python scripts/generar_datos.py --formato xlsx  # Excel
    python scripts/generar_datos.py --forzar        # sobrescribe datos existentes

Los datos son SINTÉTICOS (así debe declararse en el documento de grado) y
reproducibles: la misma semilla produce exactamente los mismos archivos.

Modelo de demanda diaria por producto (unidades):
    λ(t) = base · tendencia(t) · semana(t) · temporada(t, perfil) · feriado(t)
    cantidad ~ Poisson(λ · G),  G ~ Gamma(k, 1/k)   → binomial negativa
más compras grandes ocasionales de constructoras en los productos de obra.

Temporadas (Santa Cruz, Bolivia):
    "seco":    obra gruesa, pico en agosto (época seca), valle en febrero.
    "lluvias": impermeabilizantes, canaletas, plástico; pico a fines de enero.
    "fin_año": pintura e iluminación, pico en diciembre.
    "plano":   variación anual leve.

Inventario: política de punto de pedido. Cuando el stock más lo pedido baja
del punto de pedido se emite una compra, que llega a los 3–7 días como
"entrada". Si no alcanza el stock, la venta se recorta (venta perdida), así
el saldo nunca es negativo. Al final, algunos productos tienen pedidos
demorados y quedan bajo el mínimo, para probar las alertas.
"""

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from stockmind.adapters.csv_excel import ALMACEN_POR_DEFECTO, escribir_tabla, fmt_num, ruta_tabla  # noqa: E402

SEMILLA = 42
INICIO = date(2025, 10, 25)
DIAS = 365

PROVEEDORES = ("Distribuidora Andina S.R.L.", "Importadora El Constructor", "Comercial Oriente Ltda.")


@dataclass(frozen=True)
class Catalogo:
    codigo: str
    nombre: str
    categoria: str
    unidad: str
    costo: float
    precio: float
    base: float  # unidades por día, en promedio
    perfil: str
    proveedor: int
    perecible: bool = False


# fmt: off
CATALOGO = [
    Catalogo("CON-001", "Cemento IP-30 bolsa 50 kg",          "Construcción", "bolsa", 52.0, 60.0, 24, "seco", 0, True),
    Catalogo("CON-002", "Fierro corrugado 8 mm x 12 m",        "Construcción", "barra", 38.0, 46.0, 28, "seco", 1),
    Catalogo("CON-003", "Fierro corrugado 10 mm x 12 m",       "Construcción", "barra", 58.0, 69.0, 16, "seco", 1),
    Catalogo("CON-004", "Fierro corrugado 12 mm x 12 m",       "Construcción", "barra", 84.0, 99.0,  9, "seco", 1),
    Catalogo("CON-005", "Alambre de amarre N° 16",             "Construcción", "kg",    13.0, 17.0, 14, "seco", 1),
    Catalogo("CON-006", "Clavos de 2 1/2 pulgadas",            "Construcción", "kg",    14.0, 19.0,  8, "seco", 2),
    Catalogo("CON-007", "Yeso bolsa 25 kg",                    "Construcción", "bolsa", 22.0, 28.0,  6, "seco", 0, True),
    Catalogo("CON-008", "Cal hidratada bolsa 25 kg",           "Construcción", "bolsa", 25.0, 32.0,  4, "seco", 0, True),
    Catalogo("PLO-001", "Tubo PVC presión 1/2\" x 6 m",        "Plomería",     "pieza", 18.0, 24.0,  9, "plano", 2),
    Catalogo("PLO-002", "Tubo PVC presión 3/4\" x 6 m",        "Plomería",     "pieza", 24.0, 31.0,  6, "plano", 2),
    Catalogo("PLO-003", "Tubo PVC sanitario 4\" x 3 m",        "Plomería",     "pieza", 45.0, 58.0,  5, "seco", 2),
    Catalogo("PLO-004", "Codo PVC 1/2\" x 90°",                "Plomería",     "unidad", 1.5,  2.5, 22, "plano", 2),
    Catalogo("PLO-005", "Llave de paso 1/2\"",                 "Plomería",     "unidad", 28.0, 39.0,  3, "plano", 2),
    Catalogo("PLO-006", "Pegamento para PVC 240 ml",           "Plomería",     "unidad", 17.0, 24.0,  4, "plano", 2, True),
    Catalogo("PLO-007", "Cinta teflón 3/4\"",                  "Plomería",     "unidad",  2.0,  3.5, 18, "plano", 2),
    Catalogo("PLO-008", "Grifo para lavamanos cromado",        "Plomería",     "unidad", 65.0, 89.0, 1.2, "plano", 1),
    Catalogo("ELE-001", "Cable THW 12 AWG rollo 100 m",        "Electricidad", "rollo", 420.0, 520.0, 0.8, "seco", 1),
    Catalogo("ELE-002", "Cable 14 AWG por metro",              "Electricidad", "metro",  3.2,  4.5, 38, "plano", 1),
    Catalogo("ELE-003", "Interruptor simple",                  "Electricidad", "unidad",  9.0, 14.0,  6, "plano", 1),
    Catalogo("ELE-004", "Tomacorriente doble",                 "Electricidad", "unidad", 11.0, 16.0,  6, "plano", 1),
    Catalogo("ELE-005", "Foco LED 9 W",                        "Electricidad", "unidad",  9.0, 15.0, 12, "fin_año", 1),
    Catalogo("ELE-006", "Cinta aislante 20 m",                 "Electricidad", "unidad",  4.0,  7.0, 10, "plano", 1),
    Catalogo("ELE-007", "Breaker termomagnético 20 A",         "Electricidad", "unidad", 38.0, 52.0, 1.5, "plano", 1),
    Catalogo("PIN-001", "Pintura látex blanca 4 L",            "Pinturas",     "galón", 95.0, 125.0, 4, "fin_año", 0, True),
    Catalogo("PIN-002", "Pintura látex blanca balde 18 L",     "Pinturas",     "balde", 380.0, 480.0, 1, "fin_año", 0, True),
    Catalogo("PIN-003", "Esmalte sintético 1 L",               "Pinturas",     "litro", 45.0, 60.0, 2.5, "fin_año", 0, True),
    Catalogo("PIN-004", "Rodillo de 9 pulgadas",               "Pinturas",     "unidad", 18.0, 26.0,  3, "fin_año", 0),
    Catalogo("PIN-005", "Brocha de 3 pulgadas",                "Pinturas",     "unidad", 10.0, 15.0,  4, "fin_año", 0),
    Catalogo("PIN-006", "Thinner 1 L",                         "Pinturas",     "litro", 15.0, 21.0,  4, "fin_año", 0),
    Catalogo("IMP-001", "Impermeabilizante acrílico 4 L",      "Impermeabilizantes", "galón", 140.0, 185.0, 1.5, "lluvias", 0, True),
    Catalogo("IMP-002", "Membrana asfáltica rollo 10 m²",      "Impermeabilizantes", "rollo", 260.0, 330.0, 0.6, "lluvias", 0),
    Catalogo("IMP-003", "Canaleta PVC 4 m",                    "Impermeabilizantes", "pieza", 55.0, 72.0, 2, "lluvias", 2),
    Catalogo("IMP-004", "Silicona transparente 280 ml",        "Impermeabilizantes", "unidad", 18.0, 26.0, 5, "lluvias", 2, True),
    Catalogo("IMP-005", "Plástico negro 4 m de ancho",         "Impermeabilizantes", "metro",  6.0,  9.0, 14, "lluvias", 2),
    Catalogo("HER-001", "Martillo de uña 16 oz",               "Herramientas", "unidad", 45.0, 65.0, 1.2, "plano", 1),
    Catalogo("HER-002", "Pala punta redonda",                  "Herramientas", "unidad", 55.0, 75.0, 1.5, "seco", 1),
    Catalogo("HER-003", "Carretilla 90 L",                     "Herramientas", "unidad", 380.0, 490.0, 0.25, "seco", 1),
    Catalogo("HER-004", "Flexómetro 5 m",                      "Herramientas", "unidad", 22.0, 32.0, 2, "plano", 1),
    Catalogo("HER-005", "Disco de corte 4 1/2\"",              "Herramientas", "unidad",  8.0, 12.0, 8, "seco", 1),
    Catalogo("FIJ-001", "Tornillo autorroscante caja x 100",   "Fijaciones",   "caja",  18.0, 26.0, 3, "plano", 2),
]
# fmt: on

# Feriados nacionales y de Santa Cruz en el periodo (la tienda casi no vende).
FERIADOS = {
    date(2025, 11, 2), date(2025, 12, 25), date(2026, 1, 1), date(2026, 1, 22),
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 4, 3), date(2026, 5, 1),
    date(2026, 6, 4), date(2026, 6, 21), date(2026, 8, 6), date(2026, 9, 24),
}
# Días de poca venta: vísperas y la semana de Carnaval.
DIAS_FLOJOS = {date(2025, 12, 24), date(2025, 12, 31), date(2026, 2, 14), date(2026, 2, 15), date(2026, 2, 18)}

# Lunes … domingo. El domingo abre medio día.
SEMANA = (1.00, 0.95, 0.95, 1.00, 1.10, 1.35, 0.35)

# Cambios de precio: (fecha, categoría, factor). El costo sube antes que el precio.
AUMENTOS_COSTO = [(date(2026, 3, 15), "Construcción", 1.05)]
AUMENTOS_PRECIO = [(date(2026, 4, 1), "Construcción", 1.06)]

MOTIVOS_MERMA = ("Merma: bolsa rota", "Rotura en manipuleo", "Diferencia en conteo físico", "Producto dañado por humedad")


def _ciclo(t: date, pico: date) -> float:
    """cos con máximo en el día del año de `pico`."""
    return math.cos(2 * math.pi * (t.timetuple().tm_yday - pico.timetuple().tm_yday) / 365.25)


def factor_temporada(perfil: str, t: date) -> float:
    match perfil:
        case "seco":
            return 1 + 0.35 * _ciclo(t, date(2026, 8, 15))
        case "lluvias":
            return max(0.15, 1 + 0.75 * _ciclo(t, date(2026, 1, 25)))
        case "fin_año":
            dias_a_diciembre = min(abs((t - date(y, 12, 15)).days) for y in (t.year - 1, t.year, t.year + 1))
            return 1 + 0.10 * _ciclo(t, date(2026, 8, 15)) + 0.6 * math.exp(-(dias_a_diciembre / 14) ** 2)
        case _:
            return 1 + 0.08 * _ciclo(t, date(2026, 8, 15))


def factor_dia(t: date) -> float:
    if t in FERIADOS:
        return 0.1
    if t in DIAS_FLOJOS:
        return 0.6
    return SEMANA[t.weekday()]


def generar(salida: Path, formato: str = "csv", semilla: int = SEMILLA, inicio: date = INICIO,
            dias: int = DIAS, catalogo: list[Catalogo] | None = None) -> dict:
    rng = np.random.default_rng(semilla)
    catalogo = catalogo or CATALOGO
    fin = inicio + timedelta(days=dias - 1)
    fechas = [inicio + timedelta(days=i) for i in range(dias)]

    n = len(catalogo)
    costo = np.array([c.costo for c in catalogo])
    precio = np.array([c.precio for c in catalogo])
    media = np.array([c.base for c in catalogo])
    # Política de inventario por producto (en días de venta promedio).
    demora_tipica = 5
    stock_minimo = np.ceil(media * 7)                      # una semana de cobertura
    punto_pedido = np.ceil(media * (demora_tipica + 7))
    stock_objetivo = np.ceil(media * 30)                    # se pide hasta un mes
    stock = np.floor(stock_objetivo * rng.uniform(0.5, 0.9, n))
    # Productos cuyo último pedido se demora: terminan bajo el mínimo.
    demorados = set(rng.choice(n, size=max(1, n // 8), replace=False).tolist())

    pedidos: list[dict] = []      # pendientes: {"i", "llega", "cantidad", "doc"}
    movimientos: list[dict] = []
    ventas: list[dict] = []
    detalle: list[dict] = []
    contador = {"COM": 0, "VEN": 0, "AJU": 0, "M": 0}

    def doc(prefijo: str) -> str:
        contador[prefijo] += 1
        return f"{prefijo}-{contador[prefijo]:06d}"

    def mover(i: int, tipo: str, sentido: str, cantidad: float, t: date, motivo: str, documento: str,
              costo_u: float | None = None, lote: str = "", vence: date | None = None) -> None:
        stock[i] += cantidad if sentido == "+" else -cantidad
        assert stock[i] >= 0, "el generador nunca debe dejar stock negativo"
        contador["M"] += 1
        movimientos.append({
            "id": f"M{contador['M']:06d}", "producto_id": f"P{i + 1:04d}", "tipo": tipo, "sentido": sentido,
            "cantidad": fmt_num(cantidad), "fecha": t.isoformat(), "motivo": motivo, "saldo": fmt_num(stock[i]),
            "almacen": ALMACEN_POR_DEFECTO, "lote": lote, "fecha_vencimiento": vence.isoformat() if vence else "",
            "costo_unitario": fmt_num(costo_u) if costo_u is not None else "", "documento": documento,
        })

    # Saldo inicial: entra como una entrada con su propio lote.
    saldo_inicial = stock.copy()
    stock[:] = 0
    for i, c in enumerate(catalogo):
        if saldo_inicial[i] > 0:
            mover(i, "entrada", "+", float(saldo_inicial[i]), inicio, "Saldo inicial", "INI-000001",
                  costo[i], f"INI-{c.codigo}", inicio + timedelta(days=365) if c.perecible else None)

    for t in fechas:
        for f, cat, factor in AUMENTOS_COSTO:
            if t == f:
                costo[[k for k, c in enumerate(catalogo) if c.categoria == cat]] *= factor
        for f, cat, factor in AUMENTOS_PRECIO:
            if t == f:
                precio[[k for k, c in enumerate(catalogo) if c.categoria == cat]] *= factor

        # 1) Llegan las compras (por la mañana).
        for pedido in [p for p in pedidos if p["llega"] == t]:
            i = pedido["i"]
            c = catalogo[i]
            mover(i, "entrada", "+", pedido["cantidad"], t, f"Compra a {PROVEEDORES[c.proveedor]}",
                  pedido["doc"], round(float(costo[i]), 2), f"{pedido['doc']}-{c.codigo}",
                  t + timedelta(days=365) if c.perecible else None)
            pedidos.remove(pedido)

        # 2) Demanda del día.
        progreso = (t - inicio).days / 365.25
        lineas: list[tuple[int, int]] = []
        for i, c in enumerate(catalogo):
            lam = c.base * (1 + 0.08 * progreso) * factor_dia(t) * factor_temporada(c.perfil, t)
            lam *= rng.gamma(4.0, 1 / 4.0)   # sobredispersión (día bueno / día malo)
            cantidad = int(rng.poisson(lam))
            if c.perfil == "seco" and c.categoria == "Construcción" and t not in FERIADOS and rng.random() < 0.015:
                cantidad += int(c.base * rng.uniform(4, 10))   # pedido de una constructora
            cantidad = min(cantidad, int(stock[i]))              # sin stock no se vende
            if cantidad <= 0:
                continue
            # Varias personas compran el mismo producto en el día.
            partes = min(cantidad, max(1, int(rng.poisson(math.sqrt(cantidad)))))
            reparto = 1 + rng.multinomial(cantidad - partes, np.ones(partes) / partes)
            lineas.extend((i, int(q)) for q in reparto)

        # 3) Agrupar las líneas en tickets.
        if lineas:
            n_tickets = max(1, round(len(lineas) / 1.8))
            asignacion = rng.integers(0, n_tickets, len(lineas))
            for k in range(n_tickets):
                items: dict[int, int] = {}
                for (i, q), a in zip(lineas, asignacion):
                    if a == k:
                        items[i] = items.get(i, 0) + q
                if not items:
                    continue
                venta_id = doc("VEN")
                total = 0.0
                for i, q in sorted(items.items()):
                    pu = round(float(precio[i]), 2)
                    subtotal = round(pu * q, 2)
                    total += subtotal
                    detalle.append({"venta_id": venta_id, "producto_id": f"P{i + 1:04d}", "cantidad": str(q),
                                    "precio_unitario": fmt_num(pu), "subtotal": fmt_num(subtotal)})
                    mover(i, "salida", "-", q, t, f"Venta {venta_id}", venta_id)
                ventas.append({"id": venta_id, "fecha": t.isoformat(), "cliente": "",
                               "almacen": ALMACEN_POR_DEFECTO, "total": fmt_num(round(total, 2))})

        # 4) Mermas y diferencias de inventario.
        for i, c in enumerate(catalogo):
            if stock[i] >= 2 and rng.random() < 0.004:
                q = int(rng.integers(1, 3))
                mover(i, "ajuste", "-", q, t, str(rng.choice(MOTIVOS_MERMA)), doc("AJU"))
            elif rng.random() < 0.0008:
                mover(i, "ajuste", "+", 1, t, "Sobrante en conteo físico", doc("AJU"))

        # 5) Reposición al cierre del día.
        for i, c in enumerate(catalogo):
            en_camino = sum(p["cantidad"] for p in pedidos if p["i"] == i)
            if stock[i] + en_camino <= punto_pedido[i]:
                demora = int(rng.integers(3, 8))
                if i in demorados and (fin - t).days < 30:
                    demora += 30   # el proveedor no entrega a tiempo
                pedidos.append({"i": i, "llega": t + timedelta(days=demora),
                                "cantidad": float(stock_objetivo[i] - stock[i] - en_camino), "doc": doc("COM")})

    productos = [{
        "id": f"P{i + 1:04d}", "codigo": c.codigo, "nombre": c.nombre, "categoria": c.categoria, "unidad": c.unidad,
        "precio_venta": fmt_num(round(float(precio[i]), 2)), "costo": fmt_num(round(float(costo[i]), 2)),
        "stock_actual": fmt_num(stock[i]), "stock_minimo": fmt_num(stock_minimo[i]),
        "proveedor": PROVEEDORES[c.proveedor], "almacen": ALMACEN_POR_DEFECTO, "activo": "1",
    } for i, c in enumerate(catalogo)]

    tablas = {
        "productos": pd.DataFrame(productos),
        "movimientos": pd.DataFrame(movimientos),
        "ventas": pd.DataFrame(ventas),
        "ventas_detalle": pd.DataFrame(detalle),
    }
    for nombre, df in tablas.items():
        escribir_tabla(df, ruta_tabla(salida, nombre, formato), nombre)

    return {
        "productos": len(productos), "movimientos": len(movimientos), "ventas": len(ventas),
        "lineas_venta": len(detalle), "desde": inicio.isoformat(), "hasta": fin.isoformat(),
        "bajo_minimo": [c.codigo for i, c in enumerate(catalogo) if stock[i] < stock_minimo[i]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Genera datos sintéticos de ferretería para StockMind.")
    parser.add_argument("--salida", type=Path, default=RAIZ / "data", help="carpeta de salida (por defecto data/)")
    parser.add_argument("--formato", choices=("csv", "xlsx"), default="csv")
    parser.add_argument("--semilla", type=int, default=SEMILLA)
    parser.add_argument("--forzar", action="store_true", help="sobrescribir archivos existentes")
    args = parser.parse_args()

    existentes = [ruta_tabla(args.salida, t, args.formato) for t in ("productos", "movimientos")]
    if any(r.exists() for r in existentes) and not args.forzar:
        sys.exit(f"Ya hay datos en {args.salida}. Usa --forzar para sobrescribirlos.")

    resumen = generar(args.salida, args.formato, args.semilla)
    print(f"Datos generados en {args.salida} ({args.formato}), semilla {args.semilla}:")
    print(f"  {resumen['productos']} productos, {resumen['ventas']} ventas ({resumen['lineas_venta']} líneas), "
          f"{resumen['movimientos']} movimientos")
    print(f"  Periodo: {resumen['desde']} a {resumen['hasta']}")
    print(f"  Bajo el mínimo al cierre: {', '.join(resumen['bajo_minimo']) or 'ninguno'}")


if __name__ == "__main__":
    main()
