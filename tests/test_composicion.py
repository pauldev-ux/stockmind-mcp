"""Elección del conector: incluidos y opcionales (privados, fuera del repositorio)."""

import sys
import types

import pytest

from stockmind.adapters.csv_excel import CsvExcelConnector
from stockmind.composicion import crear_conector
from stockmind.config.settings import Settings


def _settings(conector: str, tmp_path) -> Settings:
    return Settings(_env_file=None, connector=conector, data_dir=tmp_path)


def test_conector_csv_incluido(tmp_path):
    assert isinstance(crear_conector(_settings("csv", tmp_path)), CsvExcelConnector)


def test_conector_opcional_ausente_da_error_claro(tmp_path):
    with pytest.raises(ValueError, match="no está disponible en esta instalación") as e:
        crear_conector(_settings("erp_que_no_existe", tmp_path))
    assert "privados" in str(e.value) and "csv" in str(e.value)


def test_nombre_de_conector_invalido(tmp_path):
    with pytest.raises(ValueError, match="inválido"):
        crear_conector(_settings("../otro", tmp_path))


def test_conector_opcional_presente_se_carga(tmp_path, monkeypatch):
    creado = object()
    modulo = types.ModuleType("stockmind.adapters.erp_de_prueba")
    modulo.crear_conector = lambda settings: creado
    monkeypatch.setitem(sys.modules, "stockmind.adapters.erp_de_prueba", modulo)
    assert crear_conector(_settings("erp_de_prueba", tmp_path)) is creado


def test_conector_opcional_sin_fabrica(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "stockmind.adapters.erp_sin_fabrica",
                        types.ModuleType("stockmind.adapters.erp_sin_fabrica"))
    with pytest.raises(ValueError, match="crear_conector"):
        crear_conector(_settings("erp_sin_fabrica", tmp_path))


def test_dependencia_faltante_da_error_claro(tmp_path, monkeypatch):
    import importlib

    def falla(nombre, *args, **kwargs):
        raise ModuleNotFoundError("No module named 'sqlalchemy'", name="sqlalchemy")

    monkeypatch.setattr(importlib, "import_module", falla)
    with pytest.raises(ValueError, match=r"dependencia 'sqlalchemy'.*\[mysql\]"):
        crear_conector(_settings("erp_con_mysql", tmp_path))
