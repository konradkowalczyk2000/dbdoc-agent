"""Command line entry point: python -m dbdoc --help"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .agent import DocAgent, TableResult
from . import log
from .db import Database, check_connection, make_engine
from .llm import get_llm
from . import markdown as md


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="dbdoc",
        description="Agent that documents an Azure SQL database as Markdown files.")
    p.add_argument("--out", default="db_docs", help="Output folder (default: db_docs)")
    p.add_argument("--schema", action="append", help="Schema to document (repeatable). Default: dbo")
    p.add_argument("--all-schemas", action="store_true", help="Document all user schemas")
    p.add_argument("--tables", help="Comma-separated names or patterns, e.g. 'Orders,sales.*,dim*'")
    p.add_argument("--exclude", help="Comma-separated patterns to skip, e.g. '*_bak,tmp*'")
    p.add_argument("--no-views", action="store_true", help="Skip views")
    p.add_argument("--auth", choices=["browser", "device", "cli", "default"],
                   help="Microsoft Entra ID login with MFA via azure-identity "
                        "(overrides AZURE_SQL_AUTH). Omit to use the ODBC string as is.")
    p.add_argument("--provider", default=os.getenv("DBDOC_PROVIDER", "anthropic"),
                   choices=["anthropic", "azure_openai", "openai", "none"],
                   help="LLM provider; 'none' = profile only, no LLM (default: anthropic)")
    p.add_argument("--model", help="Model name (Azure: deployment name)")
    p.add_argument("--language", default=os.getenv("DBDOC_LANGUAGE", "English"),
                   help="Language of the descriptions, e.g. Polish (default: English)")
    p.add_argument("--max-steps", type=int, default=12, help="Max agent tool calls per table")
    p.add_argument("--sample-rows", type=int, default=5000, help="Rows sampled per table for profiling")
    p.add_argument("--no-sample-values", action="store_true",
                   help="Do not send example values to the LLM (only statistics)")
    p.add_argument("--force", action="store_true", help="Regenerate even if the table did not change")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Also print every SQL query of the agent and each model step")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="Print only one line per table and errors")
    return p.parse_args(argv)


def _split(s: str | None) -> list[str] | None:
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def _server_from_odbc(odbc: str) -> str | None:
    return next((part.split("=", 1)[1].strip() for part in odbc.split(";")
                 if part.strip().lower().startswith("server=")), None)


def _load_env() -> bool | None:
    """Load .env (found by walking up from this package's folder, i.e. the project folder).
    Returns True/False for found/not found, None if python-dotenv is missing."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None
    return load_dotenv(override=True)  # .env wins over stale variables in the terminal


def main(argv=None) -> int:
    # .env must be loaded before parsing: option defaults come from DBDOC_PROVIDER / DBDOC_LANGUAGE.
    env_found = _load_env()
    args = parse_args(argv)
    log.VERBOSE, log.QUIET = args.verbose, args.quiet and not args.verbose
    run_started = time.time()

    log.banner("dbdoc-agent: documenting the database as Markdown")
    if env_found is None:
        log.warn("python-dotenv is not installed: .env is ignored (pip install python-dotenv)")
    else:
        log.info("Loaded settings from .env" if env_found else
                 "No .env file found in the project folder: using environment variables only")

    out = Path(args.out)
    (out / "tables").mkdir(parents=True, exist_ok=True)
    log.info(f"Output folder: {out.resolve()}")

    # ---- connection ------------------------------------------------------- #
    auth = args.auth or os.getenv("AZURE_SQL_AUTH") or None
    odbc, db_url = os.getenv("AZURE_SQL_ODBC"), os.getenv("DB_URL")
    if not odbc and not db_url:
        log.error("No database configured. Set AZURE_SQL_ODBC (or DB_URL) in .env, see .env.example.")
        return 1
    if odbc:
        log.info(f"Server: {_server_from_odbc(odbc) or '(no Server= in AZURE_SQL_ODBC!)'} | "
                 f"login: {'Microsoft Entra ID, ' + auth + ' mode' if auth else 'as set in the connection string'}")
    try:
        engine = make_engine(url=db_url, odbc=odbc, auth=auth)
    except ValueError as e:
        log.error(str(e))
        return 1
    check_connection(engine)
    db = Database(engine, sample_rows=args.sample_rows, with_values=not args.no_sample_values)

    # ---- LLM -------------------------------------------------------------- #
    try:
        llm = get_llm(args.provider, args.model)
    except Exception as e:
        log.error(f"Could not set up the LLM provider '{args.provider}': {type(e).__name__}: {e}")
        log.info("Hint: check the API key / endpoint variables in .env, or run with --provider none")
        return 1
    if llm:
        log.ok(f"LLM ready: {llm.label}, descriptions in {args.language}"
               f"{'' if not args.no_sample_values else ', example values are NOT sent to the model'}")
    else:
        log.info("No LLM (--provider none): only profiling the structure and data, no descriptions")
    agent = DocAgent(db, llm, language=args.language, max_steps=args.max_steps) if llm else None

    # ---- discovery -------------------------------------------------------- #
    objects = db.list_objects(schemas=args.schema, all_schemas=args.all_schemas,
                              include_views=not args.no_views,
                              include=_split(args.tables), exclude=_split(args.exclude))
    if not objects:
        log.error("No tables found. Check --schema / --tables and the login's permissions "
                  "(the user needs db_datareader and VIEW DEFINITION).")
        return 1
    n_views = sum(kind == "view" for *_, kind in objects)
    log.ok(f"Will analyze {len(objects)} objects ({len(objects) - n_views} tables, {n_views} views)")
    log.step("Collecting the list of all columns, so the agent can spot relationships")
    summary = db.schema_summary(objects)

    # ---- tables ----------------------------------------------------------- #
    results: list[TableResult] = []
    changed, skipped, failed = 0, 0, []
    for i, (schema, name, kind) in enumerate(objects, 1):
        full = f"{schema}.{name}"
        log.banner(f"[{i}/{len(objects)}] Analyzing {kind} {full}")
        md_path = out / "tables" / f"{md.file_stem(schema, name)}.md"
        cached = md.load_cache(md.cache_path(out, schema, name))
        started = time.time()
        try:
            profile = db.profile(schema, name, kind)
            unchanged = (cached and cached.profile.fingerprint == profile.fingerprint
                         and (cached.doc or not agent) and md_path.exists())
            if unchanged and not args.force:
                log.ok(f"[{i}/{len(objects)}] {full}: structure unchanged since the last run, "
                       "skipped (use --force to regenerate)")
                results.append(cached)
                skipped += 1
                continue

            notes = md.read_manual_notes(md_path)
            if notes:
                log.info("Found manual notes in the existing file: they are kept and given to the agent", 1)
            if agent:
                result = agent.document(profile, summary, md.manual_notes_for_llm(md_path))
            else:
                result = TableResult(profile=profile, doc=None)

            log.step(f"Writing {md_path.as_posix()}", 1)
            md_path.write_text(md.render_table(result, notes), encoding="utf-8")
            md.save_cache(out, result)
            results.append(result)
            changed += 1
            what = ("documented" if result.doc else
                    "profiled only" if not agent else "profiled only (agent did not finish)")
            queries = f", {log.plural(len(result.queries), 'agent query')}".replace("querys", "queries") if result.queries else ""
            log.ok(f"[{i}/{len(objects)}] {full}: {what}{queries} in {time.time() - started:.0f}s")
        except KeyboardInterrupt:
            log.warn("Interrupted by the user. Finished tables are saved; run again to continue "
                     "where you stopped.")
            break
        except Exception as e:
            failed.append(full)
            log.error(f"[{i}/{len(objects)}] {full} failed: {type(e).__name__}: "
                      f"{str(getattr(e, 'orig', e))[:300]}")
            log.info("Continuing with the next table", 1)
            if cached:
                results.append(cached)

    # Tables documented in earlier runs but outside this run's filter stay in the index.
    in_run = {r.profile.full_name for r in results}
    for path in sorted((out / ".dbdoc").glob("*.json")):
        cached = md.load_cache(path)
        if cached and cached.profile.full_name not in in_run:
            results.append(cached)

    # ---- index, overview, ER diagram ---------------------------------------- #
    log.banner("Writing the database overview, index and relationships")
    overview_path = out / ".dbdoc" / "overview.md"
    overview = overview_path.read_text(encoding="utf-8") if overview_path.exists() else ""
    documented = [(r.profile.full_name, r.doc["summary"]) for r in results if r.doc]
    if agent and documented and (changed or not overview):
        log.step(f"Asking the model for an overview of the database ({len(documented)} documented objects)")
        try:
            overview = agent.overview(documented)
            overview_path.parent.mkdir(parents=True, exist_ok=True)
            overview_path.write_text(overview, encoding="utf-8")
            log.ok("Overview written")
        except Exception as e:
            log.warn(f"Overview failed, keeping the previous one: {e}")
    elif overview:
        log.info("Nothing changed: keeping the existing overview")

    index_path = out / "README.md"
    db_label = engine.url.database or "database"
    log.step(f"Writing index {index_path.as_posix()} ({len(results)} objects)")
    index_path.write_text(md.render_index(results, overview, db_label, md.read_manual_notes(index_path)),
                          encoding="utf-8")
    n_rel = len(md.all_relationships(results))
    log.step(f"Writing {(out / 'relationships.md').as_posix()} ({log.plural(n_rel, 'relationship')} in the ER diagram)")
    (out / "relationships.md").write_text(md.render_relationships(results), encoding="utf-8")

    elapsed = time.time() - run_started
    log.banner("Summary")
    log.ok(f"Finished in {elapsed // 60:.0f} min {elapsed % 60:.0f} s: {changed} documented, "
           f"{skipped} unchanged, {len(failed)} failed")
    log.info(f"Open {index_path.resolve()} to start reading")
    if failed:
        log.error("Failed objects: " + ", ".join(failed))
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
