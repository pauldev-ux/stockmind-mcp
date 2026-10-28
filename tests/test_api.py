"""API FastAPI con LLM falso y el servidor MCP real en proceso (sin llamadas a proveedores)."""

import json
import time
from contextlib import asynccontextmanager

import jwt
import pytest
from fastapi.testclient import TestClient

from stockmind.adapters.csv_excel import CsvExcelConnector
from stockmind.agente.cliente_mcp import ClienteMCP, ErrorMCP
from stockmind.agente.historial import HistorialStore
from stockmind.agente.proveedores.falso import Paso, ProveedorFalso
from stockmind.api.app import crear_app
from stockmind.api.config import ApiSettings
from stockmind.application.bandeja import MemoriaBandejaStore
from stockmind.application.confirmaciones import GestorConfirmaciones, MemoriaConfirmacionStore
from stockmind.application.servicio import InventarioService
from stockmind.mcp_server.server import crear_servidor
from tests.conftest import HOY
from tests.test_bandeja import PRODUCTO, ConectorBandejaFalso

TOKEN = "token-de-prueba-123"
SECRETO_ERP = "secreto-compartido-con-el-erp-de-32-bytes-o-mas"
CABECERA = {"Authorization": f"Bearer {TOKEN}"}
MOVIMIENTO = {"producto": "PLO-001", "tipo": "salida", "cantidad": 2, "motivo": "Venta"}


def _app(servicio, tmp_path, guion=None, **config):
    falso = ProveedorFalso(guion if guion is not None else [Paso(texto="Hola.")])
    servidor = crear_servidor(servicio)
    ajustes = ApiSettings(_env_file=None, api_token=TOKEN, llm_provider="falso", erp_jwt_secreto=SECRETO_ERP,
                          erp_jwt_audiencia="stockmind", **config)
    app = crear_app(ajustes, proveedor=falso, historial=HistorialStore(tmp_path / "chat.db"),
                    conectar_mcp=lambda: ClienteMCP.en_proceso(servidor))
    return TestClient(app), falso


def _sse(respuesta) -> list[tuple[str, dict]]:
    eventos, nombre = [], None
    for linea in respuesta.text.splitlines():
        if linea.startswith("event:"):
            nombre = linea.split(":", 1)[1].strip()
        elif linea.startswith("data:"):
            eventos.append((nombre, json.loads(linea.split(":", 1)[1])))
    return eventos


def _chat(cliente, mensaje="hola", conversacion_id=None, cabecera=CABECERA):
    r = cliente.post("/api/v1/chat", json={"mensaje": mensaje, "conversacion_id": conversacion_id}, headers=cabecera)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    return _sse(r)


def _jwt(sub="maria", **extra):
    claims = {"sub": sub, "aud": "stockmind", "exp": int(time.time()) + 300, "empresa": 4} | extra
    return {"Authorization": f"Bearer {jwt.encode(claims, SECRETO_ERP, algorithm='HS256')}"}


# --------------------------------------------------------------- seguridad

def test_sin_token_o_con_token_invalido_da_401(servicio, tmp_path):
    cliente, _ = _app(servicio, tmp_path)
    sin = cliente.get("/api/v1/conversaciones")
    assert sin.status_code == 401 and sin.json()["error"] == "NoAutenticado" and sin.headers["www-authenticate"]
    assert cliente.get("/api/v1/conversaciones", headers={"Authorization": "Bearer otro"}).status_code == 401
    assert cliente.post("/api/v1/chat", json={"mensaje": "x"}).status_code == 401


def test_jwt_firmado_por_el_erp(servicio, tmp_path):
    cliente, _ = _app(servicio, tmp_path)
    assert cliente.get("/api/v1/conversaciones", headers=_jwt()).status_code == 200
    vencido = _jwt(exp=int(time.time()) - 10)
    assert cliente.get("/api/v1/conversaciones", headers=vencido).status_code == 401
    otra_audiencia = _jwt(aud="otro-sistema")
    assert cliente.get("/api/v1/conversaciones", headers=otra_audiencia).status_code == 401


def test_cors_configurable(servicio, tmp_path):
    cliente, _ = _app(servicio, tmp_path, cors_origins="http://localhost:5173")
    permitido = cliente.options("/api/v1/chat", headers={"Origin": "http://localhost:5173",
                                                         "Access-Control-Request-Method": "POST"})
    assert permitido.headers.get("access-control-allow-origin") == "http://localhost:5173"
    ajeno = cliente.options("/api/v1/chat", headers={"Origin": "http://malo.example",
                                                     "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in ajeno.headers


def test_mensaje_demasiado_largo_o_vacio(servicio, tmp_path):
    cliente, _ = _app(servicio, tmp_path, max_largo_mensaje=10)
    assert cliente.post("/api/v1/chat", json={"mensaje": "x" * 11}, headers=CABECERA).json()["error"] == "MensajeMuyLargo"
    assert cliente.post("/api/v1/chat", json={"mensaje": ""}, headers=CABECERA).status_code == 422


# -------------------------------------------------------------------- chat

def test_chat_en_streaming(servicio, tmp_path):
    cliente, _ = _app(servicio, tmp_path, [Paso(llamadas=[("listar_bajo_stock", {})]), Paso(texto="Hay dos.")])
    eventos = _chat(cliente, "¿qué falta?")
    tipos = [t for t, _ in eventos]
    assert tipos[0] == "conversacion" and tipos[-1] == "fin"
    assert {"tool_inicio", "tool_fin", "texto", "uso"} <= set(tipos)
    assert "".join(d["texto"] for t, d in eventos if t == "texto").strip() == "Hay dos."


def test_accion_pendiente_y_confirmacion_por_el_usuario(servicio, tmp_path, datos_dir):
    cliente, falso = _app(servicio, tmp_path, [Paso(llamadas=[("registrar_movimiento", MOVIMIENTO)]),
                                               Paso(texto="Confírmalo con el botón.")])
    eventos = _chat(cliente, "vende 2 tubos")
    conv = eventos[0][1]["id"]
    (accion,) = [d for t, d in eventos if t == "accion_pendiente"]
    assert accion["token"] and accion["vista_previa"]["stock_resultante"] == 3
    assert CsvExcelConnector(datos_dir).obtener_producto("P0002").stock_actual == 5

    # El token va en el cuerpo, nunca en la URL.
    r = cliente.post("/api/v1/acciones/confirmar", json={"conversacion_id": conv, "token": accion["token"]},
                     headers=CABECERA)
    assert r.status_code == 200 and r.json()["estado"] == "confirmada"
    assert CsvExcelConnector(datos_dir).obtener_producto("P0002").stock_actual == 3
    otra_vez = cliente.post("/api/v1/acciones/confirmar", json={"conversacion_id": conv, "token": accion["token"]},
                            headers=CABECERA)
    assert otra_vez.status_code == 404 and otra_vez.json()["error"] == "AccionNoEncontrada"

    detalle = cliente.get(f"/api/v1/conversaciones/{conv}", headers=CABECERA).json()
    assert detalle["acciones"][0]["estado"] == "confirmada" and detalle["mensajes"][-1]["nota_sistema"]
    assert detalle["uso"][0]["llamadas"] == 1 and accion["token"] not in json.dumps(detalle)


def test_cancelar(servicio, tmp_path, datos_dir):
    cliente, _ = _app(servicio, tmp_path, [Paso(llamadas=[("registrar_movimiento", MOVIMIENTO)]), Paso(texto="Ok")])
    eventos = _chat(cliente)
    conv, token = eventos[0][1]["id"], next(d for t, d in eventos if t == "accion_pendiente")["token"]
    r = cliente.post("/api/v1/acciones/cancelar", json={"conversacion_id": conv, "token": token}, headers=CABECERA)
    assert r.json()["estado"] == "cancelada"
    assert CsvExcelConnector(datos_dir).obtener_producto("P0002").stock_actual == 5


def test_el_modelo_no_puede_confirmar_por_la_api(servicio, tmp_path, datos_dir):
    token = servicio.preparar_movimiento({"producto_id": "P0002", "tipo": "salida", "cantidad": 1,
                                          "motivo": "x"})["token"]
    cliente, falso = _app(servicio, tmp_path, [Paso(llamadas=[("confirmar_operacion", {"token": token})]),
                                               Paso(texto="No puedo.")])
    eventos = _chat(cliente)
    assert "confirmar_operacion" not in falso.llamadas[0]["herramientas"]
    assert [d["ok"] for t, d in eventos if t == "tool_fin"] == [False]
    assert CsvExcelConnector(datos_dir).obtener_producto("P0002").stock_actual == 5


def test_conversaciones_por_usuario(servicio, tmp_path):
    cliente, _ = _app(servicio, tmp_path, lambda m, h: Paso(texto="ok"))
    conv = _chat(cliente, "primera")[0][1]["id"]
    _chat(cliente, "segunda", conv)
    _chat(cliente, "de maría", cabecera=_jwt())
    lista = cliente.get("/api/v1/conversaciones", headers=CABECERA).json()["conversaciones"]
    assert [c["titulo"] for c in lista] == ["primera"]
    ajena = cliente.get(f"/api/v1/conversaciones/{conv}", headers=_jwt())
    assert ajena.status_code == 404
    seguir_ajena = cliente.post("/api/v1/chat", json={"mensaje": "x", "conversacion_id": conv}, headers=_jwt())
    assert seguir_ajena.status_code == 404
    textos = [m["texto"] for m in cliente.get(f"/api/v1/conversaciones/{conv}", headers=CABECERA).json()["mensajes"]]
    assert textos == ["primera", "ok", "segunda", "ok"]
    assert cliente.delete(f"/api/v1/conversaciones/{conv}", headers=CABECERA).json() == {"ok": True}
    assert cliente.get(f"/api/v1/conversaciones/{conv}", headers=CABECERA).status_code == 404


# --------------------------------------------------- reportes, bandeja, salud

def test_reportes_por_mcp(servicio, tmp_path):
    cliente, _ = _app(servicio, tmp_path)
    r = cliente.get("/api/v1/reportes/abc", params={"ultimos_dias": "30"}, headers=CABECERA)
    assert r.status_code == 200 and r.json()["tipo"] == "abc" and r.json()["periodo"]["dias"] == 30
    bajos = cliente.get("/api/v1/reportes/bajo_stock", headers=CABECERA).json()["productos"]
    assert {p["producto_id"] for p in bajos} == {"P0002", "P0003"}
    assert cliente.get("/api/v1/reportes/inventado", headers=CABECERA).status_code == 404
    assert cliente.get("/api/v1/reportes/abc", params={"color": "x"}, headers=CABECERA).json()["error"] == \
        "ParametroDesconocido"
    assert cliente.get("/api/v1/reportes/abc", params={"ultimos_dias": "muchos"}, headers=CABECERA).status_code == 422
    doble = cliente.get("/api/v1/reportes/abc", params={"ultimos_dias": "7", "desde": "2026-10-01"}, headers=CABECERA)
    assert doble.status_code == 422 and doble.json()["invalidos"][0]["campo"] == "ultimos_dias"


def test_bandeja_y_botones_del_panel(datos_dir, reloj, auditoria, tmp_path):
    servicio = InventarioService(
        ConectorBandejaFalso(datos_dir), GestorConfirmaciones(MemoriaConfirmacionStore(), reloj=reloj), auditoria,
        bandeja=MemoriaBandejaStore(), exports_dir=tmp_path / "exports", hoy=lambda: HOY, reloj=reloj)
    for nombre in ("Martillo", "Serrucho"):
        servicio.confirmar(servicio.preparar_crear_producto(PRODUCTO | {"nombre": nombre})["token"])
    cliente, _ = _app(servicio, tmp_path)
    assert cliente.get("/api/v1/bandeja", headers=CABECERA).json()["total"] == 2
    hecho = cliente.post("/api/v1/bandeja/SM-000001/registrar", json={"observacion": "Importado"}, headers=CABECERA)
    assert hecho.status_code == 200 and hecho.json()["operacion"]["estado"] == "registrada"
    otra_vez = cliente.post("/api/v1/bandeja/SM-000001/registrar", json={}, headers=CABECERA)
    assert otra_vez.status_code == 409 and otra_vez.json()["error"] == "TransicionInvalida"
    sin_motivo = cliente.post("/api/v1/bandeja/SM-000002/descartar", json={}, headers=CABECERA)
    assert sin_motivo.status_code == 422 and sin_motivo.json()["faltantes"][0]["campo"] == "motivo"
    assert cliente.post("/api/v1/bandeja/SM-000002/descartar", json={"motivo": "Duplicado"},
                        headers=CABECERA).json()["operacion"]["estado"] == "descartada"
    assert cliente.get("/api/v1/bandeja", params={"estado": "todas"}, headers=CABECERA).json()["total"] == 2


def test_salud(servicio, tmp_path):
    cliente, _ = _app(servicio, tmp_path)
    r = cliente.get("/api/v1/salud", headers=CABECERA)
    assert r.status_code == 200 and r.json()["ok"]
    datos = r.json()
    assert datos["mcp"]["herramientas"] >= 20 and datos["conector"] == {"ok": True, "nombre": "csv",
                                                                         "modo_escritura": "directa"}
    assert datos["proveedor"] == {"nombre": "falso", "modelo": "falso-1", "configurado": True}


def test_salud_y_chat_con_el_mcp_caido(servicio, tmp_path):
    @asynccontextmanager
    async def caido():
        raise ErrorMCP("No se pudo conectar con el servidor MCP en http://127.0.0.1:1/mcp")
        yield

    ajustes = ApiSettings(_env_file=None, api_token=TOKEN, llm_provider="falso")
    cliente = TestClient(crear_app(ajustes, proveedor=ProveedorFalso(), historial=HistorialStore(tmp_path / "c.db"),
                                   conectar_mcp=caido))
    salud = cliente.get("/api/v1/salud", headers=CABECERA)
    assert salud.status_code == 503 and salud.json()["mcp"]["ok"] is False
    eventos = _chat(cliente)
    assert eventos[0] == ("error", {"error": "ErrorMCP", "mensaje": eventos[0][1]["mensaje"]})
    assert cliente.get("/api/v1/bandeja", headers=CABECERA).status_code == 502


def test_secreto_jwt_corto_no_se_acepta(servicio, tmp_path):
    with pytest.raises(ValueError, match="al menos 32"):
        crear_app(ApiSettings(_env_file=None, api_token=TOKEN, llm_provider="falso", erp_jwt_secreto="corto"),
                  proveedor=ProveedorFalso(), historial=HistorialStore(tmp_path / "c.db"))
