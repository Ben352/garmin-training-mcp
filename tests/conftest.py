import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own DuckDB file; the real data/garmin.duckdb is never touched."""
    path = tmp_path / "test.duckdb"
    monkeypatch.setenv("GARMIN_DB_PATH", str(path))
    return path
