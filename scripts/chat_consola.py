"""Chat por consola con el proveedor de IA REAL (usa tu cuota). Prueba manual del agente cliente MCP.

    .\\.venv\\Scripts\\python.exe scripts\\chat_consola.py              # MCP en proceso (un solo comando)
    .\\.venv\\Scripts\\python.exe scripts\\chat_consola.py --mcp-http   # contra `stockmind-mcp --http`

Lee LLM_PROVIDER, LLM_MODEL y LLM_API_KEY del .env (o --proveedor / --modelo).
Cuando el modelo prepara una operación, la consola pregunta si confirmarla: eso es el
"botón" del usuario; el modelo nunca puede confirmar.
Comandos: /salir · /nueva (nueva conversación) · /uso (tokens de la conversación)
Con --tiempos muestra cuánto tardó cada etapa (llamadas al modelo, tools MCP y el total).
"""

import argparse
import asyncio
import json
import logging
import sys
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from stockmind.agente.agente import Agente  # noqa: E402
from stockmind.agente.cliente_mcp import ClienteMCP  # noqa: E402
from stockmind.agente.historial import HistorialStore  # noqa: E402
from stockmind.agente.proveedores import crear_proveedor  # noqa: E402
from stockmind.api.config import ApiSettings  # noqa: E402

GRIS, VERDE, AMARILLO, ROJO, FIN = "\033[90m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"


async def conectar(pila: AsyncExitStack, config: ApiSettings, por_http: bool) -> ClienteMCP:
    if por_http:
        return await pila.enter_async_context(ClienteMCP.conectar_http(config.mcp_url,
                                                                      config.mcp_token.get_secret_value()))
    # Servidor MCP en el mismo proceso (lado servidor). El agente igual le habla por MCP.
    from stockmind.composicion import construir_servicio
    from stockmind.config.settings import get_settings
    from stockmind.mcp_server.server import crear_servidor

    servidor = crear_servidor(construir_servicio(get_settings(), origen="consola"))
    return await pila.enter_async_context(ClienteMCP.en_proceso(servidor))


def imprimir_tiempos(tiempos: list[dict]) -> None:
    print(f"{GRIS}  {'etapa':<10} {'detalle':<26} {'1er token':>9} {'total':>8}  extra")
    for t in tiempos:
        primero = f"{t['primer_token']:.2f} s" if t.get("primer_token") is not None else ""
        if t["etapa"] == "modelo":
            extra = f"{t['entrada']} tok entrada · {t['salida']} salida · {t['fragmentos_texto']} fragmentos de texto"
        elif t["etapa"] == "tool":
            extra = f"{t['caracteres']} caracteres al modelo"
        else:
            extra = ""
        print(f"  {t['etapa']:<10} {t['nombre'][:26]:<26} {primero:>9} {t['total']:>6.2f} s  {extra}")
    print(FIN, end="")
    # Una línea legible por máquina (stderr) para comparar mediciones.
    print("TIEMPOS_JSON " + json.dumps(tiempos, ensure_ascii=False), file=sys.stderr)


async def preguntar(texto: str) -> str:
    return (await asyncio.to_thread(input, texto)).strip()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Chat por consola con StockMind (proveedor real).")
    parser.add_argument("--mcp-http", action="store_true", help="usar el servidor MCP HTTP (MCP_URL, MCP_TOKEN)")
    parser.add_argument("--proveedor", help="anthropic | gemini (por defecto LLM_PROVIDER)")
    parser.add_argument("--modelo", help="modelo (por defecto LLM_MODEL o el del proveedor)")
    parser.add_argument("--tiempos", action="store_true",
                        help="mostrar cuánto tarda cada etapa (modelo, tools MCP, total del mensaje)")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    # Las peticiones HTTP (INFO) ensucian la conversación: solo WARNING o más.
    for ruidoso in ("httpx", "httpx2", "httpcore", "google_genai"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)
    # Los reintentos ya se muestran como "(reintentando…)"; su WARNING queda para los logs de la API.
    logging.getLogger("stockmind.agente.llm").setLevel(logging.ERROR)

    config = ApiSettings()
    parametros = config.parametros_proveedor() | {"modelo": args.modelo or config.llm_model}
    proveedor = crear_proveedor(args.proveedor or config.llm_provider, **parametros)
    agente = Agente(proveedor, HistorialStore(config.confirmaciones_db), max_iteraciones=config.max_iteraciones_tools,
                    historial_turnos=config.historial_turnos, max_filas_resultado=config.max_filas_resultado,
                    medir_tiempos=args.tiempos)
    print(f"StockMind · {proveedor.nombre} ({proveedor.modelo}) · MCP {'HTTP' if args.mcp_http else 'en proceso'}")
    if getattr(proveedor, "respaldos", None):
        print(f"{GRIS}Respaldo si está saturado: {', '.join(proveedor.respaldos)}{FIN}")
    print(f"{GRIS}Escribe tu pregunta. Comandos: /salir /nueva /uso{FIN}\n")

    async with AsyncExitStack() as pila:
        mcp = await conectar(pila, config, args.mcp_http)
        conversacion: str | None = None
        while True:
            texto = await preguntar("Tú> ")
            if texto in ("/salir", "/exit", ""):
                if texto:
                    break
                continue
            if texto == "/nueva":
                conversacion = None
                print(f"{GRIS}(nueva conversación){FIN}")
                continue
            if texto == "/uso":
                uso = agente.historial.uso(conversacion) if conversacion else []
                print(f"{GRIS}{json.dumps(uso, ensure_ascii=False, indent=2) if uso else '(sin uso todavía)'}{FIN}")
                continue

            pendientes, tiempos, modelos = [], [], []
            print("StockMind> ", end="", flush=True)
            async for ev in agente.conversar(mcp, "consola", texto, conversacion):
                tipo, datos = ev["evento"], ev["datos"]
                if tipo == "conversacion":
                    conversacion = datos["id"]
                elif tipo == "texto":
                    print(datos["texto"], end="", flush=True)
                elif tipo == "tool_inicio":
                    print(f"\n{GRIS}  · {datos['nombre']}({json.dumps(datos['argumentos'], ensure_ascii=False)}){FIN}",
                          flush=True)
                elif tipo == "tool_fin" and not datos["ok"]:
                    print(f"{AMARILLO}  ! {datos['nombre']}: {datos['resumen']}{FIN}", flush=True)
                elif tipo == "reintento":
                    tiempos.append({"etapa": "fallido", "nombre": datos["modelo_fallido"], "total": datos["duracion"]})
                    if datos["cambio_de_modelo"]:
                        print(f"\n{GRIS}  (modelo saturado; probando con {datos['modelo']}…){FIN}", flush=True)
                    else:
                        print(f"\n{GRIS}  (reintentando en {datos['espera']:.0f} s… intento {datos['intento']}){FIN}",
                              flush=True)
                elif tipo == "tiempo":
                    tiempos.append(datos)
                elif tipo == "uso" and datos["modelo"] not in modelos:
                    modelos.append(datos["modelo"])
                elif tipo == "accion_pendiente":
                    pendientes.append(datos)
                elif tipo == "error":
                    print(f"\n{ROJO}[{datos['error']}] {datos['mensaje']}{FIN}")
                elif tipo == "fin":
                    u = datos["uso"]
                    respondio = f"respondió {' → '.join(modelos)} · " if modelos else ""
                    print(f"\n{GRIS}({respondio}tokens: {u['entrada']} entrada · {u['salida']} salida · "
                          f"{u['cache_lectura']} de caché){FIN}")
                    if args.tiempos:
                        imprimir_tiempos(tiempos)
                    print()

            for accion in pendientes:
                print(f"\n{AMARILLO}Acción pendiente: {accion['resumen']}{FIN}")
                vista = accion.get("vista_previa") or {}
                if "stock_resultante" in vista:
                    print(f"{AMARILLO}  Stock: {vista.get('stock_actual')} → {vista['stock_resultante']}"
                          f"{' · ' + vista['motivo'] if vista.get('motivo') else ''}{FIN}")
                for aviso in accion.get("advertencias") or []:
                    print(f"{AMARILLO}  - {aviso}{FIN}")
                vence = datetime.fromisoformat(accion["vence"]).astimezone() if accion.get("vence") else None
                if vence and vence <= datetime.now().astimezone():
                    print(f"{ROJO}  El token venció a las {vence:%H:%M}; pide la operación de nuevo.{FIN}")
                hasta = f" (vence a las {vence:%H:%M})" if vence else ""
                respuesta = await preguntar(f"¿Confirmar?{hasta} [s/N] ")
                accion_de = agente.confirmar if respuesta.lower() in ("s", "si", "sí") else agente.cancelar
                resultado = await accion_de(mcp, "consola", conversacion, accion["token"])
                color = VERDE if resultado["ok"] else ROJO
                mensaje = resultado["resultado"].get("mensaje", "")
                print(f"{color}→ {resultado['estado']}: {mensaje}{FIN}")
                if resultado["resultado"].get("referencia"):
                    print(f"{AMARILLO}  Referencia en la bandeja: {resultado['resultado']['referencia']}{FIN}")
                print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print("\nHasta luego.")
    except ValueError as e:
        sys.exit(str(e))
