# Coddy public-repository cutover: legacy runtime configuration

This is a **migration safety checklist**, not a production deployment procedure. The old private Git history contains real IDs and must never be published or exported. Do not copy its files, commits, workflow logs or local environments into the public repository.

## Optional legacy settings

The following **names only** replace IDs formerly embedded in runtime source. Values are not included in this document and must not be committed to Git. All invalid, empty or absent Discord IDs fail closed.

| Variable | Historical consumer | Effect when absent |
| --- | --- | --- |
| `CODDY_CREATOR_DISCORD_ID` | `core/routine_functions.py`, creator label in profile description | Creator label is omitted |
| `CODDY_LEGACY_BIRTHDAY_ROLE_ID` | `core/routine_functions.py`, birthday role assignment | Does not assign the legacy birthday role |
| `CODDY_LEGACY_VIP_ROLE_ID` | `core/service_commands.py`, legacy store VIP service | Service reports missing VIP role |
| `CODDY_LEGACY_PORTARIA_CHANNEL_ID` | `message_services/message_moderation/moderation_functions.py` | Legacy portaria channel filter is disabled |
| `CODDY_LEGACY_ADULT_ROLE_ID` | Old `core/routine_functions.py` constant | Optional deprecated constant defaults to 0 |
| `CODDY_LEGACY_MINOR_ROLE_ID` | Old `core/routine_functions.py` constant | Optional deprecated constant defaults to 0 |
| `CODDY_LEGACY_BIRTHDAY_CHANNEL_ID` | Old `core/routine_functions.py` constant | Optional deprecated constant defaults to 0 |

`settings.py` also supports optional `DISCORD_GUILD_ID`, `DISCORD_ADMINS` (comma-separated), `DISCORD_VIP_ROLES_ID` (comma-separated), legacy role/channel IDs and `TELEGRAM_ADMIN` / `INSTAGRAM_TOKEN` from the environment. The active runtime consumers verified during sanitization use only `BOT_NAME`, `DISCORD_BOT_PREFIX` and `DISCORD_INTENTS`; these interfaces were not removed.

## Operational boundary

1. **No production change is authorized by this document.** Do not retrieve or print the historic values in automation, logs, issues or PRs. Read-only access to the historical archive must remain restricted.
2. Before a future Coddy release, explicitly identify which legacy behaviors are still active. Prefer existing API/guild-scoped configuration where available instead of perpetuating global hardcoded configuration.
3. If a legacy behavior is still required, provision its environment variable securely in the production secret store **under a separately authorized deployment task**. Validate the intended guild, role/channel existence and privilege boundaries before starting the new runtime. Do not infer IDs.
4. Missing values must never fall back to some other guild, user or channel. Test with isolated fixtures, not real Discord, MariaDB, Telegram or OpenAI accounts.
5. Public CI must use fake/synthetic identifiers and run on GitHub-hosted runners only. Do not copy production workflows, runner names, environments, GHCR privileges or secrets.
6. The old repository and its production workflow remain private and disabled during the transition. A clean public root commit does not rotate any credential exposed in historic Git; providers must be checked separately.

The API remains administrative authority, the Database remains schema authority, and Coddy must preserve Community/guild isolation. Do not use this publication work to redesign their contracts.
