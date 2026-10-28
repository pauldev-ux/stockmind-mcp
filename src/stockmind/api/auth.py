"""Autenticación de la API: `Authorization: Bearer <token>`.

- TokenEstatico: compara con API_TOKEN (tiempo constante). Para el panel y pruebas.
- JWTFirmado: valida un JWT HS256 firmado por el ERP (sub = usuario, empresa opcional),
  con emisor y audiencia si se configuran. Pensado para integrar StockMind en un módulo del ERP.
Las conversaciones quedan asociadas a `Identidad.usuario`.
"""

import hmac
from abc import ABC, abstractmethod
from typing import Literal

import jwt
from pydantic import BaseModel


class Identidad(BaseModel):
    usuario: str
    empresa: str | None = None
    origen: Literal["api_token", "erp_jwt"]


class VerificadorIdentidad(ABC):
    @abstractmethod
    def verificar(self, token: str) -> Identidad | None:
        """La identidad si el token es válido; None si no lo es."""


class TokenEstatico(VerificadorIdentidad):
    def __init__(self, token: str, usuario: str = "api"):
        if not token:
            raise ValueError("API_TOKEN está vacío: define uno largo y secreto en .env.")
        self._token = token.encode()
        self._usuario = usuario

    def verificar(self, token: str) -> Identidad | None:
        if hmac.compare_digest(token.encode(), self._token):
            return Identidad(usuario=self._usuario, origen="api_token")
        return None


class JWTFirmado(VerificadorIdentidad):
    LARGO_MINIMO = 32   # bytes; RFC 7518 §3.2 para HS256

    def __init__(self, secreto: str, emisor: str | None = None, audiencia: str | None = None):
        if len(secreto.encode()) < self.LARGO_MINIMO:
            raise ValueError(f"ERP_JWT_SECRETO debe tener al menos {self.LARGO_MINIMO} caracteres.")
        self._secreto, self._emisor, self._audiencia = secreto, emisor, audiencia

    def verificar(self, token: str) -> Identidad | None:
        if token.count(".") != 2:
            return None
        try:
            claims = jwt.decode(token, self._secreto, algorithms=["HS256"], audience=self._audiencia,
                                issuer=self._emisor, options={"require": ["exp", "sub"]})
        except jwt.PyJWTError:
            return None
        empresa = claims.get("empresa")
        return Identidad(usuario=f"erp:{claims['sub']}", empresa=str(empresa) if empresa is not None else None,
                         origen="erp_jwt")


class VerificadorCompuesto(VerificadorIdentidad):
    def __init__(self, *verificadores: VerificadorIdentidad):
        self._verificadores = verificadores

    def verificar(self, token: str) -> Identidad | None:
        for v in self._verificadores:
            if (identidad := v.verificar(token)) is not None:
                return identidad
        return None
