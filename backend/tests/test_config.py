from pathlib import Path

import pytest

from app.core.config import Settings


def test_database_url_preserves_special_password_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "local@pass:/?#%word"
    monkeypatch.setenv("POSTGRES_PASSWORD", password)

    settings = Settings(_env_file=None)

    assert settings.database_url.password == password
    assert settings.database_url.drivername == "postgresql+psycopg"
    assert password not in repr(settings)


def test_environment_overrides_dotenv_and_ignores_unrelated_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "POSTGRES_DB=from_file\nVITE_API_BASE_URL=/api/v1\n"
        'API_CORS_ORIGINS=["http://localhost:5173"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("POSTGRES_DB", "from_environment")
    monkeypatch.delenv("API_CORS_ORIGINS", raising=False)

    settings = Settings(_env_file=dotenv)

    assert settings.postgres_db == "from_environment"
    assert settings.api_cors_origins == ["http://localhost:5173"]
