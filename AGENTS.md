# AGENTS.md

Rules for Codex agents working in this repository.

## Public repository status

This is the **public provisional repository** for Coddy. It has a deliberately independent, sanitized history and is not yet the definitive `BraFurries/BraFurries-Discord` repository. Production deployment remains disabled and will be reviewed separately before any future cutover.

- **Never import private legacy history into this repository.** The historical source has distinct Git objects that may include Discord/OpenAI/Telegram credentials, a SQLite database, and personal chat/image data. Sanitizing a tree does not prove provider-side token revocation.
- Keep this repository's history independent: no old `.git`, refs, tags, logs, workflow artifacts, tracked databases or private media snapshots. Do not fork, mirror or import the legacy history.
- The tracked root file named `discord` is a **SQLite database**, not a Python package; it must never be included in the public export. Likewise exclude the old `pipeline/1` PM2 configuration and other operational/legacy runtime artifacts from the new public copy unless individually justified.
- The legacy `settings.py` contains old guild/admin/role/channel settings. Their values must be environment-based or absent, not real snowflakes in public source. Runtime code still imports `BOT_NAME`, `DISCORD_BOT_PREFIX` and `DISCORD_INTENTS`: preserve these interfaces.
- Operational IDs previously hardcoded in `core/routine_functions.py`, `core/service_commands.py` and `message_services/message_moderation/moderation_functions.py` now require explicit environment configuration; see `docs/publication-runtime-config.md`. **Do not deploy** that code until the authorized runtime environment is prepared or all affected behavior is migrated to API/guild configuration. Treat existing public bot/emoji IDs separately from private identities.
- Keep the API as business/admin authority and the Database as schema authority. Coddy is the multi-network runtime; never bypass guild authorization or Community tenant isolation during this move.
- The new public repository must contain **only safe PR/push CI** initially. Exclude `.github/workflows/prod-deploy.yaml` and all production-specific runner, environment, GHCR and deployment artifacts. Run unit tests without Discord, OpenAI, Telegram or a real database.
- Leave all production workflows in old private repositories disabled. This public repository has safe CI only; do not create/use production secrets, access runners, change GHCR access, deploy, run runtime migrations/DDL/DML, rotate credentials or rename repositories without separate authorization.
- Run Gitleaks against the exported tree and **every ref of the brand-new repository**, audit binary files and tracked/untracked boundaries, then prove a single-root history and obtain a real CI green run before any authorized rename/cutover.
- Preserve the old private repository untouched. Do not erase history, prune branches or formally archive/delete the source without separate approval.

## Core Rules

- Do not access production infrastructure, production MariaDB, production credentials, or production deployment systems.
- Do not push directly to `main`.
- The production deploy workflow is currently manually disabled for repository migration; if separately re-enabled in the future, eligible pushes to `main` may deploy; migrations, the migration runner, runtime schema-authority files, and production-pipeline changes require the manual `Deploy em Produção` `workflow_dispatch` escape hatch. A manual run still must start from the current `main` head.
- Do not start, trigger, simulate, or operate production deploys.
- Do not run production Docker, Docker Compose, or PM2 commands.
- Do not read, print, copy, expose, or commit secret values.
- Do not alter source code, config, workflows, dependencies, or docs outside the user's requested scope.

See `ARCHITECTURE.md#known-inconsistencies` before making assumptions about runtime versions, migrations, entrypoints, tests, or legacy behavior.

## Repository Shape

This is a Python Discord bot for the BraFurries community.

Use the existing architecture:

- `main.py` is the process entrypoint.
- `message_services/discord_service.py` creates and runs the Discord bot, loads cogs, syncs slash commands, registers global Discord events, and manages optional startup/shutdown callbacks.
- `cogs/` contains Discord command modules. Every `.py` file in this directory is dynamically loaded.
- `core/` contains shared domain logic, database access, Discord views, event helpers, XP logic, routines, notifications, and integrations.
- `schemas/` contains simple models, enums, and `Literal` types used by commands.
- `tests/` contains unit tests for isolated logic.

When adding commands, place them in the appropriate cog and expose `async def setup(bot)` with `await bot.add_cog(...)`.

## Implementation Conventions

Follow the style of the file being changed. The codebase mixes legacy and newer Python patterns; do not normalize style through broad refactors unless explicitly requested.

Prefer:

- Slash commands via `discord.app_commands`.
- Existing helpers in `core/` instead of duplicating behavior.
- `ctx.response.defer()` plus `ctx.followup.send(...)` for longer command work.
- `ephemeral=True` for permission errors, sensitive messages, and admin-only feedback.
- Portuguese for user-facing Discord text, matching nearby commands.
- Type hints where surrounding code already uses them.
- Focused pure functions in `core/` when behavior can be tested without Discord or database side effects.

Do not:

- Break dynamic cog loading.
- Add unrelated formatting churn.
- Replace existing permission logic with generic checks without reviewing the current command behavior.
- Remove compatibility aliases or legacy behavior unless explicitly requested.
- Remove or rename existing tables, columns, or structures without explicit user request.

## Safe Validation

Prefer validations that do not initialize the full bot or connect to external services:

```bash
python -m pytest
```

Use static analysis, targeted unit tests, and isolated imports where possible.

Dependency installation, when needed:

```bash
pip install -r requirements.txt
```

Do not use `python main.py` as a default validation step. Importing or starting the application can create connection pools, initialize schema, connect to Discord, access MariaDB, call OpenAI, call Telegram, call Google Calendar, start HTTP services, or cause real effects in Discord servers.

Only run the full bot when explicitly requested and after confirming all of the following:

- credentials are development-only;
- the configured database is not production;
- no production Discord guild will be affected;
- external integrations are configured safely;
- no production resource will be used.

## Database Rules

Production uses MariaDB, accessed through MySQL-compatible drivers and SQL syntax.

Database access is centralized in `core/database.py`. Use existing helpers:

- `pooled_connection()` for synchronous database operations;
- `async_pooled_connection()`, `async_fetchall()`, `async_execute()`, and existing async helpers for async code.

Agents must never access production MariaDB or use production database credentials.

Do not execute schema changes silently. Do not introduce new automatic schema-change mechanisms in `core/database.py` without explicitly informing the maintainer.

When a feature requires schema changes, report:

- required SQL;
- affected tables, columns, and indexes;
- impact on existing data;
- backward compatibility;
- recommended deployment order;
- whether the schema change can run before the code update;
- operational risks;
- whether the project already performs equivalent runtime initialization.

## Environment and Secrets

Production environment variables are not stored in the repository. In production, the GitHub Environment secret `PROD_ENV_FILE` is passed through stdin to the root-owned `update-brafurries-coddy-env` wrapper. After strict validation, it is staged at `/etc/brafurries/coddy/.env.pending` and promoted to `/etc/brafurries/coddy/.env` only as part of deployment. The active file is root-owned and is not part of the repository.

Agents normally do not have access to real production variables.

A local `.env` may exist, but it must not be assumed safe or development-only. It may contain credentials equal or equivalent to production.

Rules:

- Never read, print, copy, expose, or commit values from `.env`.
- Never try to recover or reconstruct production values.
- Never assume a local `.env` is safe.
- Never use the existence of `.env` as authorization to run the bot or access services.
- Never access, print, copy, or stage `PROD_ENV_FILE`.
- Infer variable names from source code and safe documentation.
- Use placeholders or `.env.example` if one exists.
- If validation depends on real credentials or a specific environment, say so instead of using existing credentials.

Treat these as sensitive/local-only:

- `.env`
- `.venv/`, `venv/`
- `credentials.json`
- `token.json`
- `.secrets/`
- OpenAI encryption keys
- Discord, Telegram, Google, OpenAI, database, and deploy credentials

## Authorization and Runtime Safety

Commands use a mix of:

- Discord permission decorators;
- manual `administrator`, `manage_guild`, `manage_roles`, and `manage_channels` checks;
- staff roles from database configuration;
- server owner checks for security whitelist actions;
- some hardcoded IDs.

Preserve existing authorization checks unless the task explicitly changes them. For moderation, security, roles, backup, portaria, XP, economy, VIP, and admin commands, review the relevant cog and called `core/` helpers before changing behavior.

## External Integrations Requiring Care

The project integrates with:

- Discord via `discord.py`;
- MariaDB through MySQL-compatible drivers;
- OpenAI Responses API;
- Telegram Bot API;
- Google Calendar API;
- DISBOARD bump handling;
- optional HTTP status API.

Do not call external production services unless explicitly requested and confirmed safe. The shared production VM uses Docker Engine and Docker Compose for Coddy; the BRFD runner has no direct Docker access and deployment uses narrowly scoped, sudo-authorized root wrappers. Do not run Docker directly in production, alter the firewall or MariaDB, make host-wide changes, or start PM2 while the Coddy Docker container is active. PM2 remains only as a temporary legacy rollback path and must never run concurrently with the Docker runtime using the same `DISCORD_TOKEN`.

## Git Rules

- Check `git status` before editing.
- Work on a feature branch when changes are requested.
- Do not push directly to `main`.
- The production workflow is currently disabled; once separately re-enabled, eligible pushes or merges to `main` may deploy after tests, image build, and safety gates; production-sensitive changes require a manual dispatch from the current `main` head.
- Do not trigger production deploys.
- Do not operate production Docker, Docker Compose, PM2, or the deployment wrappers.
- Do not revert user changes or unrelated changes.
- Existing Codex branches commonly use `codex/...`; follow that pattern unless instructed otherwise.
- No strict commit message convention is evident; existing commits mix Portuguese and English.
