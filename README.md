# dbdoc-agent

An LLM agent that analyzes an **Azure SQL** database and writes local documentation as Markdown files: one file per table or view, plus a database index and a relationship diagram. The approach is similar to [metalayer](https://github.com/ryanjanssen/metalayer). The agent doesn't guess: it checks its hypotheses with read-only queries against the data. Notes that people add by hand are kept and treated as the authoritative source on the next run.

## How it works

For each table or view:

1. **Profiling (no LLM).** The tool reads the columns, data types, primary key, declared foreign keys, existing `MS_Description` comments, and the row count (from metadata, without scanning the table). From a `TOP (n)` sample of rows it then computes, per column: the share of NULLs, the number of distinct values, the value distribution for code-like columns, and min/max.
2. **Investigation (agent).** The model receives the table profile and the list of all other tables. It has two tools:
   - `run_sql` runs one read-only `SELECT` (validated, at most 50 result rows);
   - `submit_documentation` returns the structured result: a summary, the grain (what one row represents), column descriptions, meanings of coded values, relationships, data-quality notes and example queries.

   The agent checks things such as the meaning of status codes and undeclared relationships, for example by counting orphaned rows with a `LEFT JOIN`. Every description is marked `verified` or `inferred`.
3. **Output.** The agent writes `tables/<schema>.<table>.md`. Each file has a "Verification log" section listing every query the agent ran, so every claim can be checked.

At the end it writes `README.md` (a domain overview and the list of objects) and `relationships.md` (a Mermaid ER diagram built from declared foreign keys plus relationships the agent verified against the data). The folder reads well in Obsidian, VS Code and GitHub.

```
db_docs/
├── README.md               # database overview + object index
├── relationships.md        # ER diagram (Mermaid)
├── tables/
│   ├── dbo.Orders.md
│   └── dbo.Customers.md
└── .dbdoc/                 # cache (profile + agent output), do not edit
```

## Installation (Windows)

All commands below are for **PowerShell** (for example the VS Code terminal) and are run from the project folder, e.g. `C:\projects\dbdoc-agent`.

**Requirements**

- Python 3.10 or later (64-bit), from [python.org](https://www.python.org/downloads/windows/) or `winget install Python.Python.3.13`
- [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server) (x64): `winget install Microsoft.msodbcsql.18`. Driver 17 also works if you change the name in `Driver={...}`. Do not use the legacy `SQL Server` driver: it does not support Entra ID.
- Optional: [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli-windows) (`winget install Microsoft.AzureCLI`), only needed for `--auth cli`

**Set up the project**

```powershell
cd C:\projects\dbdoc-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env        # then edit .env, e.g. notepad .env
```

If activation fails with *"running scripts is disabled on this system"*, allow local scripts for your user once and activate again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

**Check the installation**

```powershell
python --version
python -c "import pyodbc; print(pyodbc.drivers())"          # must list 'ODBC Driver 18 for SQL Server'
python -c "import azure.identity, anthropic, sqlglot; print('ok')"
```

**VS Code tips**

- Select the interpreter from `.venv`: `Ctrl+Shift+P` → **Python: Select Interpreter**.
- You don't need the `python.terminal.useEnvFile` setting: the agent loads `.env` itself. After editing `.env`, open a new terminal if values seem stale.

## Configuration (`.env`)

The agent loads `.env` from the current folder (run it from the project folder). Values in `.env` override variables already set in the terminal. Write each setting on one line, without quotes around the value.

| Variable | Meaning |
|---|---|
| `AZURE_SQL_ODBC` | ODBC connection string, e.g. `Driver={ODBC Driver 18 for SQL Server};Server=tcp:<server>.database.windows.net,1433;Database=<db>;Encrypt=yes;ApplicationIntent=ReadOnly` |
| `AZURE_SQL_AUTH` | Microsoft Entra ID sign-in mode: `browser`, `device`, `cli` or `default` (see below). Leave empty for a SQL login in the connection string. |
| `AZURE_TENANT_ID` | Tenant of the SQL server. Needed when your account belongs to several tenants or is a guest. |
| `DB_URL` | Alternative to `AZURE_SQL_ODBC`: any SQLAlchemy URL (e.g. `sqlite:///test.db`) |
| `DBDOC_PROVIDER` | `anthropic`, `azure_openai`, `openai` or `none` |
| `DBDOC_LANGUAGE` | Language of the descriptions, e.g. `English`, `Polish` |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Claude settings |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION` | Azure OpenAI settings. Leave the key empty to sign in with Entra ID. |

## Signing in with Microsoft Entra ID (MFA)

The agent gets an Entra ID token through `azure-identity` and passes it to the ODBC driver. You complete MFA once per run, and the token refreshes automatically. In this mode the connection string must not contain `Authentication`, `Uid` or `Pwd`.

```powershell
# browser window with sign-in and MFA
python -m dbdoc --auth browser

# no browser (server, SSH): enter the displayed code on your phone or another computer
python -m dbdoc --auth device

# reuse an earlier `az login --tenant <tenant-id>` (MFA happens there)
python -m dbdoc --auth cli

# unattended runs in Azure (managed identity) or with a service principal in environment variables
python -m dbdoc --auth default
```

You can also set the mode permanently in `.env` (`AZURE_SQL_AUTH=browser`).

The Entra user needs access to the database:

```sql
CREATE USER [john.smith@company.com] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [john.smith@company.com];
GRANT VIEW DEFINITION TO [john.smith@company.com];
```

As an alternative, the ODBC driver can handle MFA itself if you put `Authentication=ActiveDirectoryInteractive;UID=...` in the connection string and don't use `--auth`. That path depends on the operating system and driver version, so `--auth browser` is the recommended default.

## Read-only login (recommended)

The SQL validator blocks writes, but it is the second line of defence. The first should be a user with no write permissions:

```sql
-- in the target database
CREATE USER dbdoc_reader WITH PASSWORD = '<strong-password>';   -- or: FROM EXTERNAL PROVIDER (Entra ID)
ALTER ROLE db_datareader ADD MEMBER dbdoc_reader;
GRANT VIEW DEFINITION TO dbdoc_reader;                         -- metadata and MS_Description
```

If the database has read replicas (Premium, Business Critical or Hyperscale tiers), `ApplicationIntent=ReadOnly` in the connection string sends the agent's queries to a replica, away from production.

## Usage

Run from the project folder with the virtual environment active (`.\.venv\Scripts\Activate.ps1`). In PowerShell, keep patterns with `*` or commas in double quotes.

```powershell
# whole dbo schema, descriptions in Polish, Claude
python -m dbdoc --language Polish

# selected tables (patterns), Azure OpenAI
python -m dbdoc --provider azure_openai --tables "Orders,sales.*,dim*"

# all schemas, skipping backup and temporary tables
python -m dbdoc --all-schemas --exclude "*_bak,tmp*,*_old"

# profiling only, no LLM (quick look at the structure, no cost)
python -m dbdoc --provider none

# sensitive data: only statistics go to the model, no example values
python -m dbdoc --no-sample-values

# show all options
python -m dbdoc --help
```

Open the result in Explorer with `explorer .\db_docs`, or in VS Code with `code .\db_docs` (Markdown preview: `Ctrl+Shift+V`; Mermaid diagrams need a Mermaid preview extension).

| Option | Meaning |
|---|---|
| `--out` | Output folder (default `db_docs`) |
| `--schema` / `--all-schemas` | Which schemas to document (default: the login's default schema, usually `dbo`) |
| `--tables`, `--exclude` | Name patterns, e.g. `dbo.Fact*` |
| `--no-views` | Skip views |
| `--auth` | Entra ID sign-in mode: `browser`, `device`, `cli`, `default` |
| `--provider` | `anthropic`, `azure_openai`, `openai`, `none` |
| `--model` | Model name (for Azure: the deployment name) |
| `--language` | Language of the descriptions |
| `--max-steps` | Tool-call budget per table (default 12) |
| `--sample-rows` | Sample size for profiling (default 5000) |
| `--no-sample-values` | Send only statistics to the model, no example values |
| `--force` | Regenerate even if nothing changed |
| `--verbose` / `-v` | Also print the SQL of every agent query, the number of rows returned and each model step |
| `--quiet` / `-q` | One line per table, plus errors |

## Progress messages

The program prints every step with a timestamp: loading `.env`, signing in to Entra ID (with the account name and token expiry), connecting to the database (server, database, user), listing the tables, and for each table: reading its structure, sampling rows, every agent query with its purpose, what the agent submitted, and the file it saved. Common connection errors come with a hint on what to fix: a wrong server name, a firewall block, a missing database user or a missing ODBC driver.

```
[14:55:03] → Signing in to Microsoft Entra ID (browser mode): a browser window will open ...
[14:55:12] ✓ Signed in as john.smith@company.com, token valid until 15:55
[14:55:13] ✓ Connected to database 'sales' on server 'myserver' as john.smith@company.com
======================================================================
[3/20] Analyzing table dbo.Orders
======================================================================
[14:55:14]     → Sampling up to 5,000 rows to profile the values
[14:55:15]     → Agent (anthropic:claude-sonnet-5-5) is investigating the table
[14:55:21]         · Query 1: Decode the meaning of Status values
[14:55:40]     ✓ Agent submitted documentation: 12 columns, 2 relationships, ...
[14:55:40] ✓ [3/20] dbo.Orders: documented, 5 agent queries in 26s
```

To save the full log to a file while still seeing it on screen:

```powershell
python -m dbdoc --auth browser 2>&1 | Tee-Object -FilePath run.log
```

The ✓ → ✗ symbols display correctly in Windows Terminal and the VS Code terminal. In the old `cmd.exe` console they may show as `?`; the program keeps working.

## Incremental runs and manual notes

- Each table gets a fingerprint of its structure (columns, types, keys). On the next run, unchanged tables are skipped, so rerunning on a large database costs only as much as the tables that changed. An interrupted run (Ctrl+C) picks up where it stopped.
- Everything below the `<!-- MANUAL NOTES ... -->` marker in a table file is kept when the docs are regenerated. The agent reads these notes on the next run and treats them as authoritative. This is the main way to teach it business context, for example "Status 3 = cancelled by the customer" or "TotalNet excludes VAT". After adding a note, run `--tables TableName --force`.
- You also get better descriptions by filling in `MS_Description` in the database itself: the agent reads them, and the documentation stays with the data.

## Security and privacy

- Agent queries go through a validator (`sqlglot`): only a single `SELECT` is allowed. It rejects, among others, `INSERT/UPDATE/DELETE/MERGE`, `SELECT INTO`, DDL, `EXEC`, `OPENROWSET` and `WAITFOR`. Every query has a 60-second timeout, and results are cut to 50 rows.
- The model receives statistics and a few example values per column. For personal data, use `--no-sample-values` or a model in your own tenant (Azure OpenAI). The prompt forbids copying personal data into the documentation, but review the output before sharing it.
- Profiling reads `TOP (n)` rows from every table. On very large tables, lower `--sample-rows` or run outside peak hours.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Selected user account does not exist in tenant 'Microsoft Services'` | Set `AZURE_TENANT_ID` to the tenant of the SQL server. A personal or guest account must first be invited to that tenant. |
| `Named Pipes Provider ... Login timeout expired` | The server can't be reached: check `Server=tcp:<name>.database.windows.net,1433` and test in PowerShell with `Test-NetConnection <name>.database.windows.net -Port 1433` (`TcpTestSucceeded : True` is expected). Corporate networks or VPNs sometimes block port 1433. |
| `Client with IP address ... is not allowed` | Add your IP in the Azure portal: SQL server → Networking → Firewall rules. |
| `Login failed for user '<token-identified principal>'` | Create the user in the database (`CREATE USER ... FROM EXTERNAL PROVIDER`) and grant `db_datareader`. |
| `ModuleNotFoundError: No module named 'azure'` | Activate the virtual environment (`.\.venv\Scripts\Activate.ps1`) and run `python -m pip install -r requirements.txt`. In VS Code, check that the `.venv` interpreter is selected. |
| `Data source name not found and no default driver specified` | The ODBC driver in `Driver={...}` isn't installed: `winget install Microsoft.msodbcsql.18`, then open a new terminal. |
| `running scripts is disabled on this system` | `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` |
| `401 authentication_error: API key is invalid` | Replace the placeholder `ANTHROPIC_API_KEY` with a real key from console.anthropic.com (API access is billed separately from Claude.ai plans), or switch to `DBDOC_PROVIDER=azure_openai`. |
| Edits to `.env` seem ignored | Open a new terminal; `.env` values override variables the terminal already has. |

## Tests

```powershell
python -m pytest -q
```

The tests run offline: they use SQLite instead of Azure SQL and a scripted LLM instead of a real model. They cover the SQL validator, profiling, the agent loop, file generation, incremental runs, keeping manual notes, Entra token handling and the progress messages.

## Code structure

| File | Role |
|---|---|
| `dbdoc/db.py` | Connection, Entra ID sign-in, introspection, profiling, read-only queries |
| `dbdoc/sqlguard.py` | SQL validator |
| `dbdoc/llm.py` | Shared tool-calling loop for Claude and (Azure) OpenAI |
| `dbdoc/agent.py` | Prompt, tool definitions, documenting a table and the database overview |
| `dbdoc/markdown.py` | Markdown files, index, ER diagram, cache |
| `dbdoc/log.py` | Timestamped progress messages |
| `dbdoc/cli.py` | Command line |
