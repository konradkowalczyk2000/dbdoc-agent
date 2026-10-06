"""Database access: connection, introspection, profiling and read-only queries."""
from __future__ import annotations

import base64
import datetime as dt
import decimal
import fnmatch
import hashlib
import json
import os
import struct
import warnings
from collections import Counter
from dataclasses import asdict, dataclass, field
from urllib.parse import quote_plus

from sqlalchemy import MetaData, Table, create_engine, event, func, inspect, select
from sqlalchemy.engine import Engine

from . import log, sqlguard

SYSTEM_SCHEMAS = {"sys", "INFORMATION_SCHEMA", "guest", "information_schema"}


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #
SQL_COPT_SS_ACCESS_TOKEN = 1256  # msodbcsql pre-connect attribute for an Entra access token
AZURE_SQL_SCOPE = "https://database.windows.net/.default"
ENTRA_AUTH_MODES = ("browser", "cli", "default", "device")


def token_struct(token: str) -> bytes:
    """Encode an access token the way the ODBC driver expects (length + UTF-16-LE)."""
    raw = token.encode("utf-16-le")
    return struct.pack(f"<I{len(raw)}s", len(raw), raw)


def entra_credential(mode: str):
    """Credential for Microsoft Entra ID, MFA included. Tokens are cached in memory,
    so you sign in once per run."""
    from azure.identity import (AzureCliCredential, DefaultAzureCredential,  # pip install azure-identity
                                DeviceCodeCredential, InteractiveBrowserCredential)

    tenant = os.getenv("AZURE_TENANT_ID") or None
    if mode == "browser":   # browser window with MFA
        return InteractiveBrowserCredential(tenant_id=tenant)
    if mode == "device":    # code to enter on another device (servers, SSH, no browser)
        return DeviceCodeCredential(tenant_id=tenant)
    if mode == "cli":       # reuse `az login` (MFA happens there)
        return AzureCliCredential(tenant_id=tenant)
    if mode == "default":   # env vars, managed identity, VS Code, az CLI ... in that order
        return DefaultAzureCredential()
    raise ValueError(f"AZURE_SQL_AUTH must be one of {ENTRA_AUTH_MODES}, got {mode!r}")


SIGN_IN_HINTS = {
    "browser": "a browser window will open: pick your account and complete MFA",
    "device": "a code will be shown below: enter it at the given address on any device",
    "cli": "using the account from `az login`",
    "default": "trying environment variables, managed identity, VS Code and `az login`",
}


def _token_account(token: str) -> str | None:
    """Read the account name from the access token (JWT payload, no verification needed)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims.get("upn") or claims.get("unique_name") or claims.get("email") or claims.get("appid")
    except Exception:
        return None


def sign_in(credential, mode: str) -> None:
    tenant = os.getenv("AZURE_TENANT_ID")
    log.step(f"Signing in to Microsoft Entra ID ({mode} mode"
             f"{', tenant ' + tenant if tenant else ''}): {SIGN_IN_HINTS.get(mode, '')}")
    try:
        token = credential.get_token(AZURE_SQL_SCOPE)
    except Exception as e:
        log.error(f"Sign-in failed: {type(e).__name__}: {str(e).splitlines()[0][:300]}")
        log.info("Hints: set AZURE_TENANT_ID to the tenant of the SQL server; make sure the "
                 "account exists in that tenant (guest accounts must be invited first).")
        raise SystemExit(3) from e
    account = _token_account(token.token)
    expires = getattr(token, "expires_on", None)
    until = f", token valid until {dt.datetime.fromtimestamp(expires):%H:%M}" if expires else ""
    log.ok(f"Signed in{' as ' + account if account else ''}{until}")


def explain_connection_error(e: Exception) -> str:
    """Turn common driver errors into a short, actionable hint."""
    msg = str(e)
    if "40615" in msg or "is not allowed to access the server" in msg:
        return ("The Azure SQL firewall blocks your IP. Add it in the Azure portal: "
                "SQL server → Networking → Firewall rules → Add your client IPv4 address.")
    if "18456" in msg or "Login failed" in msg:
        return ("The server rejected the login. Make sure the user exists in this database "
                "(CREATE USER [you@company.com] FROM EXTERNAL PROVIDER) and has db_datareader.")
    if "4060" in msg or "Cannot open database" in msg:
        return "The database name is wrong or your user has no access to it. Check Database= in AZURE_SQL_ODBC."
    if "Data source name not found" in msg or "IM002" in msg:
        return ("The ODBC driver named in Driver={...} is not installed. Check with: "
                "python -c \"import pyodbc; print(pyodbc.drivers())\"")
    if "08001" in msg or "Login timeout expired" in msg or "network-related" in msg:
        return ("The server cannot be reached. Check Server=tcp:<name>.database.windows.net,1433 "
                "in AZURE_SQL_ODBC, and test the port: Test-NetConnection <name>.database.windows.net -Port 1433")
    return "See the error above."


def make_engine(url: str | None = None, odbc: str | None = None, query_timeout: int = 60,
                auth: str | None = None) -> Engine:
    """Create an engine from a SQLAlchemy URL or a raw ODBC connection string.

    auth: None = whatever the connection string says (SQL login, ActiveDirectoryInteractive …);
          browser | device | cli | default = Entra ID token from azure-identity (works with MFA).
    """
    if odbc:
        url = "mssql+pyodbc:///?odbc_connect=" + quote_plus(odbc)
    if not url:
        raise ValueError("Set AZURE_SQL_ODBC or DB_URL (see .env.example).")
    engine = create_engine(url, pool_pre_ping=True)

    if auth and engine.dialect.name == "mssql":
        lowered = (odbc or url).lower()
        if any(k in lowered for k in ("authentication=", "uid=", "pwd=", "trusted_connection=")):
            raise ValueError("With AZURE_SQL_AUTH set, remove Authentication/Uid/Pwd from "
                             "AZURE_SQL_ODBC: the login comes from the Entra token.")
        credential = entra_credential(auth)
        sign_in(credential, auth)  # eager, so the user sees the sign-in before anything else

        @event.listens_for(engine, "do_connect")
        def _inject_token(_dialect, _conn_rec, _cargs, cparams):
            token = credential.get_token(AZURE_SQL_SCOPE).token  # cached, refreshed when expiring
            cparams.setdefault("attrs_before", {})[SQL_COPT_SS_ACCESS_TOKEN] = token_struct(token)

    if engine.dialect.name == "mssql":
        @event.listens_for(engine, "connect")
        def _set_query_timeout(dbapi_conn, _record):  # pyodbc: per-query timeout in seconds
            try:
                dbapi_conn.timeout = query_timeout
            except Exception:
                pass

    return engine


# --------------------------------------------------------------------------- #
# Profile model
# --------------------------------------------------------------------------- #
@dataclass
class ColumnProfile:
    name: str
    type: str
    nullable: bool
    is_pk: bool = False
    fk: str | None = None                 # "schema.table.column" if a declared FK
    db_description: str | None = None     # MS_Description / column comment
    null_pct: float | None = None
    distinct_in_sample: int | None = None
    top_values: list[tuple[str, int]] = field(default_factory=list)
    min: str | None = None
    max: str | None = None
    avg_len: float | None = None
    sample_values: list[str] = field(default_factory=list)


@dataclass
class TableProfile:
    schema: str
    name: str
    kind: str                              # "table" | "view"
    row_count: int | None
    db_description: str | None
    columns: list[ColumnProfile]
    primary_key: list[str]
    foreign_keys: list[dict]               # {columns, ref_table, ref_columns}
    sample_size: int
    fingerprint: str

    @property
    def full_name(self) -> str:
        return f"{self.schema}.{self.name}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TableProfile":
        d = dict(d)
        d["columns"] = [ColumnProfile(**c) for c in d["columns"]]
        for c in d["columns"]:
            c.top_values = [tuple(v) for v in c.top_values]
        return cls(**d)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _to_text(v, limit: int = 80) -> str:
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        s = v.isoformat()
    elif isinstance(v, decimal.Decimal):
        s = format(v.normalize(), "f") if v == v.to_integral() else str(v)
    else:
        s = str(v)
    s = s.replace("\n", " ").replace("\r", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _is_binary(v) -> bool:
    return isinstance(v, (bytes, bytearray, memoryview))


def _orderable(v) -> bool:
    return isinstance(v, (int, float, decimal.Decimal, dt.date, dt.datetime, dt.time)) and not isinstance(v, bool)


def profile_values(values: list, with_values: bool = True) -> dict:
    """Compute column statistics from a list of sampled values."""
    n = len(values)
    non_null = [v for v in values if v is not None]
    out: dict = {"null_pct": round(100 * (n - len(non_null)) / n, 1) if n else None}
    if not non_null:
        out["distinct_in_sample"] = 0
        return out
    if any(_is_binary(v) for v in non_null[:5]):
        out["sample_values"] = ["<binary>"]
        return out

    keys = [_to_text(v) for v in non_null]
    counts = Counter(keys)
    distinct = len(counts)
    out["distinct_in_sample"] = distinct

    if all(_orderable(v) for v in non_null[:50]):
        try:
            out["min"], out["max"] = _to_text(min(non_null)), _to_text(max(non_null))
        except TypeError:
            pass
    if all(isinstance(v, str) for v in non_null[:50]):
        out["avg_len"] = round(sum(len(v) for v in non_null if isinstance(v, str)) / len(non_null), 1)

    if with_values:
        # Low-cardinality columns are likely codes/categories: show their distribution.
        if distinct <= 30:
            out["top_values"] = counts.most_common(15)
        elif distinct < 0.5 * len(keys):
            out["top_values"] = counts.most_common(5)
        out["sample_values"] = list(dict.fromkeys(keys))[:3]
    return out


def format_result(columns: list[str], rows: list, truncated: bool, max_chars: int = 6000) -> str:
    """Render a query result as a compact pipe table for the LLM."""
    lines = [" | ".join(columns)]
    for r in rows:
        lines.append(" | ".join("NULL" if v is None else _to_text(v, 60) for v in r))
    if not rows:
        lines.append("(no rows)")
    if truncated:
        lines.append(f"... (truncated to {len(rows)} rows)")
    out = "\n".join(lines)
    return out if len(out) <= max_chars else out[:max_chars] + "\n... (output truncated)"


def check_connection(engine: Engine) -> None:
    """Open the first connection, report who we are connected as, or explain the failure."""
    target = engine.url.database or "the database"
    log.step(f"Connecting to {target} …")
    try:
        with engine.connect() as conn:
            if engine.dialect.name == "mssql":
                server, database, user, version = conn.exec_driver_sql(
                    "SELECT @@SERVERNAME, DB_NAME(), SUSER_SNAME(), "
                    "CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50))").one()
                log.ok(f"Connected to database '{database}' on server '{server}' as {user} "
                       f"(SQL engine {version})")
            else:
                conn.exec_driver_sql("SELECT 1")
                log.ok(f"Connected ({engine.dialect.name})")
    except Exception as e:
        log.error(f"Connection failed: {str(getattr(e, 'orig', e))[:400]}")
        log.info("Hint: " + explain_connection_error(e))
        raise SystemExit(4) from e


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
class Database:
    def __init__(self, engine: Engine, sample_rows: int = 5000, max_result_rows: int = 50,
                 with_values: bool = True):
        self.engine = engine
        self.insp = inspect(engine)
        self.dialect = engine.dialect.name
        self.sqlglot_dialect = sqlguard.SQLGLOT_DIALECTS.get(self.dialect, self.dialect)
        self.sample_rows = sample_rows
        self.max_result_rows = max_result_rows
        self.with_values = with_values
        self._descriptions: dict | None = None
        self.last_row_count = 0

    # ---- discovery -------------------------------------------------------- #
    def list_objects(self, schemas: list[str] | None = None, all_schemas: bool = False,
                     include_views: bool = True, include: list[str] | None = None,
                     exclude: list[str] | None = None) -> list[tuple[str, str, str]]:
        if all_schemas:
            schemas = [s for s in self.insp.get_schema_names()
                       if s not in SYSTEM_SCHEMAS and not s.startswith("db_")]
        schemas = schemas or [self.insp.default_schema_name]
        log.step(f"Listing tables{' and views' if include_views else ''} in schema(s): "
                 f"{', '.join(schemas)}")

        objects = []
        for schema in schemas:
            tables = [(schema, t, "table") for t in self.insp.get_table_names(schema=schema)]
            views = ([(schema, v, "view") for v in self.insp.get_view_names(schema=schema)]
                     if include_views else [])
            log.info(f"Schema {schema}: {len(tables)} tables, {len(views)} views", 1)
            objects += tables + views

        def matches(obj, patterns):
            full, short = f"{obj[0]}.{obj[1]}".lower(), obj[1].lower()
            return any(fnmatch.fnmatch(full, p.lower()) or fnmatch.fnmatch(short, p.lower())
                       for p in patterns)

        if include:
            objects = [o for o in objects if matches(o, include)]
        if exclude:
            objects = [o for o in objects if not matches(o, exclude)]
        if include or exclude:
            log.info(f"After filters (--tables/--exclude): {len(objects)} objects", 1)
        return sorted(objects)

    def _load_descriptions(self) -> dict:
        """MS_Description extended properties (Azure SQL / SQL Server)."""
        if self._descriptions is not None:
            return self._descriptions
        self._descriptions = {}
        if self.dialect != "mssql":
            return self._descriptions
        sql = """
            SELECT s.name, o.name, c.name, CAST(ep.value AS nvarchar(4000))
            FROM sys.extended_properties ep
            JOIN sys.objects o ON ep.major_id = o.object_id
            JOIN sys.schemas s ON o.schema_id = s.schema_id
            LEFT JOIN sys.columns c ON ep.major_id = c.object_id AND ep.minor_id = c.column_id
            WHERE ep.class = 1 AND ep.name = 'MS_Description' AND o.type IN ('U', 'V')
        """
        log.step("Reading existing descriptions (MS_Description) from the database")
        try:
            with self.engine.connect() as conn:
                for schema, obj, col, desc in conn.exec_driver_sql(sql):
                    self._descriptions[(schema, obj, col)] = desc
            log.ok(f"Found {len(self._descriptions)} existing descriptions")
        except Exception as e:
            log.warn(f"Could not read MS_Description (continuing without them): {str(e)[:200]}")
        return self._descriptions

    def _row_count(self, schema: str, name: str, table: Table, kind: str) -> int | None:
        try:
            with self.engine.connect() as conn:
                if self.dialect == "mssql" and kind == "table":
                    # Metadata-based count: instant, no table scan.
                    return conn.exec_driver_sql(
                        "SELECT SUM(p.rows) FROM sys.partitions p "
                        "WHERE p.object_id = OBJECT_ID(?) AND p.index_id IN (0, 1)",
                        (f"[{schema}].[{name}]",),
                    ).scalar()
                return conn.execute(select(func.count()).select_from(table)).scalar()
        except Exception:
            return None

    # ---- profiling -------------------------------------------------------- #
    def profile(self, schema: str, name: str, kind: str) -> TableProfile:
        descriptions = self._load_descriptions()
        log.step("Reading structure: columns, keys and descriptions", 1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # unknown types (geography etc.) are fine
            table = Table(name, MetaData(), schema=schema, autoload_with=self.engine)
            columns = self.insp.get_columns(name, schema=schema)
            try:
                pk = self.insp.get_pk_constraint(name, schema=schema).get("constrained_columns") or []
            except Exception:
                pk = []
            try:
                fks_raw = self.insp.get_foreign_keys(name, schema=schema)
            except Exception:
                fks_raw = []
            try:
                table_comment = self.insp.get_table_comment(name, schema=schema).get("text")
            except Exception:
                table_comment = None

        fks, fk_by_col = [], {}
        for fk in fks_raw:
            ref_schema = fk.get("referred_schema") or schema
            ref = f"{ref_schema}.{fk['referred_table']}"
            fks.append({"columns": fk["constrained_columns"], "ref_table": ref,
                        "ref_columns": fk["referred_columns"]})
            for c, rc in zip(fk["constrained_columns"], fk["referred_columns"]):
                fk_by_col[c] = f"{ref}.{rc}"

        log.info(f"{len(columns)} columns, primary key: {', '.join(pk) or 'none'}, "
                 f"declared foreign keys: {len(fks)}", 1)

        # Sample rows (TOP n on Azure SQL). Not random, but cheap and good enough.
        log.step(f"Sampling up to {self.sample_rows:,} rows to profile the values", 1)
        with self.engine.connect() as conn:
            result = conn.execute(select(table).limit(self.sample_rows))
            keys = list(result.keys())
            rows = result.fetchall()

        col_profiles = []
        for c in columns:
            idx = keys.index(c["name"]) if c["name"] in keys else None
            values = [r[idx] for r in rows] if idx is not None else []
            stats = profile_values(values, self.with_values) if idx is not None else {}
            col_profiles.append(ColumnProfile(
                name=c["name"],
                type=str(c["type"]),
                nullable=bool(c.get("nullable", True)),
                is_pk=c["name"] in pk,
                fk=fk_by_col.get(c["name"]),
                db_description=descriptions.get((schema, name, c["name"])) or c.get("comment"),
                **stats,
            ))

        row_count = self._row_count(schema, name, table, kind)
        total = f"{row_count:,}" if row_count is not None else "unknown"
        log.ok(f"Profiled {len(rows):,} sampled rows (table has {total} rows)", 1)

        fingerprint_src = json.dumps(
            [[c.name, c.type, c.nullable] for c in col_profiles] + [pk, fks], sort_keys=True
        )
        return TableProfile(
            schema=schema, name=name, kind=kind,
            row_count=row_count,
            db_description=descriptions.get((schema, name, None)) or table_comment,
            columns=col_profiles, primary_key=pk, foreign_keys=fks,
            sample_size=len(rows),
            fingerprint=hashlib.sha1(fingerprint_src.encode()).hexdigest()[:12],
        )

    def schema_summary(self, objects: list[tuple[str, str, str]], max_cols: int = 30) -> str:
        """Compact 'schema.table(col, col, ...)' list so the agent sees neighbours."""
        lines = []
        for schema, name, _kind in objects:
            try:
                cols = [c["name"] for c in self.insp.get_columns(name, schema=schema)]
            except Exception:
                continue
            more = f", …+{len(cols) - max_cols}" if len(cols) > max_cols else ""
            lines.append(f"{schema}.{name}({', '.join(cols[:max_cols])}{more})")
        return "\n".join(lines)

    # ---- agent tool ------------------------------------------------------- #
    def run_readonly(self, sql: str) -> str:
        sqlguard.validate(sql, self.sqlglot_dialect)
        with self.engine.connect() as conn:
            # exec_driver_sql: no bind-parameter parsing of ':' inside the query.
            result = conn.exec_driver_sql(sql)
            cols = list(result.keys())
            rows = result.fetchmany(self.max_result_rows + 1)
            conn.rollback()
        truncated = len(rows) > self.max_result_rows
        self.last_row_count = min(len(rows), self.max_result_rows)
        return format_result(cols, rows[: self.max_result_rows], truncated)
