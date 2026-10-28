"""Fábrica de proveedores de IA. Los SDK se importan solo si se usa ese proveedor."""

from stockmind.agente.llm import ConReintentos, LLMProvider

PROVEEDORES = ("anthropic", "gemini", "falso")
SIN_RESPALDO = "ninguno"


def lista_respaldo(valor: str | None, por_defecto: tuple[str, ...], principal: str) -> list[str]:
    """LLM_MODELOS_RESPALDO: vacío = la lista del proveedor; "ninguno" = sin respaldo; si no, separados por coma."""
    if valor is None:
        modelos = list(por_defecto)
    elif valor.strip().lower() == SIN_RESPALDO:
        modelos = []
    else:
        modelos = [m.strip() for m in valor.split(",") if m.strip()]
    vistos = {principal}
    return [m for m in modelos if not (m in vistos or vistos.add(m))]


def crear_proveedor(nombre: str, *, modelo: str | None = None, api_key: str | None = None,
                    thinking: str = "bajo", fallbacks: bool = True, max_tokens: int = 16000,
                    modelos_respaldo: str | None = None, reintentos: int = 3, espera_inicial: float = 2.0,
                    enfriamiento: float = 300.0, timeout: float = 30.0) -> LLMProvider:
    """Anthropic y Gemini van envueltos en `ConReintentos` (reintentos, respaldo y circuit breaker).
    Todos los modelos comparten un solo cliente HTTP, que se reutiliza entre mensajes."""
    nombre = (nombre or "").strip().lower()
    try:
        if nombre == "anthropic":
            from stockmind.agente.proveedores.claude import RESPALDO_POR_DEFECTO, ProveedorAnthropic

            principal = ProveedorAnthropic(api_key, modelo, thinking=thinking, fallbacks=fallbacks,
                                           max_tokens=max_tokens, timeout=timeout)
            otros = [ProveedorAnthropic(modelo=m, thinking=thinking, fallbacks=fallbacks, max_tokens=max_tokens,
                                        timeout=timeout, cliente=principal._cliente)
                     for m in lista_respaldo(modelos_respaldo, RESPALDO_POR_DEFECTO, principal.modelo)]
        elif nombre == "gemini":
            from stockmind.agente.proveedores.gemini import RESPALDO_POR_DEFECTO, ProveedorGemini

            principal = ProveedorGemini(api_key, modelo, thinking=thinking, max_tokens=max_tokens, timeout=timeout)
            otros = [ProveedorGemini(api_key, m, thinking=thinking, max_tokens=max_tokens, timeout=timeout,
                                     cliente=principal._cliente)
                     for m in lista_respaldo(modelos_respaldo, RESPALDO_POR_DEFECTO, principal.modelo)]
        else:
            principal = None
    except ModuleNotFoundError as e:
        raise ValueError(f"Falta el SDK del proveedor «{nombre}» ({e.name}): pip install -e \".[api]\"") from None
    if principal is not None:
        return ConReintentos([principal, *otros], reintentos=reintentos, espera_inicial=espera_inicial,
                             enfriamiento=enfriamiento)
    if nombre == "falso":
        from stockmind.agente.proveedores.falso import ProveedorFalso

        return ProveedorFalso()
    raise ValueError(f"LLM_PROVIDER desconocido: «{nombre}». Opciones: {', '.join(PROVEEDORES)}.")
