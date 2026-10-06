"""Offline tests: SQLite instead of Azure SQL, scripted LLM instead of a real model."""
import sqlite3

import pytest

from dbdoc import cli, sqlguard
from dbdoc import llm as llm_module
from dbdoc.agent import DocAgent
from dbdoc.db import Database, make_engine


@pytest.fixture
def db_url(tmp_path):
    path = tmp_path / "shop.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE Customers (CustomerId INTEGER PRIMARY KEY, CustomerName TEXT NOT NULL,
                                Email TEXT, CreatedAt TEXT);
        CREATE TABLE Orders (OrderId INTEGER PRIMARY KEY, CustomerId INTEGER,   -- no FK declared
                             OrderDate TEXT, Status INTEGER, TotalNet REAL);
        CREATE TABLE OrderItems (OrderItemId INTEGER PRIMARY KEY,
                                 OrderId INTEGER REFERENCES Orders(OrderId),
                                 Sku TEXT, Qty INTEGER, UnitPrice REAL);
    """)
    con.executemany("INSERT INTO Customers VALUES (?,?,?,?)",
                    [(i, f"Customer {i}", f"c{i}@example.com", "2025-01-01") for i in range(1, 21)])
    con.executemany("INSERT INTO Orders VALUES (?,?,?,?,?)",
                    [(i, (i % 20) + 1, f"2026-0{1 + i % 9}-15", i % 4, 10.0 * i) for i in range(1, 101)])
    con.executemany("INSERT INTO OrderItems VALUES (?,?,?,?,?)",
                    [(i, (i % 100) + 1, f"SKU-{i % 7}", 1 + i % 3, 9.99) for i in range(1, 301)])
    con.commit()
    con.close()
    return f"sqlite:///{path}"


class ScriptedLLM(llm_module.BaseLLM):
    """Runs a verification query, then documents every column it was given."""
    label = "scripted:test"

    def __init__(self):
        self.tool_outputs = []

    def run_tools(self, system, user, tools, handle, final_tool, max_steps):
        table = user.split("\n")[0].split()[-1].split(".")[-1]
        for sql in (f"SELECT COUNT(*) AS n FROM {table}", f"DELETE FROM {table}"):
            out, done = handle("run_sql", {"sql": sql, "purpose": "test"})
            self.tool_outputs.append(out)
            assert not done
        cols = [line[2:].split()[0] for line in user.split("\n") if line.startswith("- ")]
        args = {
            "summary": f"Test summary of {table}.", "grain": "One row per thing.",
            "columns": [{"name": c, "description": f"Column {c}.", "role": "other",
                         "confidence": "verified",
                         **({"value_meanings": [{"value": "0", "meaning": "New"}]} if c == "Status" else {})}
                        for c in cols],
            "relationships": ([{"column": "CustomerId", "ref_table": "main.Customers",
                                "ref_column": "CustomerId", "confidence": "verified",
                                "evidence": "0 orphans"}] if table == "Orders" else []),
            "notes": ["A note."],
            "example_queries": [{"question": "How many?", "sql": f"SELECT COUNT(*) FROM {table}"}],
        }
        out, done = handle(final_tool, args)
        assert done, out
        return args

    def complete(self, system, user):
        return "An overview of the shop database."


# ---------------------------------------------------------------- sqlguard
@pytest.mark.parametrize("sql", [
    "SELECT TOP (10) * FROM dbo.Orders",
    "WITH x AS (SELECT Status, COUNT(*) n FROM Orders GROUP BY Status) SELECT * FROM x",
    "SELECT 'drop table x' AS s",
    "SELECT a FROM t UNION ALL SELECT b FROM u",
])
def test_guard_allows_reads(sql):
    sqlguard.validate(sql, "tsql")


@pytest.mark.parametrize("sql", [
    "DELETE FROM Orders",
    "UPDATE Orders SET Status = 1",
    "SELECT * INTO backup_orders FROM Orders",
    "SELECT 1; DROP TABLE Orders",
    "EXEC sp_who",
    "SELECT * FROM OPENROWSET(BULK 'x', SINGLE_CLOB) AS t",
    "SELECT 1 WAITFOR DELAY '00:00:10'",
    "TRUNCATE TABLE Orders",
    "",
])
def test_guard_blocks_writes(sql):
    with pytest.raises(sqlguard.UnsafeQuery):
        sqlguard.validate(sql, "tsql")


# ---------------------------------------------------------------- profiling
def test_profile(db_url):
    db = Database(make_engine(url=db_url))
    p = db.profile("main", "Orders", "table")
    cols = {c.name: c for c in p.columns}
    assert p.row_count == 100 and p.primary_key == ["OrderId"]
    assert dict(cols["Status"].top_values) == {"0": 25, "1": 25, "2": 25, "3": 25}
    item = db.profile("main", "OrderItems", "table")
    assert {c.name: c.fk for c in item.columns}["OrderId"] == "main.Orders.OrderId"

    hidden = Database(make_engine(url=db_url), with_values=False).profile("main", "Customers", "table")
    assert all(not c.sample_values and not c.top_values for c in hidden.columns)


# ---------------------------------------------------------------- end to end
def test_end_to_end(db_url, tmp_path, monkeypatch):
    scripted = ScriptedLLM()
    monkeypatch.setenv("DB_URL", db_url)
    monkeypatch.setattr(cli, "get_llm", lambda provider, model: scripted)
    out = tmp_path / "docs"

    assert cli.main(["--out", str(out), "--schema", "main"]) == 0
    orders = (out / "tables" / "main.Orders.md").read_text()
    assert "Test summary of Orders." in orders
    assert "### `Status`" in orders and "| `0` | New |" in orders
    assert "[main.Customers](main.Customers.md)" in orders
    assert any(o.startswith("REJECTED") for o in scripted.tool_outputs)  # DELETE was blocked
    assert "n\n20" in scripted.tool_outputs[0]  # Customers is first: real query result came back
    assert "An overview of the shop database." in (out / "README.md").read_text()
    rel = (out / "relationships.md").read_text()
    assert "main_Customers ||--o{ main_Orders" in rel and "main_Orders ||--o{ main_OrderItems" in rel

    # Manual notes survive regeneration; unchanged tables are skipped.
    path = out / "tables" / "main.Orders.md"
    path.write_text(path.read_text().replace("_Add business context here.", "Status 3 = cancelled."))
    calls = len(scripted.tool_outputs)
    assert cli.main(["--out", str(out), "--schema", "main"]) == 0
    assert len(scripted.tool_outputs) == calls  # nothing re-documented
    assert cli.main(["--out", str(out), "--schema", "main", "--tables", "Orders", "--force"]) == 0
    assert "Status 3 = cancelled." in path.read_text()
    index = (out / "README.md").read_text()
    assert "3 objects" in index and "[main.Customers]" in index  # filtered run keeps full index


def test_profile_only(db_url, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_URL", db_url)
    out = tmp_path / "docs"
    assert cli.main(["--out", str(out), "--schema", "main", "--provider", "none"]) == 0
    text = (out / "tables" / "main.OrderItems.md").read_text()
    assert "documented: profile-only" in text and "declared foreign key" in text


# ---------------------------------------------------------------- Entra ID / MFA token
def test_token_struct():
    from dbdoc.db import token_struct
    raw = token_struct("abc")
    assert raw[:4] == (6).to_bytes(4, "little") and raw[4:] == "abc".encode("utf-16-le")


def test_entra_token_injected(monkeypatch):
    """With AZURE_SQL_AUTH the token goes to the driver as attribute 1256 (no pyodbc needed)."""
    import types
    import sqlalchemy
    from dbdoc import db as dbmod

    fake_pyodbc = types.SimpleNamespace(paramstyle="qmark", version="5.2.0", Error=Exception,
                                        Cursor=type("Cursor", (), {"nextset": None}))
    real_create = sqlalchemy.create_engine
    monkeypatch.setattr(dbmod, "create_engine", lambda url, **kw: real_create(url, module=fake_pyodbc, **kw))
    fake_cred = types.SimpleNamespace(get_token=lambda scope: types.SimpleNamespace(token="tok"))
    monkeypatch.setattr(dbmod, "entra_credential", lambda mode: fake_cred)

    odbc = "Driver={ODBC Driver 18 for SQL Server};Server=tcp:x.database.windows.net;Database=d"
    engine = dbmod.make_engine(odbc=odbc, auth="browser")
    cparams = {}
    for listener in engine.dialect.dispatch.do_connect:
        listener(engine.dialect, None, [], cparams)
    assert cparams["attrs_before"][1256] == dbmod.token_struct("tok")

    with pytest.raises(ValueError, match="remove Authentication"):
        dbmod.make_engine(odbc=odbc + ";Authentication=ActiveDirectoryInteractive", auth="browser")


# ---------------------------------------------------------------- progress messages
def test_sign_in_messages(capsys):
    import base64, json, types
    from dbdoc import db as dbmod
    payload = base64.urlsafe_b64encode(json.dumps({"upn": "konrad@example.com"}).encode()).decode().rstrip("=")
    token = types.SimpleNamespace(token=f"h.{payload}.s", expires_on=2_000_000_000)
    dbmod.sign_in(types.SimpleNamespace(get_token=lambda scope: token), "browser")
    out = capsys.readouterr().out
    assert "Signing in to Microsoft Entra ID (browser mode" in out
    assert "Signed in as konrad@example.com, token valid until" in out

    def boom(scope):
        raise RuntimeError("AADSTS50020: User account does not exist in tenant")
    with pytest.raises(SystemExit):
        dbmod.sign_in(types.SimpleNamespace(get_token=boom), "browser")
    err = capsys.readouterr()
    assert "Sign-in failed: RuntimeError: AADSTS50020" in err.err and "AZURE_TENANT_ID" in err.out


@pytest.mark.parametrize("message, expected", [
    ("[08001] Named Pipes Provider: Could not open a connection ... Login timeout expired", "cannot be reached"),
    ("Cannot open server 'x' requested by the login. Client with IP address '1.2.3.4' is not "
     "allowed to access the server. (40615)", "firewall"),
    ("Login failed for user '<token-identified principal>'. (18456)", "CREATE USER"),
    ("[IM002] Data source name not found and no default driver specified", "pyodbc.drivers()"),
])
def test_connection_error_hints(message, expected):
    from dbdoc.db import explain_connection_error
    assert expected in explain_connection_error(Exception(message))


def test_progress_output(db_url, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DB_URL", db_url)
    monkeypatch.setattr(cli, "get_llm", lambda provider, model: ScriptedLLM())
    assert cli.main(["--out", str(tmp_path / "d"), "--schema", "main"]) == 0
    out = capsys.readouterr().out
    for text in ("Connecting to", "Connected", "LLM ready", "Will analyze 3 objects",
                 "[2/3] Analyzing table main.OrderItems", "Sampling up to 5,000 rows",
                 "Agent (scripted:test) is investigating", "Query 1: test",
                 "Query blocked by the read-only guard", "Agent submitted documentation",
                 "Writing index", "Finished in"):
        assert text in out, text
