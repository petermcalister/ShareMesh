# Env-Config Centralisation Plan

> **For execution:** Use `/run-agent` to implement this plan.

## Overview

Replace the current scatter of `os.environ.get(...)` + `(ENV_VAR, default)` tuples + `env_util.get(...)` typed accessors with a single hand-written Pydantic `Config` tree backed by a committed `src/utils/env_config.json` file (colocated with `env_util.py`; structure + `${VAR:default}` refs) and a slimmed `.env.local` (gitignored secrets + per-machine paths only). Delete `src/database/registry.py` entirely; split its responsibilities between `env_util.Config` (connection params) and a new `src/database/migrate.py` (folder-scan DB discovery). Move runtime connection logic into `src/utils/db_util.py`; rename `src/database/engine.py` → `migration_engine.py` (filesystem move — `/checkin` detects the rename) to reflect that it's now migration-domain only. Rewrite `.claude/rules/environment.md` and extend the structural checker to enforce the new pattern as a pre-push gate.

**Cross-domain test routing:** All pytest + behave runs target the harness's embedded Postgres (port 5434) via every domain's `test` block referencing `shared.postgres_embedded`. Only SQLite-engined cases bypass this (none exist in the current cowork-native registered databases; the SQLiteEngine in migration_engine.py is kept dormant for engine-agnostic future-proofing).

**Domain taxonomy:** Subsystem domains (memory, finance, web_agent, job_hunt, whatsapp, pg_dump) match the existing `src/<domain>/` structure. Cross-cutting blocks live alongside: `api` (external-service keys — embeddings.gemini_key + agent.anthropic_key), `identity` (personal emails / JIDs / repo URL), `migration` (sqlite_db_path for dormant SQLiteEngine).

## Feature List

See: `.claude/plans/env-config-centralisation-plan-features.json`

Total features: 23
Passing: 0

## Task Groups

| Batch | Features | Theme |
|-------|----------|-------|
| 1 | F001–F006 | Config scaffolding — env_config.json, Pydantic models, loader, ${VAR:default} resolver, EXECUTION_ENV auto-selector, tests |
| 2 | F007–F009 | DB infrastructure split — new db_util.get_connection(), rename engine.py → migration_engine.py, new migrate.py with folder-scan discovery |
| 3 | F010–F012 | Registry deletion — redirect src/+tools/ callers; redirect tests/+features/ callers; delete registry.py |
| 4 | F013–F018 | Runtime consumer migrations — memory, finance UI (heals POSTGRES_* duplication), whatsapp/forms, pg_dump/job_hunt, web_agent feature flags + harness EXECUTION_ENV, tools/* + .env.example cleanup |
| 5 | F019–F021 | Rule + enforcement — rewrite environment.md, extend structural checker with OS-var allowlist, activate as pre-push gate |
| 6 | F022–F023 | Cleanup — drop deprecation shims, update CLAUDE.md, memory note, archive plan; final Guido delta review |

## Files to Modify

| File | Change |
|------|--------|
| `src/utils/env_config.json` | **NEW** — committed config tree at the SAME directory as env_util.py. Shape: `shared.postgres` (EEL) + `shared.postgres_embedded` (port 5434) blocks; per-subsystem default/test blocks (memory, finance, web_agent, job_hunt, whatsapp, pg_dump); cross-cutting blocks (`api.embeddings.gemini_key`, `api.agent.anthropic_key`, `identity.*`, `migration.sqlite_db_path`); ${VAR:default} refs throughout. **All `test` blocks `$shared: "postgres_embedded"`** — pytest + behave both target embedded_pg cross-domain. |
| `src/utils/env_util.py` | **Rewrite** — Pydantic Config models, load_config(), reload_config(), test_seam(), `load_dot_env()` (renamed from `_load_env_local`; called as preflight by load_config to populate os.environ from .env.local), $shared merger, ${VAR:default} lazy resolver, EXECUTION_ENV auto-selector. The 12 legacy typed accessors (personal_gmail/etc) get retired in F022 by refactoring their callers to `cfg.<domain>.<key>`. ~400 lines (up from 160). |
| `src/utils/db_util.py` | **Extend** — add get_connection(name) that builds psycopg2 conn from env_util.Config with CREATE SCHEMA + SET search_path + pgvector adapter. Keep existing fetch_*_dict helpers. ~200 lines (up from 100). |
| `src/database/engine.py` | **Rename → migration_engine.py via FILESYSTEM MOVE** (NOT `git mv` — that's a git write forbidden by .claude/rules/git-discipline.md; the user's `/checkin` detects the rename via git content-similarity). Strip runtime connect() paths (moved to db_util). Keep Protocol + locking + script execution + SQLite stub for future-proofing. SQLiteEngine.connect() reads `cfg.migration.sqlite_db_path` (via env_util.Config), not `os.environ.get('SQLITE_DB_PATH', ...)`. |
| `src/database/migrate.py` | **NEW** — discover_databases() via folder scan + get_migrations_dir(name). Replaces the migrations_dir lookup in registry.py. |
| `src/database/registry.py` | **DELETE** (after F010 redirects callers + F012 verifies no orphans). |
| `src/database/cli.py` | `_connect_engine(engine)` becomes "build conn via db_util.get_connection + engine via migration_engine"; `_get_target_databases` uses migrate.discover_databases(). |
| `src/database/runner.py` | Update import: `from src.database.engine import` → `from src.database.migration_engine import`. No logic change. |
| `src/web_agent/recipes.py` | Replace `resolve_database('web_agent')` with `db_util.get_connection('web_agent')`. |
| `src/web_agent/sessions.py` | Migrate 3 OPERATIONAL feature flags (WEB_AGENT_NOTIFY_THRESHOLD_SECS, WEB_AGENT_SKIP_WHATSAPP, WEB_AGENT_SKIP_JUDGE) to `cfg.web_agent.<key>` reads via env_util.Config — these guard real production code paths so they belong in env_config.json under web_agent.default.*, NOT in test_seam(). Drop the underscore-prefixed module constants. |
| `src/whatsapp/bridge_client.py` | Replace `BRIDGE_URL = os.environ.get(...)` constant with a `get_bridge_url()` function reading `cfg.whatsapp.bridge_url`. |
| `src/forms/form_generator.py` | Replace hardcoded `BRIDGE_URL = "http://localhost:8081"` constant by IMPORTING `from src.whatsapp.bridge_client import get_bridge_url` and calling it at use sites (single source of truth — no duplicate accessor). Drops the structural-checker exemption. |
| `src/pg_dump/metadata.py` | PG_DUMP_SCHEMAS env-read → `cfg.pg_dump.schemas` (comma-split). |
| `src/pg_dump/{dump,restore,onedrive}.py` | Any `env_util.get()` calls → `cfg.pg_dump.<key>`. |
| `src/memory/{wa_poll,weekly_scan}.py` | `os.environ.get('WHATSAPP_COWORK_PA_JID', 'cowork-pa')` → `cfg.whatsapp.cowork_pa_jid`. |
| `src/memory/embeddings.py` | `env_util.google_gemini_api_key()` → `cfg.api.embeddings.gemini_key` (api is the cross-cutting domain for external-service keys). |
| `src/finance_ui/db/connection.py` | `env_util.get('POSTGRES_*')` → `cfg.finance.host/port/dbname/user/password` (heals POSTGRES_* duplication). |
| `src/finance_ui/pages/settings.py` | Same swap. |
| `src/finance_ui/agent/server.py` | POSTGRES_* → `cfg.finance.<key>`; ANTHROPIC_API_KEY → `cfg.api.agent.anthropic_key`. |
| `tools/pg_dump/run.py` | 3 `resolve_database('finance')` calls → `db_util.get_connection('finance')`. |
| `tools/web_agent/run.py` | 1 `resolve_database('web_agent')` call → `db_util.get_connection('web_agent')`. |
| `tools/{finance,library,log_maintenance,phoenix_eval}/run.py` | Any `env_util.get()` reads → Config accessors. |
| `tools/harness/postgres_isolation/per_hook_memory_schema.py` | `embedded_pg_env_overlay()` adds `EXECUTION_ENV=test` to returned dict. |
| `tools/harness/postgres_isolation/per_hook_web_agent_schema.py` | Same addition. |
| `tools/harness/hook_runner/{behave,pytest}.py` | If they construct env overlays directly (not through embedded_pg_env_overlay), set EXECUTION_ENV=test there too. |
| `tools/harness/structural_checker/_check_env_config.py` | Extend with the new "no opaque os.environ.get for config-shaped keys" rule + OS-var allowlist; drop src/forms/form_generator.py from _EXEMPT_PATHS once F015 migrates it. |
| `.env.local` | **Per-machine cleanup** — reduce to secrets (passwords, API keys, JIDs, personal emails) + per-machine paths (ONEDRIVE_PG_DUMPS_DIR). User-action; documented in plan, not a code change. |
| `.env.example` | Reduced to secrets + per-machine placeholders only (~25 lines, down from ~70). Drop POSTGRES_*, DB_*, MEMORY_DB_*, WEB_AGENT_DB_*, JOB_HUNT_DB_*, FINANCE_SCHEMA, MEMORY_SCHEMA, WHATSAPP_BRIDGE_URL, PG_CONTAINER_NAME defaults — they live in env_config.json now. |
| `.claude/rules/environment.md` | **Rewrite** — new model, ${VAR:default} grammar, OS-var allowlist, test_seam() documented escape hatch. |
| `tests/test_env_util_v2.py` | **NEW** — unit tests for loader, resolver, auto-selector, test_seam, round-trip parity. |
| `tests/test_db_util.py` | Extend with get_connection() unit tests + EXECUTION_ENV routing assertion. |
| `tests/test_database.py` | Redirect 16 mock points from `mock.patch('src.database.cli.resolve_database', ...)` to the new accessor mocks. |
| `tests/test_dev_harness_memory_isolation.py` | Update to assert `cfg.memory.host` (with EXECUTION_ENV=test) routes to embedded_pg, instead of asserting via resolve_database. |
| `tests/test_pg_dump.py` | Update the resolve_database stub site (line 896). |
| `tests/test_job_hunt_*.py` | 3 files — swap resolve_database imports for db_util.get_connection. |
| `tests/test_memory.py` | Update docstring + any direct env reads. |
| `tests/test_harness_*.py` | Update assertions about MEMORY_SCHEMA / WEB_AGENT_SCHEMA env-overlay pinning to also assert EXECUTION_ENV. |
| `tests/test_harness_structural_checker_env_config.py` | Extend with unit tests for the new rule + allowlist. |
| `tests/conftest.py` | Update docstring + any direct env reads. |
| `features/environment.py` | (a) `env_util.get('GMAIL_ADDRESS')` → `cfg.identity.gmail_address` (stashed on `context.gmail_address` as today). (b) `env_util.get('OUTLOOK_ADDRESS')` → `cfg.identity.outlook_address` (stashed on `context.outlook_address`). (c) Update the stale `resolve_database('memory') in embedded_pg` comment at line 116 to reference `cfg.memory.envs.test` + EXECUTION_ENV. (d) Update the `env_util.pg_dump_password` comment at line 568. (e) Any other `env_util.get()` reads → Config accessors. Covered by F011's acceptance. |
| `features/steps/*` | 6+ step-definition files: swap resolve_database for db_util.get_connection. |
| `CLAUDE.md` | Update the "Memory System" + "Database Migration Framework" + new "Environment Configuration" section to describe env_config.json + Config + cfg.<domain>.<key> + the discipline rule. Cross-ref the new rules. |

## Test Strategy

- **Unit tests (pytest):**
  - `tests/test_env_util_v2.py` — load_config(), $shared merger, ${VAR:default} resolver, EXECUTION_ENV auto-selector, reload_config(), test_seam(), Config-vs-JSON schema validation, round-trip parity with .env.example
  - `tests/test_db_util.py` (extend) — get_connection() returns valid psycopg2 conn with search_path set; EXECUTION_ENV=test routes to embedded_pg
  - `tests/test_database.py` (update) — 16 mock points redirect to new accessors; existing migration behaviour unchanged
  - `tests/test_dev_harness_memory_isolation.py` (update) — assert cfg.memory.host (with EXECUTION_ENV=test) routes to embedded_pg
  - `tests/test_harness_structural_checker_env_config.py` (extend) — new rule fires on synthetic violation; allowlist behaviour
- **Integration tests (behave):**
  - Existing behave suite must pass unchanged after F011's step-definition redirects
  - No new .feature file needed — this is an architectural refactor, not new user-facing behaviour. Existing features (database_migrations.feature, job_hunt_*.feature, web_agent_*.feature, pg_dump_sync.feature) provide end-to-end coverage of the refactored surfaces.
- **Verification:**
  - After F012: `grep -r 'from src.database.registry\|import.*registry' src/ tests/ tools/ features/` returns no matches
  - After F018: `.env.example` is ~25 lines, all DB_*/POSTGRES_*/MEMORY_DB_*/WEB_AGENT_DB_*/JOB_HUNT_DB_*/FINANCE_SCHEMA/MEMORY_SCHEMA defaults gone
  - After F021: `poetry run dev-structure` emits 0 env-config violations; planting a `os.environ.get('DB_HOST', ...)` in src/memory/db.py fails the gate
- **Test isolation:**
  - env_util.Config is a process-level singleton. Tests that monkeypatch env vars at module level must call `env_util.reload_config()` after the patch to see the new value. The lazy ${VAR} resolver mitigates this for refs (re-reads on each access), but eager static values are baked at load time. This is a documented cost — call it out in environment.md and the test file docstrings.
  - Per-process schema isolation (the cowork_memory_<suffix>_<pid> + cowork_web_agent_<suffix>_<pid> pattern) is unchanged. The harness still sets MEMORY_SCHEMA / WEB_AGENT_SCHEMA per subprocess; env_util.Config picks them up via the ${MEMORY_SCHEMA} / ${WEB_AGENT_SCHEMA} refs in the test-env blocks of env_config.json.

## Behave Feature

**N/A** — this is an architectural refactor with no new user-facing behaviour. Existing behave coverage (`features/database_migrations.feature`, `features/job_hunt_*.feature`, `features/web_agent_*.feature`, `features/pg_dump_sync.feature`) exercises the refactored surfaces end-to-end. F011 ensures those scenarios continue passing.

## DB Task Discipline

**N/A** — this plan does not touch database schema, migrations, or stored data. The `src/database/migrations/<dbname>/` SQL files stay byte-identical. No new migrations are added; no DDL is run. The work is purely Python module restructuring + config-file introduction + rule update.

## Debug & Operating Notes

### The two-naming-schemes wrinkle that triggered this plan

Today the same database connection params live under two parallel env-var name sets: `DB_HOST/PORT/NAME/USER/PASSWORD` (read by `src/database/registry.py` + `src/database/engine.py`) and `POSTGRES_HOST/PORT/DB/USER/PASSWORD` (read by `src/finance_ui/db/connection.py` + `src/finance_ui/pages/settings.py`). They MUST be set to the same values in `.env.local` or finance UI breaks while engine still works. F014 collapses this by routing both code paths through `cfg.finance.<key>` which resolves via `shared.postgres`.

### Resolution timing

`load_config()` is **eager** for static JSON values and **lazy** for `${VAR}` refs. This means:
- `cfg.memory.envs.default.port` (defined as `"5432"`) is baked at first `load_config()` call
- `cfg.memory.envs.default.password` (defined as `"${DB_PASSWORD:postgres}"`) is resolved from `os.environ` on each attribute access
- A test that does `monkeypatch.setenv("DB_PASSWORD", "x")` then reads `cfg.memory.envs.default.password` sees `"x"` immediately — no `reload_config()` needed (because ${} refs are lazy)
- A test that mutates the JSON file on disk must call `reload_config()` to see the change

### Cross-domain test routing — all roads to embedded_pg

Every domain's `test` block uses `$shared: "postgres_embedded"`. That includes `finance.test`, not just `memory.test` / `web_agent.test` / `job_hunt.test`. The harness's embedded Postgres (port 5434, db `postgres`, user/password `postgres`/`postgres`) is the single Postgres target for pytest + behave runs across the entire cowork-native surface. Production runs (EXECUTION_ENV unset) route through `default` blocks which `$shared: "postgres"` → the EEL container at port 5432. The 3-tuple `(MEMORY_DB_HOST, DB_HOST, default)` fallback pattern that the old registry used to selectively redirect ONLY memory disappears — it's no longer needed because the new model unifies all test routing on embedded_pg.

SQLite-engined cases bypass this (none in current cowork-native registered DBs; SQLiteEngine in migration_engine.py is dormant). If a future domain adopts SQLite, its test block would NOT use `$shared: "postgres_embedded"` — it would inherit from a new `shared.sqlite_test` block (or have its own bespoke test config).

### Harness env-var flow (unchanged shape, new EXECUTION_ENV addition)

The harness writes `MEMORY_DB_*` + `MEMORY_SCHEMA` (and `WEB_AGENT_DB_*` + `WEB_AGENT_SCHEMA`) into subprocess env via `tools/harness/postgres_isolation/per_hook_memory_schema.py:embedded_pg_env_overlay()` before spawning behave/pytest. F017 adds `EXECUTION_ENV=test` to that overlay. Then:
- Subprocess starts → imports modules → first attribute access on `cfg.memory.<key>` triggers `load_config()`
- `load_config()`'s preflight runs `load_dot_env()` (loads .env.local into os.environ idempotently)
- EXECUTION_ENV=test → auto-selector returns `cfg.memory.envs.test.<key>` block
- `cfg.memory.envs.test.host` resolves (via `$shared: "postgres_embedded"` + `${MEMORY_DB_HOST:127.0.0.1}`) to embedded_pg host
- `cfg.memory.envs.test.schema` = `"${MEMORY_SCHEMA}"` (no default — missing in test env = explicit error, which is the desired safety against accidentally landing tests in production schemas)

### Behave schema names — consumer composes the dynamic part

The harness's `MEMORY_SCHEMA` and `WEB_AGENT_SCHEMA` are fully-qualified names (e.g., `cowork_memory_group1_12345`) — declarable as `${MEMORY_SCHEMA}` in JSON, resolved at attribute access. Finance behave uses `BEHAVE_SCHEMA_PREFIX` (e.g., `group1_<pid>`) that `features/environment.py:before_feature()` composes into `f"{prefix}_{feature_slug}"` per scenario. With Pete's "hard-code actual schema/test group names in behave" principle:
- `finance.test.schema` declares the per-process schema with a fixed prefix, e.g., `"${FINANCE_SCHEMA:cowork_hook_default}"` (the harness sets FINANCE_SCHEMA per-process, like MEMORY_SCHEMA).
- `features/environment.py:before_feature()` reads the base via `cfg.finance.envs.test.schema` (or `cfg.finance.schema` under EXECUTION_ENV=test) and composes the per-feature suffix at runtime — the static part stays in JSON, the dynamic part composes in consumer code.

This is Pete's principle in action: "if you're worried about load_config not handling dynamic outputs, just have the consuming code construct/append the suffix to the static part at run time."

### The `vendored vs cowork-native` split inside `src/web_agent/`

Most of `src/web_agent/` is browser-use vendored code (header reads "Configuration system for browser-use"). It reads ~20 env vars via `os.getenv` for its own config — `OPENAI_API_KEY`, `BROWSER_USE_*`, `LMNR_*`, `PLAYWRIGHT_BROWSERS_PATH`, `XDG_*`, `IN_DOCKER`, `WIN_FONT_DIR`. These STAY untouched. Browser-use's own `ANTHROPIC_API_KEY` read in `config.py` is also untouched (vendored); cowork's finance agent has its own ANTHROPIC_API_KEY read in `src/finance_ui/agent/server.py` which F014 migrates to `cfg.api.agent.anthropic_key`.

The cowork-native additions inside `src/web_agent/sessions.py` (3 OPERATIONAL feature flags: `WEB_AGENT_NOTIFY_THRESHOLD_SECS`, `WEB_AGENT_SKIP_WHATSAPP`, `WEB_AGENT_SKIP_JUDGE`) and `src/web_agent/recipes.py` (resolve_database call) ARE migrated. The 3 flags go into env_config.json under `web_agent.default.*` (they guard real production code paths — they're operational config, not test seams). `src/web_agent/migrate_from_sqlite.py:179` reads `WEB_AGENT_DB_PATH` for the legacy SQLite one-shot importer; this is the rare case where `env_util.test_seam('WEB_AGENT_DB_PATH')` is appropriate — a one-shot migration script that won't have a permanent Config slot.

F020's structural-checker path exemption for `src/web_agent/` excludes the vendored surface (`config.py`, `browser/*`, `observability.py`, `utils.py`, `skills/service.py`, `_chromium_finder.py`) but NOT `sessions.py` / `recipes.py` (which were migrated in F010 + F017). The simplest exemption shape: list the vendored file paths explicitly.

### registry.py deletion blast radius

`resolve_database` is called or mocked in ~25 files across the tree. F010 covers the src/+tools/ side, F011 covers tests/+features/, F012 verifies orphans. The single test file `tests/test_database.py` has 16 mock-patch sites at `src.database.cli.resolve_database` — these need to be re-pointed at the new accessor (probably `src.utils.db_util.get_connection` and `src.database.migrate.get_migrations_dir`). Each mock site is 1-3 lines, mechanical.

### SQLITE_DB_PATH routes through env_util too

The dormant `SQLiteEngine` in `migration_engine.py` (preserved for engine-agnostic future-proofing per Pete's deploy-vs-runtime principle) historically read `os.environ.get("SQLITE_DB_PATH", "database.db")` directly in its `connect()`. F008 swaps this to `cfg.migration.sqlite_db_path` (declared as `"${SQLITE_DB_PATH:database.db}"` in env_config.json). Same default behaviour, but now flows through the centralised Config so the structural checker doesn't flag it.

### Things this plan deliberately doesn't do

- Does NOT migrate `src/datamart/`, `src/agent_pool/`, `src/codeburn/` — they have their own config worlds (`src/datamart/utils/envUtil.py` is a 1,750-line aep_datamart import with its own EnvConfig).
- Does NOT touch the migration framework's SQL files (`src/database/migrations/<dbname>/*.sql`).
- Does NOT alter the browser-use vendored surface in `src/web_agent/` (config.py, browser/*, observability.py, utils.py, skills/service.py, _chromium_finder.py).
- Does NOT change the dev-harness's per-process schema isolation behaviour — only adds EXECUTION_ENV=test to the existing overlay.
- Does NOT introduce a new dependency (Pydantic is already a transitive via pydantic-ai, datamart, finance_ui).
- Does NOT use `git mv` for the engine.py → migration_engine.py rename — that would violate git-discipline. Filesystem move; `/checkin` detects the rename via content-similarity.

## Incremental Order

Strict sequential within batches; batches sequential too (F007 needs F002+F003 done; F010 needs F007+F009; F012 needs F010+F011; etc.).

1. F001 — env_config.json (no Python yet — just the file)
2. F002 — Pydantic Config models
3. F003 — load_config() + $shared merger
4. F004 — ${VAR:default} lazy resolver + EXECUTION_ENV auto-selector
5. F005 — test_seam() + reload_config()
6. F006 — unit tests for the whole loader
7. F007 — db_util.get_connection() (uses F003-F004 Config)
8. F008 — rename engine.py → migration_engine.py + strip runtime parts
9. F009 — new migrate.py with folder-scan discovery
10. F010 — redirect src/+tools/ callers (registry.py still exists with shim)
11. F011 — redirect tests/+features/ callers
12. F012 — delete registry.py
13. F013 — migrate src/memory/
14. F014 — migrate src/finance_ui/ (heals POSTGRES_*)
15. F015 — migrate src/whatsapp/ + src/forms/
16. F016 — migrate src/pg_dump/ + src/job_hunt/
17. F017 — migrate src/web_agent/ feature flags + harness EXECUTION_ENV=test
18. F018 — migrate tools/* + reduce .env.example
19. F019 — rewrite environment.md
20. F020 — extend structural checker
21. F021 — activate as pre-push gate; fix stragglers
22. F022 — drop deprecation shims + update CLAUDE.md + archive plan
23. F023 — Guido delta review

## Skill Activation Ledger

| Skill | Expected use | Phase |
|---|---|---|
| `pydantic` | Authoring the hand-written Config Pydantic models (F002) — validate the shape against env_config.json | F002, F003 |
| `pytest-creator` | tests/test_env_util_v2.py, tests/test_db_util.py extensions, tests/test_harness_structural_checker_env_config.py extensions | F006, F007, F020 |
| `debug-skill` | Any regression that surfaces after F010-F012 (registry deletion) — markers in files/logs/app.log will be the fastest signal | F010-F012, F017 |
| `behave-creator` | NOT expected — no new .feature file in this plan. Existing scenarios in features/database_migrations.feature, features/job_hunt_*.feature, features/web_agent_*.feature, features/pg_dump_sync.feature already cover the refactored surfaces. | N/A |

## Code Review

The FINAL feature F023 is a Guido code review (Delta mode against this branch's diff). When F001-F022 all `passes: true`, the implementing agent (or `/run-agent`) spawns the `guido-review` subagent, reviews the full diff, and:

- Fixes every HIGH-severity finding, OR records explicit acceptance rationale in this plan markdown under a new `## Accepted findings` section
- Appends the review summary to `claude-progress.txt`
- Only then flips F023's `passes` to `true`

The plan is NOT complete until F023 passes.

## Acceptance Criteria

All features F001-F023 in `env-config-centralisation-plan-features.json` have `passes: true`, including:

- `src/utils/env_config.json` exists (colocated with env_util.py) and validates against the Pydantic Config class
- `src/database/registry.py` no longer exists; grep across the repo finds no orphan references
- `cfg.<domain>.<key>` (via env_util.load_config()) is the single read path for cowork-native config
- `db_util.get_connection(name)` is the single connection entry point for runtime code
- `.claude/rules/environment.md` reflects the new model and the OS-var allowlist
- `tools/harness/structural_checker/_check_env_config.py` flags hand-rolled `os.environ.get` for config-shaped keys in cowork-native src/ paths
- Pre-push hook (`poetry run dev-structure`) emits 0 env-config violations against the final tree
- `.env.example` is ~25 lines (down from ~70)
- POSTGRES_*/DB_* duplication is gone — both finance UI and engine path read from `cfg.finance.<key>` → `shared.postgres`
- All existing pytest + behave tests pass
- Guido delta review's HIGH-severity findings are either fixed or accepted-with-rationale in this plan

## Accepted findings (Guido delta review, 2026-05-21)

### H1 — `src.database.migrate` module shadowed by function — FIXED
Dropped the `migrate` function re-export from `src/database/__init__.py`. The
function stays reachable via `from src.database.runner import migrate` (the
only legitimate consumer is the migration CLI internally, which already uses
that path). The submodule `src.database.migrate` is now unambiguously the
module.

### H2 — `finance.test.schema` literal default `cowork_hook_default` — ACCEPTED
Finance test isolation works differently from memory/web_agent/job_hunt:
- memory/web_agent: harness sets `MEMORY_SCHEMA` / `WEB_AGENT_SCHEMA` per
  pytest+behave subprocess via `embedded_pg_env_overlay()`. The Config tree
  routes via the test block.
- finance: per-feature schema isolation lives in `features/environment.py:
  before_feature()` which composes `context.feature_schema = f"{BEHAVE_SCHEMA_PREFIX}_{feature_slug}"`
  and runs `SET search_path TO {context.feature_schema}` inside the conn,
  bypassing the cfg.finance.envs.test.schema_ value entirely.
- The `cowork_hook_default` default in env_config.json:24 is a backstop for
  the rare case where a pytest finance consumer hits `cfg.finance` under
  `EXECUTION_ENV=test` without going through behave's per-feature overlay.
  An unmigrated schema name will surface as a SQL error at first table
  access — failure-loud, not silent.

Made `cfg.finance.envs.test` not the production path for finance; the real
finance schema seam is `context.feature_schema` in `features/environment.py`.

### H3 — `_check_env_config.py` SQL-keyword list missing `WHERE`/`LIMIT` — FIXED
Extended the SQL-context keyword list in `_check_hardcoded_schemas` to
include `WHERE|LIMIT|WITH|RETURNING|ON`. This surfaces the real bug at
`src/database/runner.py:322` (baseline detection hardcoded `'public'`),
which was fixed in the same commit by switching to
`WHERE table_schema = current_schema()` so the check follows the active
search_path set by `db_util.get_connection`.
