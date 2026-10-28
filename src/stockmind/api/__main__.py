"""`stockmind-api`: levanta la API del chat. Necesita el servidor MCP en HTTP (`stockmind-mcp --http`)."""

import logging
import sys


def main() -> None:
    import uvicorn

    from stockmind.api.app import crear_app
    from stockmind.api.config import ApiSettings

    config = ApiSettings()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    faltan = [n for n, v in (("API_TOKEN", config.api_token.get_secret_value() or
                              config.erp_jwt_secreto.get_secret_value()),
                             ("MCP_TOKEN", config.mcp_token.get_secret_value())) if not v]
    if faltan:
        sys.exit(f"Faltan en .env: {', '.join(faltan)} (textos largos y secretos; ver .env.example).")
    try:
        app = crear_app(config)
    except ValueError as e:
        sys.exit(str(e))
    logging.getLogger("stockmind.api").info(
        "API en http://%s:%s/docs · proveedor %s (%s) · MCP %s", config.api_host, config.api_port,
        app.state.proveedor.nombre, app.state.proveedor.modelo, config.mcp_url)
    uvicorn.run(app, host=config.api_host, port=config.api_port, log_level="info")


if __name__ == "__main__":
    main()
