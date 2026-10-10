"""Keep the tests offline: never read the developer's real .env.

cli.main() loads .env with override=True, so without this fixture a real
AZURE_SQL_ODBC / AZURE_SQL_AUTH in the project folder would replace the test
database and the tests would try to sign in to and query the real server.
"""
import pytest


@pytest.fixture(autouse=True)
def _ignore_dotenv(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    for var in ("AZURE_SQL_ODBC", "AZURE_SQL_AUTH", "DB_URL", "DBDOC_PROVIDER", "DBDOC_LANGUAGE"):
        monkeypatch.delenv(var, raising=False)
