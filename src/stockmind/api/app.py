"""API HTTP de StockMind para el chat con IA, el panel web y la integración con un ERP.

Todo lo de inventario pasa por el servidor MCP (las mismas tools que usa el modelo):
el chat, los reportes y la bandeja. La API no importa `stockmind.application`.

Rutas (todas bajo /api/v1 y con `Authorization: Bearer <token>`):
  POST   /chat                          SSE: conversacion, texto, reintento, tool_inicio, tool_fin,
                                        accion_pendiente, uso, error, fin
  POST   /acciones/confirmar | cancelar  {conversacion_id, token}  (el usuario, nunca el modelo)
  GET    /conversaciones · GET/DELETE /conversaciones/{id}
  GET    /reportes/{tipo}                mas_vendidos | bajo_stock | sin_movimiento | abc | rotacion | resumen
  GET    /bandeja · POST /bandeja/{ref}/registrar | descartar
  GET    /salud
"""

import json
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from stockmind.agente.agente import Agente, ErrorAgente
from stockmind.agente.cliente_mcp import ClienteMCP, ConexionPersistente, ErrorMCP, ResultadoMCP
from stockmind.agente.historial import HistorialStore
from stockmind.agente.llm import LLMProvider
from stockmind.agente.proveedores import crear_proveedor
from stockmind.api.auth import Identidad, JWTFirmado, TokenEstatico, VerificadorCompuesto, VerificadorIdentidad
from stockmind.api.config import ApiSettings

log = logging.getLogger("stockmind.api")

ConectarMCP = Callable[[], AbstractAsyncContextManager[ClienteMCP]]

REPORTES = {
    "mas_vendidos": "reporte_mas_vendidos", "bajo_stock": "reporte_bajo_stock",
    "sin_movimiento": "reporte_sin_movimiento", "abc": "reporte_abc", "rotacion": "reporte_rotacion",
    "resumen": "resumen_ejecutivo",
}
# Errores de negocio de las tools → código HTTP
_ESTADOS = {"ErrorValidacion": 422, "ProductoNoEncontrado": 404, "OperacionNoEncontrada": 404,
            "TransicionInvalida": 409, "BandejaNoDisponible": 409, "TokenInvalido": 409,
            "EscrituraNoHabilitada": 409}


class ErrorAPI(Exception):
    def __init__(self, estado: int, error: str, mensaje: str, **extra: Any):
        super().__init__(mensaje)
        self.estado, self.error, self.mensaje, self.extra = estado, error, mensaje, extra


def _respuesta_error(estado: int, error: str, mensaje: str, **extra: Any) -> JSONResponse:
    cabeceras = {"WWW-Authenticate": "Bearer"} if estado == 401 else None
    return JSONResponse({"ok": False, "error": error, "mensaje": mensaje, **extra}, status_code=estado,
                        headers=cabeceras)


class PeticionChat(BaseModel):
    mensaje: str = Field(min_length=1)
    conversacion_id: str | None = None


class PeticionAccion(BaseModel):
    conversacion_id: str
    token: str = Field(min_length=1)


class PeticionRegistrar(BaseModel):
    observacion: str | None = None


class PeticionDescartar(BaseModel):
    motivo: str | None = None


def construir_verificador(config: ApiSettings) -> VerificadorIdentidad:
    verificadores: list[VerificadorIdentidad] = []
    if config.api_token.get_secret_value():
        verificadores.append(TokenEstatico(config.api_token.get_secret_value()))
    if config.erp_jwt_secreto.get_secret_value():
        verificadores.append(JWTFirmado(config.erp_jwt_secreto.get_secret_value(), config.erp_jwt_emisor,
                                        config.erp_jwt_audiencia))
    if not verificadores:
        raise ValueError("Configura API_TOKEN (o ERP_JWT_SECRETO) en .env para proteger la API.")
    return VerificadorCompuesto(*verificadores)


def _coaccionar(nombre: str, valor: str, propiedad: dict[str, Any]) -> Any:
    tipos = {propiedad.get("type")} | {opcion.get("type") for opcion in propiedad.get("anyOf", [])}
    try:
        if "integer" in tipos:
            return int(valor)
        if "number" in tipos:
            return float(valor.replace(",", "."))
        if "boolean" in tipos:
            return valor.strip().lower() in ("1", "true", "si", "sí", "yes")
    except ValueError:
        raise ErrorAPI(422, "DatosInvalidos", f"El parámetro «{nombre}» debe ser numérico (recibido: {valor!r}).") from None
    return valor


def _datos_o_error(resultado: ResultadoMCP) -> dict[str, Any]:
    datos = resultado.datos
    if resultado.ok:
        return datos
    error = str(datos.get("error", "ErrorHerramienta"))
    raise ErrorAPI(_ESTADOS.get(error, 400), error, str(datos.get("mensaje", "La operación falló.")),
                   **{k: v for k, v in datos.items() if k not in ("ok", "error", "mensaje")})


def _mensaje_publico(m: Any) -> dict[str, Any]:
    return {
        "rol": m.rol, "nota_sistema": m.nota_sistema, "texto": m.texto,
        "herramientas": [b.nombre for b in m.llamadas],
        "resultados": [{"nombre": b.nombre, "es_error": b.es_error} for b in m.bloques if b.tipo == "resultado"],
    }


def crear_app(config: ApiSettings | None = None, *, proveedor: LLMProvider | None = None,
              conectar_mcp: ConectarMCP | None = None, historial: HistorialStore | None = None,
              verificador: VerificadorIdentidad | None = None) -> FastAPI:
    config = config or ApiSettings()
    verificador = verificador or construir_verificador(config)
    proveedor = proveedor or crear_proveedor(config.llm_provider, **config.parametros_proveedor())
    historial = historial or HistorialStore(config.confirmaciones_db)
    persistente: ConexionPersistente | None = None
    if conectar_mcp is None:
        token_mcp = config.mcp_token.get_secret_value()
        # Una sola sesión MCP para todas las peticiones (antes se abría una por pregunta).
        persistente = ConexionPersistente(lambda: ClienteMCP.conectar_http(config.mcp_url, token_mcp))
        conectar_mcp = persistente
    agente = Agente(proveedor, historial, max_iteraciones=config.max_iteraciones_tools,
                    historial_turnos=config.historial_turnos, max_filas_resultado=config.max_filas_resultado)

    @asynccontextmanager
    async def ciclo_de_vida(_: FastAPI):
        yield
        if persistente is not None:
            await persistente.cerrar()

    app = FastAPI(title="StockMind API", version="0.1.0", lifespan=ciclo_de_vida,
                  description="Chat con IA (agente cliente MCP), reportes y bandeja de StockMind.")
    app.state.conexion_mcp = persistente
    if config.origenes_cors:
        app.add_middleware(CORSMiddleware, allow_origins=config.origenes_cors, allow_credentials=False,
                           allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type"])

    @app.exception_handler(ErrorAPI)
    async def _error_api(_: Request, e: ErrorAPI) -> JSONResponse:
        return _respuesta_error(e.estado, e.error, e.mensaje, **e.extra)

    @app.exception_handler(ErrorAgente)
    async def _error_agente(_: Request, e: ErrorAgente) -> JSONResponse:
        return _respuesta_error(e.estado, e.error, e.mensaje)

    @app.exception_handler(ErrorMCP)
    async def _error_mcp(_: Request, e: ErrorMCP) -> JSONResponse:
        return _respuesta_error(502, "ErrorMCP", e.mensaje)

    @app.exception_handler(RequestValidationError)
    async def _error_validacion(_: Request, e: RequestValidationError) -> JSONResponse:
        campos = [".".join(str(x) for x in err["loc"][1:]) or str(err["loc"][0]) for err in e.errors()]
        return _respuesta_error(422, "DatosInvalidos", f"Datos inválidos o faltantes: {', '.join(campos)}.")

    @app.exception_handler(StarletteHTTPException)
    async def _error_http(_: Request, e: StarletteHTTPException) -> JSONResponse:
        return _respuesta_error(e.status_code, "ErrorHTTP", str(e.detail))

    seguridad = HTTPBearer(auto_error=False)

    def identidad(credenciales: HTTPAuthorizationCredentials | None = Depends(seguridad)) -> Identidad:
        if credenciales is None:
            raise ErrorAPI(401, "NoAutenticado", "Falta el encabezado Authorization: Bearer <token>.")
        encontrada = verificador.verificar(credenciales.credentials)
        if encontrada is None:
            raise ErrorAPI(401, "NoAutenticado", "Token inválido o vencido.")
        return encontrada

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(identidad)])

    # --------------------------------------------------------------- chat

    @api.post("/chat", summary="Enviar un mensaje (respuesta en streaming SSE)")
    async def chat(peticion: PeticionChat, quien: Identidad = Depends(identidad)) -> EventSourceResponse:
        if len(peticion.mensaje) > config.max_largo_mensaje:
            raise ErrorAPI(422, "MensajeMuyLargo", f"El mensaje supera los {config.max_largo_mensaje} caracteres.")
        if peticion.conversacion_id and historial.obtener(quien.usuario, peticion.conversacion_id) is None:
            raise ErrorAPI(404, "ConversacionNoEncontrada", "No existe esa conversación.")

        async def eventos():
            def sse(_tipo: str, **datos: Any) -> dict[str, str]:
                return {"event": _tipo, "data": json.dumps(datos, ensure_ascii=False, default=str)}
            try:
                async with conectar_mcp() as mcp:
                    async for ev in agente.conversar(mcp, quien.usuario, peticion.mensaje, peticion.conversacion_id):
                        yield sse(ev["evento"], **ev["datos"])
            except ErrorMCP as e:
                yield sse("error", error="ErrorMCP", mensaje=e.mensaje)
                yield sse("fin", conversacion_id=peticion.conversacion_id)
            except Exception:
                log.exception("Error inesperado en /chat")
                yield sse("error", error="ErrorInterno", mensaje="Ocurrió un error interno; revisa el log de la API.")
                yield sse("fin", conversacion_id=peticion.conversacion_id)

        return EventSourceResponse(eventos())

    # --------------------------------------------------- acciones del usuario

    @api.post("/acciones/confirmar", summary="El usuario confirma una vista previa (botón)")
    async def confirmar(peticion: PeticionAccion, quien: Identidad = Depends(identidad)) -> dict[str, Any]:
        async with conectar_mcp() as mcp:
            return await agente.confirmar(mcp, quien.usuario, peticion.conversacion_id, peticion.token)

    @api.post("/acciones/cancelar", summary="El usuario cancela una vista previa (botón)")
    async def cancelar(peticion: PeticionAccion, quien: Identidad = Depends(identidad)) -> dict[str, Any]:
        async with conectar_mcp() as mcp:
            return await agente.cancelar(mcp, quien.usuario, peticion.conversacion_id, peticion.token)

    # ---------------------------------------------------------- conversaciones

    @api.get("/conversaciones", summary="Conversaciones del usuario")
    async def conversaciones(quien: Identidad = Depends(identidad)) -> dict[str, Any]:
        lista = historial.listar(quien.usuario)
        return {"ok": True, "conversaciones": [c.model_dump(mode="json", exclude={"usuario"}) for c in lista]}

    @api.get("/conversaciones/{conversacion_id}", summary="Historial, acciones y uso de tokens")
    async def conversacion(conversacion_id: str, quien: Identidad = Depends(identidad)) -> dict[str, Any]:
        c = historial.obtener(quien.usuario, conversacion_id)
        if c is None:
            raise ErrorAPI(404, "ConversacionNoEncontrada", "No existe esa conversación.")
        return {
            "ok": True, "conversacion": c.model_dump(mode="json", exclude={"usuario"}),
            "mensajes": [_mensaje_publico(m) for m in historial.mensajes(c.id)],
            "acciones": [a.model_dump(mode="json", exclude={"usuario", "datos"}) for a in historial.acciones(c.id)],
            "uso": historial.uso(c.id),
        }

    @api.delete("/conversaciones/{conversacion_id}", summary="Borrar una conversación")
    async def borrar(conversacion_id: str, quien: Identidad = Depends(identidad)) -> dict[str, Any]:
        if not historial.borrar(quien.usuario, conversacion_id):
            raise ErrorAPI(404, "ConversacionNoEncontrada", "No existe esa conversación.")
        return {"ok": True}

    # ------------------------------------------------- reportes y bandeja (MCP)

    @api.get("/reportes/{tipo}", summary="Reportes (las mismas tools MCP que usa el chat)")
    async def reporte(tipo: str, request: Request) -> dict[str, Any]:
        herramienta = REPORTES.get(tipo)
        if herramienta is None:
            raise ErrorAPI(404, "ReporteDesconocido", f"Reporte desconocido. Opciones: {', '.join(REPORTES)}.")
        async with conectar_mcp() as mcp:
            esquema = next(h.esquema for h in await mcp.herramientas() if h.nombre == herramienta)
            propiedades = esquema.get("properties", {})
            argumentos = {}
            for nombre, valor in request.query_params.items():
                if nombre not in propiedades:
                    raise ErrorAPI(422, "ParametroDesconocido",
                                   f"Parámetro desconocido «{nombre}». Válidos: {', '.join(propiedades)}.")
                argumentos[nombre] = _coaccionar(nombre, valor, propiedades[nombre])
            return _datos_o_error(await mcp.llamar(herramienta, argumentos))

    @api.get("/bandeja", summary="Operaciones de la bandeja de salida")
    async def bandeja(estado: str = "pendiente", tipo: str | None = None) -> dict[str, Any]:
        async with conectar_mcp() as mcp:
            argumentos = {"estado": estado} | ({"tipo": tipo} if tipo else {})
            return _datos_o_error(await mcp.llamar("listar_pendientes", argumentos))

    @api.post("/bandeja/{referencia}/registrar", summary="El usuario marca una pendiente como registrada (botón)")
    async def registrar(referencia: str, peticion: PeticionRegistrar) -> dict[str, Any]:
        async with conectar_mcp() as mcp:
            return _datos_o_error(await mcp.llamar(
                "marcar_registrada", {"id": referencia} | ({"observacion": peticion.observacion}
                                                          if peticion.observacion else {})))

    @api.post("/bandeja/{referencia}/descartar", summary="El usuario descarta una pendiente (botón)")
    async def descartar(referencia: str, peticion: PeticionDescartar) -> dict[str, Any]:
        async with conectar_mcp() as mcp:
            return _datos_o_error(await mcp.llamar(
                "descartar_pendiente", {"id": referencia} | ({"motivo": peticion.motivo} if peticion.motivo else {})))

    # ------------------------------------------------------------------ salud

    @api.get("/salud", summary="Estado del servidor MCP, del conector y del proveedor de IA")
    async def salud() -> JSONResponse:
        info: dict[str, Any] = {
            "proveedor": {"nombre": proveedor.nombre, "modelo": proveedor.modelo,
                          "configurado": proveedor.nombre == "falso" or bool(config.llm_api_key.get_secret_value())},
            "mcp": {"url": config.mcp_url},
        }
        try:
            async with conectar_mcp() as mcp:
                herramientas = await mcp.herramientas()
                campos = await mcp.llamar("campos_obligatorios_producto", {})
            info["mcp"].update(ok=True, herramientas=len(herramientas))
            info["conector"] = {"ok": campos.ok, "nombre": campos.datos.get("conector"),
                                "modo_escritura": campos.datos.get("modo_escritura"),
                                **({"mensaje": campos.datos.get("mensaje")} if not campos.ok else {})}
        except Exception as e:
            info["mcp"].update(ok=False, mensaje=getattr(e, "mensaje", type(e).__name__))
            info["conector"] = {"ok": False}
        info["ok"] = info["mcp"]["ok"] and info["conector"]["ok"]
        return JSONResponse(info, status_code=200 if info["ok"] else 503)

    app.include_router(api)
    app.state.agente, app.state.historial, app.state.proveedor = agente, historial, proveedor
    return app
