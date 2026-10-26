"""Utilidades de texto del dominio."""

import unicodedata


def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes y sin espacios de sobra, para comparar nombres."""
    sin_tildes = unicodedata.normalize("NFKD", texto)
    return " ".join("".join(c for c in sin_tildes if not unicodedata.combining(c)).casefold().split())
