from stockmind.config.settings import PROJECT_ROOT, Settings


def test_variables_vacias_cuentan_como_no_definidas(monkeypatch):
    # "X=" vacío en .env no debe impedir arrancar: se usa el valor por defecto.
    monkeypatch.setenv("TOKEN_TTL_MINUTOS", "")
    monkeypatch.setenv("CONNECTOR", "")
    s = Settings(_env_file=None)
    assert s.token_ttl_minutos == 10 and s.connector == "csv"


def test_variables_de_otros_conectores_se_ignoran(monkeypatch):
    # Un conector privado puede leer sus propias variables del mismo .env.
    monkeypatch.setenv("ERP_PRIVADO_DB_USER", "root")
    assert not hasattr(Settings(_env_file=None), "erp_privado_db_user")


def test_rutas_relativas_se_resuelven_desde_la_raiz(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "otra")
    assert Settings(_env_file=None).data_dir == (PROJECT_ROOT / "otra").resolve()
