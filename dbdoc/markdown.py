"""Markdown output (one file per table, an index and an ER diagram) and the run cache."""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from .agent import TableResult
from .db import TableProfile

MANUAL_MARKER = "<!-- MANUAL NOTES: everything below this line is kept when the docs are regenerated -->"
MANUAL_DEFAULT = "## Manual notes\n\n_Add business context here. The agent reads it on the next run._\n"
_UNSAFE = re.compile(r"[^\w.\-]+", re.UNICODE)


def file_stem(schema: str, name: str) -> str:
    return _UNSAFE.sub("_", f"{schema}.{name}")


def _cell(text) -> str:
    if text is None:
        return ""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _link(full_name: str, from_tables_dir: bool = True) -> str:
    schema, _, name = full_name.partition(".")
    target = f"{file_stem(schema, name)}.md"
    return f"[{full_name}]({target if from_tables_dir else 'tables/' + target})"


# --------------------------------------------------------------------------- #
# Manual notes & cache
# --------------------------------------------------------------------------- #
def read_manual_notes(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    if MANUAL_MARKER not in text:
        return ""
    notes = text.split(MANUAL_MARKER, 1)[1].strip()
    return "" if notes == MANUAL_DEFAULT.strip() else notes


def _manual_section_for_llm(notes: str) -> str:
    return re.sub(r"^## Manual notes\s*", "", notes).strip()


def cache_path(out: Path, schema: str, name: str) -> Path:
    return out / ".dbdoc" / f"{file_stem(schema, name)}.json"


def save_cache(out: Path, result: TableResult) -> None:
    path = cache_path(out, result.profile.schema, result.profile.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "profile": result.profile.to_dict(), "doc": result.doc,
        "queries": result.queries, "model": result.model,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def load_cache(path: Path) -> TableResult | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return TableResult(profile=TableProfile.from_dict(d["profile"]), doc=d.get("doc"),
                           queries=d.get("queries", []), model=d.get("model", "none"))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Table file
# --------------------------------------------------------------------------- #
def render_table(result: TableResult, manual_notes: str) -> str:
    p, doc = result.profile, result.doc or {}
    docs_by_col = {c["name"]: c for c in doc.get("columns", [])}
    rows = f"{p.row_count:,}" if p.row_count is not None else "unknown"
    now = dt.datetime.now().isoformat(timespec="seconds")

    out = [
        "---",
        f"name: {p.full_name}",
        f"schema: {p.schema}",
        f"object: {p.name}",
        f"kind: {p.kind}",
        f"rows: {p.row_count if p.row_count is not None else ''}",
        f"fingerprint: {p.fingerprint}",
        f"generated_at: {now}",
        f"generator: dbdoc-agent ({result.model})",
        f"documented: {'yes' if result.doc else 'profile-only'}",
        "---",
        "",
        f"# {p.full_name}",
        "",
    ]
    summary = doc.get("summary") or p.db_description or "_Not documented yet (profile only)._"
    out += [summary, ""]
    if doc.get("grain"):
        out += [f"**Grain:** {doc['grain']}", ""]
    out += [f"**Type:** {p.kind} · **Rows:** {rows} · "
            f"**Primary key:** {', '.join(f'`{c}`' for c in p.primary_key) or 'none declared'}", ""]

    # Columns
    out += ["## Columns", "",
            "| Column | Type | Null | Key | Description | Profile (sample) |",
            "|---|---|---|---|---|---|"]
    for c in p.columns:
        d = docs_by_col.get(c.name, {})
        key = "PK" if c.is_pk else ""
        if c.fk:
            key = (key + " " if key else "") + "FK"
        desc = d.get("description") or c.db_description or ""
        if d and d.get("confidence") == "inferred":
            desc += " _(inferred)_"
        stats = []
        if c.null_pct:
            stats.append(f"{c.null_pct}% null")
        if c.distinct_in_sample is not None:
            stats.append(f"{c.distinct_in_sample} distinct")
        if c.min is not None:
            stats.append(f"{c.min} … {c.max}")
        out.append(f"| `{_cell(c.name)}` | {_cell(c.type)} | {'yes' if c.nullable else 'no'} | "
                   f"{key} | {_cell(desc)} | {_cell(', '.join(stats))} |")
    out.append("")

    # Coded values
    coded = [(c.name, docs_by_col[c.name]["value_meanings"]) for c in p.columns
             if docs_by_col.get(c.name, {}).get("value_meanings")]
    if coded:
        out += ["## Coded values", ""]
        for name, meanings in coded:
            out += [f"### `{name}`", "", "| Value | Meaning |", "|---|---|"]
            out += [f"| `{_cell(m['value'])}` | {_cell(m['meaning'])} |" for m in meanings]
            out.append("")

    # Relationships: declared FKs first, then what the agent found
    rel_lines, seen = [], set()
    for fk in p.foreign_keys:
        for col, rc in zip(fk["columns"], fk["ref_columns"]):
            seen.add((col, fk["ref_table"].lower()))
            rel_lines.append(f"- `{col}` → {_link(fk['ref_table'])}.`{rc}` — declared foreign key")
    for r in doc.get("relationships", []):
        if (r["column"], r["ref_table"].lower()) in seen:
            continue
        extra = f" ({r['cardinality']})" if r.get("cardinality") else ""
        evidence = f": {r['evidence']}" if r.get("evidence") else ""
        rel_lines.append(f"- `{r['column']}` → {_link(r['ref_table'])}.`{r['ref_column']}`{extra} — "
                         f"{r['confidence']}{evidence}")
    if rel_lines:
        out += ["## Relationships", "", *rel_lines, ""]

    if doc.get("notes"):
        out += ["## Notes and data quality", "", *[f"- {n}" for n in doc["notes"]], ""]

    if doc.get("example_queries"):
        out += ["## Example queries", ""]
        for q in doc["example_queries"]:
            out += [f"**{q['question']}**", "", "```sql", q["sql"].strip(), "```", ""]

    if result.queries:
        ok = sum(q["ok"] for q in result.queries)
        out += ["## Verification log", "",
                f"<details><summary>{len(result.queries)} queries run by the agent ({ok} succeeded)</summary>",
                ""]
        for q in result.queries:
            mark = "✓" if q["ok"] else "✗"
            out += [f"{mark} {q['purpose']}", "", "```sql", q["sql"].strip(), "```", ""]
        out += ["</details>", ""]

    out += [MANUAL_MARKER, "", manual_notes.strip() or MANUAL_DEFAULT.strip(), ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Index and ER diagram
# --------------------------------------------------------------------------- #
def _mermaid_id(full_name: str) -> str:
    return re.sub(r"\W+", "_", full_name)


def all_relationships(results: list[TableResult]) -> list[tuple[str, str, str, str]]:
    """(child, parent, column, kind) for declared FKs and verified inferred ones."""
    known = {r.profile.full_name.lower(): r.profile.full_name for r in results}
    rels, seen = [], set()
    for r in results:
        child = r.profile.full_name
        for fk in r.profile.foreign_keys:
            key = (child, fk["ref_table"].lower(), ",".join(fk["columns"]))
            if key not in seen:
                seen.add(key)
                rels.append((child, known.get(fk["ref_table"].lower(), fk["ref_table"]),
                             ", ".join(fk["columns"]), "declared"))
        for rel in (r.doc or {}).get("relationships", []):
            key = (child, rel["ref_table"].lower(), rel["column"])
            if key not in seen and rel.get("confidence") == "verified":
                seen.add(key)
                rels.append((child, known.get(rel["ref_table"].lower(), rel["ref_table"]),
                             rel["column"], "verified"))
    return rels


def render_relationships(results: list[TableResult]) -> str:
    rels = all_relationships(results)
    out = ["# Relationships", "",
           "Declared foreign keys and relationships verified by the agent against the data.", "",
           "```mermaid", "erDiagram"]
    for child, parent, col, _kind in rels:
        out.append(f'    {_mermaid_id(parent)} ||--o{{ {_mermaid_id(child)} : "{col}"')
    out += ["```", "", "| From | Column | To | Source |", "|---|---|---|---|"]
    out += [f"| {_link(c, False)} | `{col}` | {_link(p, False)} | {k} |" for c, p, col, k in rels]
    return "\n".join(out) + "\n"


def render_index(results: list[TableResult], overview: str, database_label: str,
                 manual_notes: str) -> str:
    out = [f"# Database documentation: {database_label}", "",
           f"_Generated by dbdoc-agent on {dt.date.today().isoformat()}. "
           f"{len(results)} objects. See also [relationships](relationships.md)._", ""]
    if overview:
        out += ["## Overview", "", overview.strip(), ""]
    out += ["## Objects", "", "| Object | Type | Rows | Summary |", "|---|---|---|---|"]
    for r in sorted(results, key=lambda r: r.profile.full_name.lower()):
        p = r.profile
        summary = ((r.doc or {}).get("summary") or p.db_description or "").split(". ")[0]
        rows = f"{p.row_count:,}" if p.row_count is not None else ""
        out.append(f"| {_link(p.full_name, False)} | {p.kind} | {rows} | {_cell(summary)} |")
    out += ["", MANUAL_MARKER, "", manual_notes.strip() or MANUAL_DEFAULT.strip(), ""]
    return "\n".join(out)


def manual_notes_for_llm(path: Path) -> str:
    return _manual_section_for_llm(read_manual_notes(path))
