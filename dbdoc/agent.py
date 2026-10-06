"""The documentation agent: profile -> investigate with SQL -> submit."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import log
from .db import Database, TableProfile
from .llm import BaseLLM
from .sqlguard import UnsafeQuery

SYSTEM_PROMPT = """You are a senior data architect documenting a {dialect} database \
so that analysts and AI agents can query it correctly.

You document ONE table or view at a time. You receive a profile computed from a \
sample of rows, plus the list of other tables. Your job:
1. Explain what the object represents and its grain (what exactly one row is).
2. Describe every column in 1-2 sentences: business meaning, not just the name repeated.
3. Decode coded values (status codes, flags, type columns) — check them with SQL.
4. Find relationships: declared foreign keys, and implicit ones you suspect from names.
   Verify implicit ones by checking that values match the referenced table
   (e.g. count orphans with a LEFT JOIN).
5. Note data quality issues and pitfalls (duplicates, NULL-heavy columns, mixed
   units, soft deletes, columns that look unused).
6. Write 2-4 example queries that answer realistic business questions.

Rules:
- Use run_sql to verify every non-obvious claim. Only single read-only SELECT
  statements are allowed. Always limit rows ({limit_hint}) and prefer aggregates
  (COUNT, GROUP BY, MIN/MAX) over raw rows. Tables can be large: avoid full
  scans with expensive operations on huge tables when an aggregate on a
  narrower predicate will do.
- Mark each column and relationship with confidence "verified" (checked with
  data or declared in the schema / existing descriptions) or "inferred"
  (plausible but not proven). Never present a guess as a fact.
- Existing descriptions from the database and human notes are authoritative.
- Do NOT copy personal data (names, e-mails, phone numbers, addresses, personal
  IDs) into the documentation. Describe the column instead.
- You have a budget of {max_steps} tool calls. Finish by calling
  submit_documentation exactly once.
- Write all descriptions in {language}. Keep SQL identifiers unchanged.
"""

TOOLS = [
    {
        "name": "run_sql",
        "description": "Run one read-only SELECT against the database and get up to 50 rows back.",
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A single SELECT statement."},
                "purpose": {"type": "string", "description": "What this query verifies (one sentence)."},
            },
            "required": ["sql", "purpose"],
        },
    },
    {
        "name": "submit_documentation",
        "description": "Submit the final documentation of the table. Call exactly once, at the end.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "What the table represents, 1-3 sentences."},
                "grain": {"type": "string", "description": "What exactly one row represents."},
                "columns": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "role": {"type": "string", "enum": [
                                "identifier", "foreign_key", "category", "measure", "date",
                                "flag", "text", "technical", "other"]},
                            "confidence": {"type": "string", "enum": ["verified", "inferred"]},
                            "value_meanings": {
                                "type": "array",
                                "description": "Meaning of coded values, if the column is a code/flag.",
                                "items": {"type": "object", "properties": {
                                    "value": {"type": "string"}, "meaning": {"type": "string"}},
                                    "required": ["value", "meaning"]},
                            },
                        },
                        "required": ["name", "description", "role", "confidence"],
                    },
                },
                "relationships": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "column": {"type": "string"},
                            "ref_table": {"type": "string", "description": "schema.table"},
                            "ref_column": {"type": "string"},
                            "cardinality": {"type": "string", "description": "e.g. many-to-one"},
                            "confidence": {"type": "string", "enum": ["verified", "inferred"]},
                            "evidence": {"type": "string"},
                        },
                        "required": ["column", "ref_table", "ref_column", "confidence"],
                    },
                },
                "notes": {"type": "array", "items": {"type": "string"},
                          "description": "Data quality issues, pitfalls, business rules."},
                "example_queries": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "question": {"type": "string"}, "sql": {"type": "string"}},
                        "required": ["question", "sql"]},
                },
            },
            "required": ["summary", "grain", "columns"],
        },
    },
]


@dataclass
class TableResult:
    profile: TableProfile
    doc: dict | None
    queries: list[dict] = field(default_factory=list)
    model: str = "none"


def render_profile(p: TableProfile) -> str:
    rows = f"{p.row_count:,}" if p.row_count is not None else "unknown"
    lines = [
        f"# {p.kind.upper()} {p.full_name}",
        f"Rows: {rows} | Profiled sample: {p.sample_size} rows",
        f"Primary key: {', '.join(p.primary_key) or 'none declared'}",
    ]
    if p.db_description:
        lines.append(f"Existing description: {p.db_description}")
    for fk in p.foreign_keys:
        lines.append(f"Declared FK: ({', '.join(fk['columns'])}) -> {fk['ref_table']}"
                     f"({', '.join(fk['ref_columns'])})")
    lines.append("\nColumns:")
    for c in p.columns:
        parts = [f"- {c.name} {c.type}{'' if c.nullable else ' NOT NULL'}"]
        if c.is_pk:
            parts.append("PK")
        if c.fk:
            parts.append(f"FK->{c.fk}")
        if c.null_pct is not None:
            parts.append(f"nulls {c.null_pct}%")
        if c.distinct_in_sample is not None:
            parts.append(f"distinct(sample) {c.distinct_in_sample}")
        if c.min is not None:
            parts.append(f"range {c.min} .. {c.max}")
        if c.avg_len is not None:
            parts.append(f"avg_len {c.avg_len}")
        if c.top_values:
            parts.append("top: " + ", ".join(f"{v} ({n})" for v, n in c.top_values))
        elif c.sample_values:
            parts.append("e.g. " + ", ".join(c.sample_values))
        if c.db_description:
            parts.append(f'existing description: "{c.db_description}"')
        lines.append(" | ".join(parts))
    return "\n".join(lines)


class DocAgent:
    def __init__(self, db: Database, llm: BaseLLM, language: str = "English", max_steps: int = 12):
        self.db = db
        self.llm = llm
        self.language = language
        self.max_steps = max_steps

    def _system(self) -> str:
        if self.db.dialect == "mssql":
            dialect, limit = "Azure SQL / SQL Server (T-SQL)", "use SELECT TOP (n)"
        else:
            dialect, limit = self.db.dialect, "use LIMIT n"
        return SYSTEM_PROMPT.format(dialect=dialect, limit_hint=limit,
                                    max_steps=self.max_steps, language=self.language)

    def document(self, profile: TableProfile, schema_summary: str,
                 manual_notes: str = "", max_summary_chars: int = 15000) -> TableResult:
        if len(schema_summary) > max_summary_chars:
            schema_summary = schema_summary[:max_summary_chars] + "\n… (list truncated)"
        user = render_profile(profile) + "\n\nOther objects in the database:\n" + schema_summary
        if manual_notes.strip():
            user += "\n\nHuman notes about this table (authoritative):\n" + manual_notes.strip()

        queries: list[dict] = []
        column_names = {c.name for c in profile.columns}
        log.step(f"Agent ({self.llm.label}) is investigating the table "
                 f"(budget: {self.max_steps} steps)", 1)

        def handle(name: str, args: dict) -> tuple[str, bool]:
            if name == "run_sql":
                sql = args.get("sql", "")
                entry = {"purpose": args.get("purpose", ""), "sql": sql, "ok": False}
                queries.append(entry)
                log.info(f"Query {len(queries)}: {entry['purpose'] or '(no purpose given)'}", 2)
                log.detail(" ".join(sql.split())[:400], 3)
                try:
                    out = self.db.run_readonly(sql)
                    entry["ok"] = True
                    log.detail(f"→ {log.plural(self.db.last_row_count, 'row')}", 3)
                    return out, False
                except UnsafeQuery as e:
                    log.warn(f"Query blocked by the read-only guard: {e}", 3)
                    return f"REJECTED: {e}", False
                except Exception as e:
                    first = str(getattr(e, "orig", e)).splitlines()[0][:200]
                    log.warn(f"Query failed (the agent will adjust): {first}", 3)
                    return f"ERROR: {str(e)[:500]}", False
            if name == "submit_documentation":
                cols = args.get("columns") or []
                unknown = [c.get("name") for c in cols if c.get("name") not in column_names]
                missing = column_names - {c.get("name") for c in cols}
                if unknown:
                    log.warn(f"Agent referenced unknown columns {unknown}; asking it to fix", 2)
                    return (f"ERROR: unknown columns {unknown}. Use exact names from the "
                            f"profile and submit again."), False
                if missing and len(missing) > len(column_names) // 2:
                    log.warn("Agent skipped most columns; asking it to complete the documentation", 2)
                    return (f"ERROR: most columns are missing ({sorted(missing)[:20]}). "
                            "Document every column and submit again."), False
                log.ok("Agent submitted documentation: "
                       f"{log.plural(len(cols), 'column')}, "
                       f"{log.plural(len(args.get('relationships') or []), 'relationship')}, "
                       f"{log.plural(len(args.get('notes') or []), 'note')}, "
                       f"{log.plural(len(args.get('example_queries') or []), 'example query')}"
                       .replace("querys", "queries"), 1)
                return "OK", True
            return f"ERROR: unknown tool {name}", False

        doc = self.llm.run_tools(self._system(), user, TOOLS, handle,
                                 final_tool="submit_documentation", max_steps=self.max_steps)
        if doc is None:
            log.warn("Agent did not finish within its step budget; saving the profile only "
                     "(try --max-steps 20)", 1)
        return TableResult(profile=profile, doc=doc, queries=queries, model=self.llm.label)

    def overview(self, summaries: list[tuple[str, str]]) -> str:
        listing = "\n".join(f"- {name}: {summary}" for name, summary in summaries)
        system = ("You are a data architect. Write a concise overview of a database for new "
                  f"analysts, in {self.language}, as Markdown without a top-level heading: "
                  "2-3 paragraphs on the business domain, then a bullet list of the key "
                  "entities and how they connect, then where to start for typical questions. "
                  "Only use facts from the table summaries.")
        return self.llm.complete(system, "Table summaries:\n" + listing)
