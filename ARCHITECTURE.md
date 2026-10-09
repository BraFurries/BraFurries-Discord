# ARCHITECTURE.md

## Purpose

BraFurries-Discord is a Python Discord bot for the BraFurries community. The bot is named Coddy and provides Discord slash commands, event listeners, background routines, and integrations for community management and engagement.

Major feature areas include member registration, birthdays, community event listings, XP and voice XP, economy and store flows, VIP roles, moderation, forms, backups, reports, security monitoring, DISBOARD bumps, optional OpenAI features, and an HTTP status API.

## Stack

The project is Python-based. The current production image is built from `Dockerfile`, uses `python:3.12-slim`, installs the dependencies pinned in `requirements.txt`, and starts `python main.py`.

Key dependencies include `discord.py`, `aiohttp`, `mysql-connector-python`, `PyMySQL`, `aiomysql`, `openai`, `cryptography`, `pydantic`, and `python-dotenv`.

Production persistence is MariaDB, accessed through MySQL-compatible drivers and SQL syntax.

### Current Runtime

Coddy runs in production in Docker:

- container: `brafurries-coddy`;
- orchestration: Docker Engine and Docker Compose;
- image process: `python main.py` on Python 3.12;
- image user: non-root UID/GID `10001`;
- restart policy: `unless-stopped`;
- current memory limit: 768 MiB;
- the application filesystem is not persistent storage.

The current production Compose configuration uses `network_mode: host` to access the host-native MariaDB. This is temporary operational state and is not a reason to change networking in an unrelated change.

### Legacy / Temporary Runtime

PM2 is no longer the production runtime. It is retained only as a temporary legacy rollback option. Never start PM2 and the Coddy Docker container at the same time with the same `DISCORD_TOKEN`.

## Directory Structure

### Root

- `main.py`: process entrypoint. Sets UTF-8 stdout, defines bot metadata, and calls `run_discord_client`.
- `settings.py`: static/default bot and Discord settings, including bot name, intents, guild IDs, roles, and legacy social settings.
- `requirements.txt`: pinned Python dependencies.
- `Dockerfile`: current production container image definition using Python 3.12, non-root UID/GID 10001, and `python main.py`.
- `pipeline/1`: legacy PM2 JSON template; it is not part of the current Docker deployment flow.
- `scripts/run_migrations.py`: applies the repository's versioned SQL migrations once.
- `migrations/`: versioned SQL migration files.
- `README.md`: user-facing setup and command documentation.
- `docs/documentacao.html`: static command documentation page.
- `.github/workflows/prod-deploy.yaml`: guarded automatic and manual production deployment workflow.

### `message_services/`

- `discord_service.py`: main runtime composition module. It creates the bot instance, loads environment variables, loads cogs dynamically, syncs slash commands, registers global Discord event handlers, starts background loops, handles startup/shutdown callbacks, and optionally starts the HTTP status API.
- `bot_status_api.py`: `aiohttp` web server for `/`, `/status`, `/health`, `/live`, and `/ready`.
- `message_moderation/`: message moderation helpers.

### `cogs/`

Command modules loaded dynamically by `message_services.discord_service.load_cogs()`.

Observed cogs include administration/configuration, backup, economy, event listings, forms, help, import/recovery, member information, interactions, moderation, games, records, reports, security, trending, utilities, VIP, and XP. Each cog normally defines `async def setup(bot)` and calls `await bot.add_cog(...)`.

### `core/`

Shared implementation modules include:

- `database.py`: central persistence layer, connection pools, runtime schema initialization, server settings, user/profile data, economy, XP, forms, backup, moderation records, events, calendar integration, and many feature-specific data functions.
- `runtime_config.py`: small runtime configuration helpers, including the `BOT_DATABASE_NAME` fallback.
- `shutdown.py`: graceful shutdown handling.
- `routine_functions.py`: shared Discord routines for XP, AI auto-response, VIP role helpers, birthday messages, temporary role removal, thread/hashtag enforcement, and formatting helpers.
- `discord_events.py`: log helpers for profile changes, warnings, bans, mutes, message edits/deletes, and staff role resolution.
- `levels.py`, `xp_policy.py`, and `bot_status.py`: focused XP and status logic.
- `form_views.py` and `portaria_views.py`: persistent Discord views.
- `monthly_activity.py` and `monthly_bumps.py`: monthly activity and bump aggregation.
- `AI_Functions/terceiras/openAI.py`: OpenAI integration for Coddy responses, portaria analysis, and conversation summaries.

### `schemas/`

Simple model and type definitions, including `Config` and `MyBot` in `models/bot.py`, user and feature models, forms, events, localized types, and server message types.

### `tests/`

Unit tests cover isolated XP, validation, status API/payload, database-user mapping, runtime configuration, graceful shutdown, monthly bump persistence, security confirmations, forms, information flows, and read-only event behavior.

## Initialization Flow

1. `main.py` calls `run_discord_client(chatBot)`.
2. `message_services.discord_service` constructs a global `MyBot` with Discord intents.
3. `run_discord_client` loads environment variables and reads `DISCORD_TOKEN`.
4. If configured, the status API is registered as a startup/shutdown callback.
5. `bot.run(token)` starts the Discord client.
6. `MyBot.setup_hook()` runs startup callbacks.
7. On Discord `on_ready`, `initialize_bot()` runs once: it loads cogs, refreshes guild configuration, syncs slash commands after successful cog loading, starts background tasks, registers persistent views, and refreshes invite caches.
8. During normal shutdown, the XP buffer is flushed before the async MariaDB pool is closed; the optional status API shutdown callback runs afterward.

Important side effect: importing `core.database` creates connection pools and executes several schema initialization functions.

## Discord Events

Global event handlers in `message_services/discord_service.py` handle `on_ready`, guild join/remove, invite create/delete, member and user updates, app command errors, messages and message edits/deletes, reactions, and DISBOARD bump confirmations.

Cog listeners add more behavior for voice XP, records, trending presence, forms, security audit handling, and other features.

The main message handler ignores bot messages except DISBOARD, then processes hashtag role mentions in configured public threads, thread owner-only posting restrictions, message XP, and AI auto-responses to mentions or replies.

## Command Architecture

Commands are primarily Discord slash commands using `discord.app_commands`. Patterns include top-level commands, `app_commands.Group`, cog groups through `commands.GroupCog`, manual permission checks, Discord permission decorators, deferred responses for long operations, and persistent `discord.ui.View` and `discord.ui.Modal` flows.

Cog loading is filesystem-based: every `.py` file in `cogs/` is attempted as an extension.

## Database and Persistence

MariaDB remains host-native in production. Coddy connects through:

- `BOT_DATABASE_HOST`;
- `BOT_DATABASE_USER`;
- `BOT_DATABASE_PASSWORD`;
- `BOT_DATABASE_NAME`, which falls back to `coddy` at runtime when unset or blank.

`core/database.py` owns most persistence behavior. It exposes synchronous pooled access through `mysql.connector.pooling.MySQLConnectionPool` and asynchronous pooled access through `aiomysql`, including `close_async_pool()` for shutdown.

### Current Schema Management

Schema management remains mixed and legacy:

- `core/database.py` contains `initialize_*` functions and runtime schema changes;
- `scripts/run_migrations.py` applies the existing versioned SQL files from `migrations/`;
- Flyway and BraFurries-Database are not the single source of truth for this schema.

Schema changes are operational changes. New database work should document the required SQL, affected tables/columns/indexes, data impact, backward compatibility, deployment order, operational risks, and whether runtime initialization already performs equivalent work.

## Community lifecycle and membership synchronization

Community/network lifecycle is API control-plane state. Coddy observes live Discord state and calls the authenticated internal API using the existing `BOT_API_BASE_URL` and `BOT_STATUS_API_TOKEN` integration; it does not create, reactivate, rename, deactivate, or transfer ownership of Community rows through direct SQL.

Runtime behavior is split into:
- incremental member/guild observations for join, leave, nickname/profile, role/approval, guild name, and owner changes;
- complete member reconciliation at startup and every six hours;
- a complete guild-presence snapshot so integrations removed while Coddy was offline become inactive;
- an API-owned sync queue consumed by Coddy for manual/global reconciliation requests.

A complete member snapshot is sent only after Discord's full member fetch succeeds and the observed count matches `guild.member_count`. Incomplete views fail closed and cannot mark members absent. Sync execution is serialized per guild.

Legacy helpers in `core/database.py` remain for runtime domains such as economy/XP compatibility, but `ensure_community_registration_for_guilds` no longer performs Community lifecycle DML. External Discord names remain in `user_discord`; Discord synchronization does not overwrite the platform-owned `users.display_name`.

## Authorization

Authorization is distributed across cogs and helpers. Observed mechanisms include Discord permissions (`administrator`, `manage_guild`, `manage_roles`, and `manage_channels`), permission decorators, configured staff roles resolved with `getStaffRoles`, server-owner checks for security actions, feature-specific IDs, and role-hierarchy checks.

Because these checks are distributed, behavior changes must inspect the relevant cog and the called `core.database` or `core.discord_events` helpers.

## External Integrations

### Discord

The bot uses `discord.py` for slash commands, event listeners, views, modals, embeds, audit logs, guild invites, role/channel APIs, DMs, and message history. Configured intents include guilds, members, messages, reactions, typing, presences, and message content.

### OpenAI

`core/AI_Functions/terceiras/openAI.py` uses `AsyncOpenAI` and the Responses API for Coddy persona replies, portaria ticket analysis, and moderation summaries. OpenAI tokens are stored per guild in the database and encrypted with Fernet, with compatibility logic for legacy encryption.

### Telegram

Telegram Bot API notifies maintainers/admins about command or runtime errors and supports the `call_titio` utility. The relevant environment variable names are `TELEGRAM_TOKEN` and `TELEGRAM_ADMIN`.

### Google Calendar

Coddy does not perform authenticated Google Calendar operations. `GOOGLE_CALENDAR_LINK` may be used as a public link displayed with event listings.

### DISBOARD

The bot watches DISBOARD messages by a hardcoded bot ID to detect bump confirmations, schedule warnings, and grant configured rewards.

## HTTP Status API

The optional `aiohttp` status API is configured with `BOT_STATUS_API_PORT`, `BOT_STATUS_API_HOST`, and `BOT_STATUS_API_TOKEN`.

In production it listens on `127.0.0.1:18088`.

- `/live` is an unauthenticated liveness response and does not depend on Discord or MariaDB readiness.
- `/ready` is an unauthenticated readiness response. It requires completed bot initialization and a ready Discord client.
- `/`, `/status`, `/health`, `/logs`, and `/managed-guilds/{discord_user_id}` require a bearer token when `BOT_STATUS_API_TOKEN` is configured.
- `/status` includes process RSS, the cgroup memory limit when available, and process CPU utilization. `/logs` exposes the current process's 10,000-record administrative in-memory buffer; `limit` defaults to 200 (maximum 1,000), `after` reads newer records, `before` reads older records, and the cursors are mutually exclusive. `level=INFO` filters INFO and above while `level=INFO:exact` matches only INFO.
- `/managed-guilds/{discord_user_id}` returns only guild metadata for cached members who are the guild owner, have administrator, or have `manage_guild`; it returns no permission bits. The route fails closed with 503 until bot initialization and Discord readiness are complete.

Docker health checking uses `/live`. Deployment validation checks both `/live` and `/ready`.

The `/perfil` command obtains confirmed-identity counts and community-scoped
warning totals from the API's internal identity endpoint. Coddy uses
`BOT_API_BASE_URL` (default `http://127.0.0.1:18080`) and the shared
`BOT_STATUS_API_TOKEN`; it does not read or reconstruct `user_identity_links`.

## Environment Management

Production environment values are not stored in the repository. `PROD_ENV_FILE` is held in the GitHub Environment named `Produção`.

The conceptual production flow is:

`GitHub Environment secret PROD_ENV_FILE -> stdin to update-brafurries-coddy-env stage -> strict validation -> /etc/brafurries/coddy/.env.pending -> deployment and migrations -> /etc/brafurries/coddy/.env`

The active environment file is root-owned and outside the repository. Never include real secret values in documentation, logs, commits, or tests.

A local `.env` can exist in a developer workspace. Its presence does not prove it is development-only or safe to use against external systems.

## Deployment Flow

Production deployment is configured in `.github/workflows/prod-deploy.yaml`. Eligible pushes to `main` deploy automatically after tests, image build, and a GitHub-hosted safety gate. Manual `workflow_dispatch` on `main` remains available for intentional operation and recovery.

Automatic deployment is skipped when the push changes `migrations/**`, `scripts/run_migrations.py`, a runtime schema-authority file (`core/database.py` or `cogs/xp.py`), the production workflow, or its gate helper. Those changes require a reviewed manual dispatch because candidate migrations and runtime schema initialization can change database structure before the runtime switch and cannot be structurally rolled back. The gate also skips a push whose event base is unavailable or whose commit is no longer the current head of `main`.

The workflow runs from `main` and proceeds as follows:

1. `build-image` runs on GitHub-hosted `ubuntu-latest` with Python 3.12.10.
2. It installs dependencies, installs pytest, and runs `python -m pytest`.
3. It logs in to GHCR, builds and pushes the Docker image, and verifies its SHA-256 digest.
4. A GitHub-hosted gate verifies automatic-deploy eligibility and current `main` freshness; manual dispatch on `main` bypasses only path safety, never freshness. A stale manual run is skipped and must be restarted from the current `main` head.
5. `deploy-production` runs on the self-hosted `BRFD` runner, protected by the GitHub Environment `Produção`.
6. The runner stages `PROD_ENV_FILE` through `update-brafurries-coddy-env`.
7. It calls `deploy-brafurries-coddy` with the immutable image reference, which performs the candidate-image migration and Docker Compose deployment.
8. The workflow validates `/live`, validates `/ready`, and records the final deployment status.

Production deploys use an immutable image reference of the form `ghcr.io/brafurries/brafurries-discord@sha256:<digest>`, not a mutable tag.

The root-authorized production wrappers remain the only authority that changes Docker, environment promotion, migrations, or runtime state on the VM.

### Production Operational Boundaries

The BRFD runner user has no direct Docker access. Production deployment uses narrow, root-authorized wrappers through sudoers:

- `/usr/local/sbin/deploy-brafurries-coddy`;
- `/usr/local/sbin/update-brafurries-coddy-env`.

The VM is shared with other services. Agents must not run Docker directly in production, alter firewall rules or MariaDB, make global host changes, access or print `PROD_ENV_FILE`, or trigger a production deployment.

## Testing

The test suite is primarily unit-level and covers isolated policy, parsing/validation, status and status API behavior, runtime configuration, graceful shutdown signal handling, monthly bump persistence, database-user mapping, security confirmation handling, forms, member information, and read-only event behavior.

Avoid full bot startup as routine validation: startup can touch credentials, connection pools, MariaDB, Discord, external APIs, HTTP services, and runtime schema initialization.

## Observed Architectural Decisions

- Discord command surface is modularized by cog.
- Bot lifecycle and global events are centralized in `message_services/discord_service.py`.
- Most persistence is centralized in one large `core/database.py` module.
- Some newer behavior uses async database access to avoid blocking Discord heartbeats.
- Persistent Discord views are re-registered during bot initialization.
- Guild-specific settings are stored in MariaDB and converted from snake_case database columns to camelCase config attributes.
- OpenAI tokens are stored per server and encrypted locally.
- Production runtime is Docker; PM2 is only a legacy rollback path.
- User-facing copy is mostly Portuguese.

## Known Inconsistencies

Current limitations include the following:

- Database persistence, runtime schema initialization, and many domain functions are concentrated in `core/database.py`.
- Authorization checks are distributed across cogs and helpers.
- Runtime startup has side effects beyond simple imports.
- External-system integration tests are not present.
- Some configuration is hardcoded in `settings.py` or feature modules.
- Schema management is mixed: versioned SQL migrations coexist with runtime initialization in `core/database.py`; Flyway and BraFurries-Database are not the single schema authority.
- README compatibility notes may lag the current Docker production runtime.
- Code style is mixed: legacy imports, SQL, and simple classes coexist with newer typed helpers, dataclasses, async pools, and focused unit tests.
- Some IDs are hardcoded in code and settings; it is not always clear which are global defaults versus production-specific configuration.
- Production uses MariaDB, while code and dependencies use MySQL-compatible drivers and terminology in several places.
