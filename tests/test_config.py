import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

import config
from tests.seed_test_db import create_sample_database


@pytest.fixture
def default_url(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    # A space, as in this repository's own path.
    monkeypatch.setattr(config, "SAMPLE_DATABASE", tmp_path / "a dir" / "sample.db")
    return config._resolve_database_url


def test_the_default_database_does_not_depend_on_the_working_directory():
    assert config.SAMPLE_DATABASE.is_absolute()
    assert config.SAMPLE_DATABASE.parent == config.Path(config.__file__).resolve().parent


def test_a_missing_default_database_is_an_error_not_a_new_empty_file(default_url):
    with pytest.raises(OperationalError, match="unable to open database file"):
        create_engine(default_url()).connect()

    assert not config.SAMPLE_DATABASE.exists()


def test_the_default_database_is_read_only_at_the_file_level(default_url):
    config.SAMPLE_DATABASE.parent.mkdir()
    create_sample_database(config.SAMPLE_DATABASE)

    with create_engine(default_url()).connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM users")).scalar() > 0
        with pytest.raises(OperationalError, match="readonly database"):
            connection.execute(text("DELETE FROM users"))


def test_a_token_group_reads_its_own_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL_ANALYST", "postgresql://dbx_analyst@db/app")

    assert config.group_database_url("analyst") == "postgresql://dbx_analyst@db/app"


def test_a_token_group_without_a_database_url_stops_startup(monkeypatch):
    monkeypatch.delenv("DATABASE_URL_FINANCE", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL_FINANCE must be set"):
        config.group_database_url("finance")
