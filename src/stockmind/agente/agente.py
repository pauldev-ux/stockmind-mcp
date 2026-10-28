"""Agente de chat: un CLIENTE MCP que usa las tools de StockMind a pedido de un modelo de IA.

Human in the loop (obligatorio):
- El modelo nunca recibe las tools que solo puede ejecutar el usuario (HERRAMIENTAS_SOLO_USUARIO).
  Si igual las pide por nombre, se rechazan sin ejecutarse.
- Cuando una tool devuelve una vista previa con token, el agente emite `accion_pendiente`
  (con el token, para los botones del frontend) y al modelo le devuelve el resultado SIN el token.
- La confirmación la ejecuta el usuario desde un endpoint aparte (`confirmar` / `cancelar`),
  y el resultado se agrega al historial como nota del sistema.

El agente no importa nada de `stockmind.application`: todo pasa por MCP.
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from stockmind.agente.cliente_mcp import ClienteMCP
from stockmind.agente.historial import Accion, HistorialStore
from stockmind.agente.llm import ErrorLLM, LLMProvider
from stockmind.agente.modelos import (
    BloqueLlamada,
    BloqueResultado,
    BloqueTexto,
    EventoReintento,
    Mensaje,
    RespuestaLLM,
    Uso,
)
from stockmind.agente.prompt import NOTA_ACCION_PENDIENTE, PROMPT_SISTEMA

log = logging.getLogger(__name__)

#: Tools que cambian estado y SOLO puede ejecutar el usuario (botones del frontend).
HERRAMIENTAS_SOLO_USUARIO = frozenset({
    "confirmar_operacion", "cancelar_operacion", "marcar_registrada", "descartar_pendiente",
})


class ErrorAgente(Exception):
    def __init__(self, mensaje: str, estado: int = 400, error: str = "ErrorAgente"):
        super().__init__(mensaje)
        self.mensaje, self.estado, self.error = mensaje, estado, error


def evento(_tipo: str, **datos: Any) -> dict[str, Any]:
    return {"evento": _tipo, "datos": datos}


def _resumen_accion(operacion: str, datos: dict[str, Any]) -> str:
    vista = datos.get("vista_previa") or {}
    if operacion == "crear_producto":
        texto = f"Crear el producto «{vista.get('nombre', '?')}»"
    else:
        producto = (vista.get("producto") or {}).get("nombre", "?")
        texto = (f"{str(vista.get('tipo', 'movimiento')).capitalize()} de {vista.get('cantidad', '?')} "
                 f"de «{producto}»")
    if datos.get("modo_escritura") == "bandeja":
        texto += " (quedará PENDIENTE de registrar en el ERP)"
    return texto


NOTA_RECORTE = ("Se muestran solo las primeras {n} filas de cada lista, en el orden en que las devolvió la "
                "herramienta; los totales son de la lista completa. El detalle completo está en el panel.")
_LARGO_RESUMEN = 2500


def recortar_para_modelo(datos: Any, max_filas: int) -> tuple[Any, bool]:
    """Listas largas → sus primeras `max_filas` filas y `<clave>_total` con el largo real.
    Devuelve (datos recortados, si se recortó algo). Lo demás (totales, metodo, avisos) queda igual."""
    if isinstance(datos, dict):
        salida, recortado = {}, False
        for clave, valor in datos.items():
            if isinstance(valor, list) and max_filas > 0 and len(valor) > max_filas:
                salida[clave] = [recortar_para_modelo(v, max_filas)[0] for v in valor[:max_filas]]
                salida[f"{clave}_total"] = len(valor)
                recortado = True
            else:
                salida[clave], sub = recortar_para_modelo(valor, max_filas)
                recortado = recortado or sub
        return salida, recortado
    if isinstance(datos, list):
        partes = [recortar_para_modelo(v, max_filas) for v in datos]
        return [p for p, _ in partes], any(r for _, r in partes)
    return datos, False


def compactar_esquema(esquema: Any) -> Any:
    """Esquema JSON de MCP → la misma información en menos tokens: sin `title` (repite el nombre),
    sin `default: null`, y `anyOf [X, null]` → X (el parámetro sigue siendo opcional: no está en
    `required`)."""
    if isinstance(esquema, list):
        return [compactar_esquema(v) for v in esquema]
    if not isinstance(esquema, dict):
        return esquema
    salida: dict[str, Any] = {}
    for clave, valor in esquema.items():
        if clave == "title" and isinstance(valor, str):
            continue
        if clave == "default" and valor is None:
            continue
        if clave == "properties" and isinstance(valor, dict):
            salida[clave] = {nombre: compactar_esquema(v) for nombre, v in valor.items()}
        else:
            salida[clave] = compactar_esquema(valor)
    opciones = salida.get("anyOf")
    if isinstance(opciones, list) and {"type": "null"} in opciones:
        resto = [o for o in opciones if o != {"type": "null"}]
        del salida["anyOf"]
        if len(resto) == 1 and isinstance(resto[0], dict):
            salida = {**resto[0], **salida}
        elif resto:
            salida["anyOf"] = resto
    return salida


def _es_pregunta(m: Mensaje) -> bool:
    return (m.rol == "usuario" and not m.nota_sistema and bool(m.texto)
            and not any(isinstance(b, BloqueResultado) for b in m.bloques))


def _resumir(mensajes: list[Mensaje]) -> str:
    lineas = []
    for m in mensajes:
        if _es_pregunta(m):
            lineas.append(f"- Usuario: {m.texto[:200]}")
        elif m.nota_sistema and m.texto:
            lineas.append(f"  {m.texto[:300]}")
        elif m.rol == "asistente" and m.texto:
            lineas.append(f"  StockMind: {m.texto[:300]}")
    texto = "\n".join(lineas)
    if len(texto) > _LARGO_RESUMEN:
        texto = "…" + texto[-_LARGO_RESUMEN:]
    return ("[Resumen del sistema] Mensajes anteriores de esta conversación (recortados para ahorrar tokens; "
            "si necesitas un dato de ahí, vuelve a consultarlo con las herramientas):\n" + texto)


def ventana_historial(mensajes: list[Mensaje], turnos: int, *, sin_crudo_previo: bool = False) -> list[Mensaje]:
    """Los últimos `turnos` turnos (cada uno empieza con una pregunta del usuario) y un resumen de los
    anteriores. Si se recorta y el proveedor liga su razonamiento al contexto exacto (Anthropic), los
    mensajes del modelo de turnos previos se envían en forma neutra: sus bloques de thinking ya no
    corresponderían al nuevo prefijo."""
    inicios = [i for i, m in enumerate(mensajes) if _es_pregunta(m)]
    if turnos <= 0 or len(inicios) <= turnos:
        return list(mensajes)
    corte = inicios[-turnos]
    recientes = mensajes[corte:]
    if sin_crudo_previo:
        recientes = [m.model_copy(update={"crudo": None}) if m.rol == "asistente" else m for m in recientes]
    resumen = Mensaje(rol="usuario", nota_sistema=True, bloques=[BloqueTexto(texto=_resumir(mensajes[:corte]))])
    return [resumen, *recientes]


def _resumen_resultado(datos: dict[str, Any]) -> str:
    if datos.get("mensaje"):
        return str(datos["mensaje"])[:200]
    claves = [k for k in datos if k not in ("ok", "metodo", "avisos")]
    return ("Devolvió: " + ", ".join(claves[:6]))[:200]


class Agente:
    def __init__(self, proveedor: LLMProvider, historial: HistorialStore, *, max_iteraciones: int = 8,
                 prompt: str = PROMPT_SISTEMA, largo_max_resultado: int = 60_000, medir_tiempos: bool = False,
                 historial_turnos: int = 8, max_filas_resultado: int = 15):
        self.proveedor = proveedor
        # Menos tokens por llamada: solo los últimos N turnos (más un resumen) y las primeras filas
        # de los resultados grandes. 0 = sin límite.
        self.historial_turnos = historial_turnos
        self.max_filas_resultado = max_filas_resultado
        # Con medir_tiempos se emiten eventos `tiempo` (etapa modelo/tool/tools_list/mensaje), en segundos.
        self.medir_tiempos = medir_tiempos
        self.historial = historial
        self.max_iteraciones = max_iteraciones
        self.prompt = prompt
        self.largo_max_resultado = largo_max_resultado

    # ------------------------------------------------------------------ chat

    async def conversar(self, mcp: ClienteMCP, usuario: str, texto: str,
                        conversacion_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
        """Un mensaje del usuario → eventos: conversacion, texto, reintento, tool_inicio, tool_fin,
        accion_pendiente, uso, tiempo (opcional), error y fin.

        Si una tool deja una acción pendiente, el turno termina ahí (sin otra llamada al modelo).
        """
        conversacion = (self.historial.obtener(usuario, conversacion_id) if conversacion_id
                        else self.historial.crear(usuario, texto))
        if conversacion is None:
            raise ErrorAgente("No existe esa conversación.", 404, "ConversacionNoEncontrada")
        yield evento("conversacion", id=conversacion.id, titulo=conversacion.titulo)
        inicio_mensaje = time.perf_counter()

        pregunta = Mensaje(rol="usuario", bloques=[BloqueTexto(texto=texto)])
        self.historial.agregar(conversacion.id, pregunta)
        # El historial completo queda guardado; al modelo se le envía la ventana (fija durante el mensaje).
        mensajes = ventana_historial(self.historial.mensajes(conversacion.id), self.historial_turnos,
                                     sin_crudo_previo=getattr(self.proveedor, "contexto_ligado", False))

        t0 = time.perf_counter()
        herramientas = [h.model_copy(update={"esquema": compactar_esquema(h.esquema)})
                        for h in await mcp.herramientas() if h.nombre not in HERRAMIENTAS_SOLO_USUARIO]
        if self.medir_tiempos:
            yield evento("tiempo", etapa="tools_list", nombre="tools/list", total=time.perf_counter() - t0)
        permitidas = {h.nombre for h in herramientas}
        total = Uso(proveedor=self.proveedor.nombre, modelo=self.proveedor.modelo)
        acciones = 0

        for iteracion in range(1, self.max_iteraciones + 1):
            respuesta: RespuestaLLM | None = None
            t0, primer_fragmento, fragmentos = time.perf_counter(), None, 0
            try:
                async for parte in self.proveedor.responder(self.prompt, mensajes, herramientas):
                    if primer_fragmento is None and not isinstance(parte, EventoReintento):
                        primer_fragmento = time.perf_counter() - t0
                    if isinstance(parte, RespuestaLLM):
                        respuesta = parte
                    elif isinstance(parte, EventoReintento):
                        yield evento("reintento", **parte.model_dump())
                    elif parte.texto:
                        fragmentos += 1
                        yield evento("texto", texto=parte.texto)
            except ErrorLLM as e:
                yield evento("error", error="ErrorProveedor", mensaje=e.mensaje)
                break
            self.historial.agregar(conversacion.id, respuesta.mensaje)
            mensajes.append(respuesta.mensaje)
            self.historial.registrar_uso(conversacion.id, respuesta.uso)
            total = total.sumar(respuesta.uso)
            yield evento("uso", **respuesta.uso.model_dump())
            if self.medir_tiempos:
                yield evento("tiempo", etapa="modelo", nombre=respuesta.uso.modelo, iteracion=iteracion,
                             primer_token=primer_fragmento, total=time.perf_counter() - t0,
                             fragmentos_texto=fragmentos, entrada=respuesta.uso.entrada, salida=respuesta.uso.salida)

            if respuesta.motivo == "rechazo":
                yield evento("error", error="RespuestaRechazada",
                             mensaje="El proveedor de IA se negó a responder esta consulta"
                                     + (f": {respuesta.detalle}" if respuesta.detalle else "."))
                break
            llamadas = respuesta.mensaje.llamadas
            if not llamadas:
                if respuesta.motivo == "limite_tokens":
                    yield evento("error", error="RespuestaCortada",
                                 mensaje="La respuesta se cortó por su largo; pide algo más concreto.")
                break

            resultados, pendientes_ahora = [], 0
            for llamada in llamadas:
                yield evento("tool_inicio", id=llamada.id, nombre=llamada.nombre, argumentos=llamada.argumentos)
                t0 = time.perf_counter()
                bloque, accion, datos = await self._ejecutar(mcp, conversacion.id, usuario, llamada, permitidas,
                                                             truncada=respuesta.motivo == "limite_tokens")
                if self.medir_tiempos:
                    yield evento("tiempo", etapa="tool", nombre=llamada.nombre, total=time.perf_counter() - t0,
                                 caracteres=len(bloque.contenido))
                resultados.append(bloque)
                if accion is not None:
                    acciones += 1
                    pendientes_ahora += 1
                    yield evento("accion_pendiente", **accion)
                # `datos`: el resultado completo (sin token) para el panel; el modelo recibió la versión recortada.
                yield evento("tool_fin", id=llamada.id, nombre=llamada.nombre, ok=not bloque.es_error
                             and datos.get("ok", True) is not False, resumen=_resumen_resultado(datos),
                             datos={k: v for k, v in datos.items() if k not in ("token", "instruccion")})
            resultado = Mensaje(rol="usuario", bloques=resultados)
            self.historial.agregar(conversacion.id, resultado)
            mensajes.append(resultado)
            if pendientes_ahora:
                # El turno termina con la vista previa: el usuario decide YA, con el token recién emitido.
                # Esperar otra llamada al modelo (que puede tardar minutos si está saturado) hacía que el
                # token venciera antes de que el usuario viera el botón.
                break
        else:
            yield evento("error", error="LimiteIteraciones",
                         mensaje=f"Se alcanzó el límite de {self.max_iteraciones} usos de herramientas para este "
                                 "mensaje. Reformula la consulta o divídela en partes.")
        if self.medir_tiempos:
            yield evento("tiempo", etapa="mensaje", nombre="total", total=time.perf_counter() - inicio_mensaje)
        yield evento("fin", conversacion_id=conversacion.id, acciones_pendientes=acciones, uso=total.model_dump())

    async def _ejecutar(self, mcp: ClienteMCP, conversacion_id: str, usuario: str, llamada: BloqueLlamada,
                        permitidas: set[str], truncada: bool) -> tuple[BloqueResultado, dict | None, dict]:
        def error(nombre_error: str, mensaje: str) -> tuple[BloqueResultado, None, dict]:
            datos = {"ok": False, "error": nombre_error, "mensaje": mensaje}
            return (BloqueResultado(id=llamada.id, nombre=llamada.nombre, es_error=True,
                                    contenido=json.dumps(datos, ensure_ascii=False)), None, datos)

        if llamada.nombre in HERRAMIENTAS_SOLO_USUARIO:
            log.warning("El modelo intentó ejecutar %s en la conversación %s: bloqueado", llamada.nombre,
                        conversacion_id)
            return error("NoPermitido", "Esta acción solo la puede ejecutar el usuario con los botones de la "
                                        "interfaz. Tú no puedes confirmar, cancelar, marcar ni descartar operaciones.")
        if llamada.nombre not in permitidas:
            return error("HerramientaDesconocida", f"No existe la herramienta «{llamada.nombre}».")
        if truncada or "_invalido" in llamada.argumentos:
            return error("ArgumentosInvalidos", "Los argumentos llegaron incompletos o ilegibles; vuelve a intentarlo.")

        try:
            resultado = await mcp.llamar(llamada.nombre, llamada.argumentos)
        except Exception as e:          # p. ej. el servidor MCP se cayó a mitad de la conversación
            log.exception("Falló la tool %s", llamada.nombre)
            return error("ErrorMCP", f"No se pudo ejecutar la herramienta ({type(e).__name__}).")
        datos = resultado.datos
        accion = None
        para_modelo = datos
        if datos.get("requiere_confirmacion") and datos.get("token"):
            registrada = self.historial.registrar_accion(
                conversacion_id, usuario, str(datos.get("operacion", llamada.nombre)), datos["token"],
                _resumen_accion(str(datos.get("operacion", "")), datos),
                {k: v for k, v in datos.items() if k not in ("token", "instruccion")})
            accion = {"id": registrada.id, "conversacion_id": conversacion_id, "operacion": registrada.operacion,
                      "resumen": registrada.resumen, "modo_escritura": datos.get("modo_escritura"),
                      "vista_previa": datos.get("vista_previa"), "advertencias": datos.get("advertencias", []),
                      "vence": datos.get("vence"), "token": datos["token"]}
            # El modelo NUNCA ve el token: no podría usarlo, pero así no puede filtrarlo ni intentarlo.
            para_modelo = {**{k: v for k, v in datos.items() if k not in ("token", "instruccion")},
                           "accion_id": registrada.id, "instruccion": NOTA_ACCION_PENDIENTE}
        para_modelo, recortado = recortar_para_modelo(para_modelo, self.max_filas_resultado)
        if recortado:
            para_modelo = {**para_modelo, "nota_recorte": NOTA_RECORTE.format(n=self.max_filas_resultado)}
        contenido = json.dumps(para_modelo, ensure_ascii=False, default=str)
        if len(contenido) > self.largo_max_resultado:
            contenido = contenido[: self.largo_max_resultado] + '… [resultado recortado por su tamaño]'
        return (BloqueResultado(id=llamada.id, nombre=llamada.nombre, contenido=contenido,
                                es_error=resultado.es_error), accion, datos)

    # --------------------------------------------- acciones del usuario (HITL)

    async def confirmar(self, mcp: ClienteMCP, usuario: str, conversacion_id: str, token: str) -> dict[str, Any]:
        return await self._resolver(mcp, usuario, conversacion_id, token, "confirmar_operacion",
                                    ("confirmó", "confirmar", "confirmada"))

    async def cancelar(self, mcp: ClienteMCP, usuario: str, conversacion_id: str, token: str) -> dict[str, Any]:
        return await self._resolver(mcp, usuario, conversacion_id, token, "cancelar_operacion",
                                    ("canceló", "cancelar", "cancelada"))

    async def _resolver(self, mcp: ClienteMCP, usuario: str, conversacion_id: str, token: str, herramienta: str,
                        verbos: tuple[str, str, str]) -> dict[str, Any]:
        pasado, infinitivo, estado_ok = verbos
        if self.historial.obtener(usuario, conversacion_id) is None:
            raise ErrorAgente("No existe esa conversación.", 404, "ConversacionNoEncontrada")
        accion: Accion | None = self.historial.tomar_accion(conversacion_id, usuario, token)
        if accion is None:
            raise ErrorAgente("No hay una acción pendiente con ese token en esta conversación "
                              "(ya se confirmó, se canceló o no existe).", 404, "AccionNoEncontrada")
        try:
            resultado = await mcp.llamar(herramienta, {"token": token})
        except Exception:
            self.historial.resolver_accion(accion.id, "pendiente", {"ok": False, "error": "ErrorMCP"})
            raise
        datos = resultado.datos
        estado = estado_ok if resultado.ok else "fallida"
        self.historial.resolver_accion(accion.id, estado, datos)
        detalle = json.dumps({k: v for k, v in datos.items() if k != "token"}, ensure_ascii=False, default=str)
        nota = (f"[Nota del sistema] El usuario {pasado} la operación «{accion.resumen}». Resultado: {detalle}"
                if resultado.ok else
                f"[Nota del sistema] El usuario intentó {infinitivo} la operación «{accion.resumen}», pero falló: "
                f"{detalle}")
        self.historial.agregar(conversacion_id, Mensaje(rol="usuario", nota_sistema=True,
                                                        bloques=[BloqueTexto(texto=nota)]))
        return {"ok": resultado.ok, "accion_id": accion.id, "estado": estado, "resultado": datos}
