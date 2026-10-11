import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own DuckDB files; the real data/ is never touched."""
    path = tmp_path / "test.duckdb"
    monkeypatch.setenv("GARMIN_DB_PATH", str(path))
    monkeypatch.setenv("GARMIN_AUDIT_PATH", str(tmp_path / "audit.duckdb"))
    return path
