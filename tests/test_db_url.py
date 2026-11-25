from backend import db


def test_render_style_postgres_urls_use_psycopg(monkeypatch):
    for raw in ("postgres://u:p@h:5432/d", "postgresql://u:p@h:5432/d"):
        monkeypatch.setenv("DATABASE_URL", raw)
        assert db._database_url() == "postgresql+psycopg://u:p@h:5432/d"


def test_explicit_driver_url_is_kept(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/d")
    assert db._database_url() == "postgresql+psycopg://u:p@h/d"
