import asyncio
import logging
import re
import time
from dataclasses import dataclass
from uuid import uuid4

import discord
from discord import app_commands
from discord.ext import commands

from core.database import registerWarnIfAbsent
from core.discord_events import getStaffRoles
from core.time_functions import now


DYNO_BOT_ID = 155149108183695360
LORITTA_BOT_ID = 297153970613387264
MEE6_BOT_ID = 159985870458322944

logger = logging.getLogger(__name__)


@dataclass
class RoleRecoveryEntry:
    display_name: str
    role_id_from_log: int | None
    server_role: discord.Role | None
    member_ids: set[int]
    is_name_ambiguous: bool = False


@dataclass
class ParsedRecoveryCandidate:
    key: str
    display_name: str
    role_id_from_log: int | None
    server_role: discord.Role | None
    is_name_ambiguous: bool = False


@dataclass
class ParsedRecoveryMessage:
    action: str | None
    member_ids: set[int]
    candidates: list[ParsedRecoveryCandidate]
    unmatched_log: str | None = None


class RecoverRolesView(discord.ui.View):
    def __init__(self, cog: "ImportCog", owner_id: int, snapshot_id: str):
        super().__init__(timeout=14400)
        self.cog = cog
        self.owner_id = owner_id
        self.snapshot_id = snapshot_id
        self.page = 0
        self.show_missing = False
        self.selected_role_key: str | None = None

        self.role_select = discord.ui.Select(
            placeholder="Selecione um cargo",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Carregando...", value="__loading")],
        )
        self.role_select.callback = self.on_select_role
        self.add_item(self.role_select)
        self._refresh_components()

    @property
    def snapshot(self):
        return self.cog.recover_snapshots.get(self.snapshot_id)

    def _clear_snapshot(self):
        self.cog.recover_snapshots.pop(self.snapshot_id, None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Somente quem executou o comando pode interagir com esta mensagem.",
                ephemeral=True,
            )
            return False
        return True

    def _current_entries(self) -> list[tuple[str, RoleRecoveryEntry]]:
        if not self.snapshot:
            return []

        entries: dict[str, RoleRecoveryEntry] = self.snapshot[
            "missing_entries" if self.show_missing else "found_entries"
        ]
        return sorted(entries.items(), key=lambda item: item[1].display_name.lower())

    def _pages_count(self) -> int:
        entries_count = len(self._current_entries())
        if entries_count == 0:
            return 1
        return ((entries_count - 1) // 25) + 1

    def _page_slice(self) -> list[tuple[str, RoleRecoveryEntry]]:
        entries = self._current_entries()
        start = self.page * 25
        return entries[start : start + 25]

    def _refresh_components(self):
        items = self._page_slice()
        if not items:
            self.role_select.disabled = True
            self.role_select.options = [
                discord.SelectOption(
                    label="Nenhum cargo disponível nesta visualização",
                    value="__none",
                    default=True,
                )
            ]
            self.recover_button.disabled = True
        else:
            self.role_select.disabled = False
            self.role_select.options = []
            for key, entry in items:
                suffix = ""
                if entry.role_id_from_log:
                    suffix = f" • ID {entry.role_id_from_log}"

                description = f"{len(entry.member_ids)} membro(s) no último estado"
                self.role_select.options.append(
                    discord.SelectOption(
                        label=entry.display_name[:100],
                        value=key,
                        description=description[:100],
                        default=key == self.selected_role_key,
                    )
                )

                if key == self.selected_role_key and suffix:
                    self.role_select.placeholder = (
                        f"Selecionado: {entry.display_name[:65]}{suffix}"[:100]
                    )

            if self.selected_role_key is None or self.selected_role_key not in {
                key for key, _ in items
            }:
                self.selected_role_key = items[0][0]
                self.role_select.options[0].default = True
            self.recover_button.disabled = False

        total_pages = self._pages_count()
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= (total_pages - 1)

        self.toggle_button.label = (
            "Ver cargos encontrados" if self.show_missing else "Ver cargos não encontrados"
        )

    def build_embed(self) -> discord.Embed:
        snapshot = self.snapshot
        if not snapshot:
            return discord.Embed(
                title="Recuperação de cargos expirada",
                description="Execute o comando novamente para gerar um novo estado.",
                color=discord.Color.red(),
            )

        found_entries = snapshot["found_entries"]
        missing_entries = snapshot["missing_entries"]
        title = "Cargos encontrados no servidor" if not self.show_missing else "Cargos não encontrados no servidor"
        color = discord.Color.green() if not self.show_missing else discord.Color.orange()
        embed = discord.Embed(title=title, color=color)

        items = self._page_slice()
        if items:
            lines = []
            for key, entry in items:
                id_text = f"ID log: `{entry.role_id_from_log}`" if entry.role_id_from_log else "ID log: não informado"
                server_text = (
                    f"Cargo servidor: <@&{entry.server_role.id}>"
                    if entry.server_role
                    else (
                        "Cargo servidor: nome ambíguo (múltiplos cargos com esse nome)"
                        if entry.is_name_ambiguous
                        else "Cargo servidor: não encontrado"
                    )
                )
                lines.append(
                    f"• **{entry.display_name}** — {id_text} — {server_text} — membros: `{len(entry.member_ids)}`"
                )

            description_limit = 4096
            description = ""
            hidden_count = 0

            for line in lines:
                candidate = f"{description}\n{line}" if description else line
                if len(candidate) <= description_limit:
                    description = candidate
                else:
                    hidden_count += 1

            if hidden_count:
                suffix = f"\n... e mais **{hidden_count}** item(ns) nesta página."
                if len(description) + len(suffix) <= description_limit:
                    description += suffix
                else:
                    description = (
                        description[: description_limit - len(suffix) - 3].rstrip()
                        + "..."
                        + suffix
                    )

            embed.description = description or "Nenhum cargo nesta visualização."
        else:
            embed.description = "Nenhum cargo nesta visualização."

        embed.add_field(
            name="Resumo",
            value=(
                f"Cargos detectados nos logs: **{len(found_entries) + len(missing_entries)}**\n"
                f"Encontrados no servidor: **{len(found_entries)}**\n"
                f"Não encontrados no servidor: **{len(missing_entries)}**"
            ),
            inline=False,
        )
        embed.set_footer(
            text=f"Página {self.page + 1}/{self._pages_count()} • Bot de origem: {snapshot['bot_name']}"
        )
        return embed

    async def on_select_role(self, interaction: discord.Interaction):
        selected = self.role_select.values[0]
        if selected.startswith("__"):
            await interaction.response.defer()
            return

        self.selected_role_key = selected
        self._refresh_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        self._clear_snapshot()

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=1)
    async def prev_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._refresh_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page = min(self._pages_count() - 1, self.page + 1)
        self._refresh_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Recuperar último estado", style=discord.ButtonStyle.success, row=2)
    async def recover_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        snapshot = self.snapshot
        if not snapshot:
            return await interaction.response.send_message(
                "Estado expirado. Rode o comando novamente.", ephemeral=True
            )

        selected_key = self.selected_role_key
        if not selected_key:
            return await interaction.response.send_message(
                "Selecione um cargo para recuperar.", ephemeral=True
            )

        if self.show_missing:
            entry = snapshot["missing_entries"].get(selected_key)
            if not entry:
                return await interaction.response.send_message(
                    "Cargo selecionado não está disponível para recuperação.",
                    ephemeral=True,
                )

            resolve_view = ResolveMissingRoleView(
                self.cog,
                self.owner_id,
                self.snapshot_id,
                selected_key,
            )
            return await interaction.response.send_message(
                (
                    "Selecione o cargo atual equivalente para continuar a recuperação.\n"
                    f"Cargo identificado no log: **{entry.display_name}**\n"
                    f"Membros previstos: **{len(entry.member_ids)}**"
                ),
                view=resolve_view,
                ephemeral=True,
            )

        entry = snapshot["found_entries"].get(selected_key)
        if not entry or not entry.server_role:
            return await interaction.response.send_message(
                "Cargo selecionado não está disponível para recuperação.", ephemeral=True
            )

        confirm_view = RecoverConfirmView(
            self.cog,
            self.owner_id,
            self.snapshot_id,
            selected_key,
            target_role_id=entry.server_role.id,
            source_group="found_entries",
        )
        await interaction.response.send_message(
            (
                f"Confirma recuperar o último estado do cargo **{entry.display_name}**?\n"
                f"Cargo destino: {entry.server_role.mention}\n"
                f"Membros previstos: **{len(entry.member_ids)}**"
            ),
            view=confirm_view,
            ephemeral=True,
        )

    @discord.ui.button(label="Ver cargos não encontrados", style=discord.ButtonStyle.primary, row=2)
    async def toggle_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.show_missing = not self.show_missing
        self.page = 0
        self.selected_role_key = None
        self._refresh_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class RecoverConfirmView(discord.ui.View):
    def __init__(
        self,
        cog: "ImportCog",
        owner_id: int,
        snapshot_id: str,
        selected_key: str,
        target_role_id: int,
        source_group: str,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.owner_id = owner_id
        self.snapshot_id = snapshot_id
        self.selected_key = selected_key
        self.target_role_id = target_role_id
        self.source_group = source_group

    @property
    def snapshot(self):
        return self.cog.recover_snapshots.get(self.snapshot_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Somente quem executou o comando pode confirmar esta ação.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()

        snapshot = self.snapshot
        if not snapshot:
            return await interaction.edit_original_response(
                content="Estado expirado. Rode o comando novamente.", view=None
            )

        source_entries = snapshot.get(self.source_group, {})
        entry = source_entries.get(self.selected_key)
        if not entry:
            return await interaction.edit_original_response(
                content="Cargo não disponível para recuperação.", view=None
            )

        guild = interaction.guild
        if guild is None:
            return await interaction.edit_original_response(
                content="Este comando só pode ser usado em servidor.", view=None
            )

        target_role = guild.get_role(self.target_role_id)
        if not target_role:
            return await interaction.edit_original_response(
                content="Cargo de destino não encontrado no servidor.", view=None
            )

        member_ids = list(entry.member_ids)
        total = len(member_ids)
        restored = 0
        failed = 0
        skipped = 0
        processed = 0

        def progress_message(done: bool = False) -> str:
            status = "✅ Recuperação concluída" if done else "⏳ Recuperação em andamento"
            return (
                f"{status} para **{entry.display_name}**.\n"
                f"Cargo de destino: {target_role.mention}\n"
                f"Progresso: **{processed}/{total}**\n"
                f"Adicionados: **{restored}**\n"
                f"Ignorados: **{skipped}**\n"
                f"Falhas: **{failed}**"
            )

        async def safe_update_progress(done: bool = False):
            try:
                await interaction.edit_original_response(
                    content=progress_message(done=done),
                    view=None,
                )
            except discord.HTTPException:
                pass

        await safe_update_progress(done=False)

        queue: asyncio.Queue[int | None] = asyncio.Queue()
        for member_id in member_ids:
            queue.put_nowait(member_id)

        worker_count = min(8, total) if total else 0

        async def assign_role_worker():
            nonlocal restored, failed, skipped, processed
            while True:
                member_id = await queue.get()
                if member_id is None:
                    queue.task_done()
                    break

                member = guild.get_member(member_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(member_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        skipped += 1
                        processed += 1
                        if processed % 5 == 0 or processed == total:
                            await safe_update_progress(done=False)
                        queue.task_done()
                        continue

                if target_role in member.roles:
                    skipped += 1
                    processed += 1
                    if processed % 5 == 0 or processed == total:
                        await safe_update_progress(done=False)
                    queue.task_done()
                    continue

                try:
                    await member.add_roles(
                        target_role,
                        reason=f"Recuperação de último estado via /recover cargos ({snapshot['bot_name']})",
                    )
                    restored += 1
                except discord.HTTPException:
                    failed += 1
                finally:
                    processed += 1
                    if processed % 5 == 0 or processed == total:
                        await safe_update_progress(done=False)
                    queue.task_done()

        workers = [asyncio.create_task(assign_role_worker()) for _ in range(worker_count)]

        await queue.join()

        for _ in range(worker_count):
            queue.put_nowait(None)

        if workers:
            await asyncio.gather(*workers)

        await safe_update_progress(done=True)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Recuperação cancelada.", view=None)

class ResolveMissingRoleView(discord.ui.View):
    def __init__(self, cog: "ImportCog", owner_id: int, snapshot_id: str, selected_key: str):
        super().__init__(timeout=900)
        self.cog = cog
        self.owner_id = owner_id
        self.snapshot_id = snapshot_id
        self.selected_key = selected_key
        self.selected_role_id: int | None = None

    @property
    def snapshot(self):
        return self.cog.recover_snapshots.get(self.snapshot_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Somente quem executou o comando pode confirmar esta ação.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Selecione o cargo equivalente", min_values=1, max_values=1)
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0] if select.values else None
        self.selected_role_id = role.id if role else None
        await interaction.response.defer()

    @discord.ui.button(label="Confirmar cargo equivalente", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        snapshot = self.snapshot
        if not snapshot:
            return await interaction.response.edit_message(
                content="Estado expirado. Rode o comando novamente.", view=None
            )

        entry = snapshot["missing_entries"].get(self.selected_key)
        if not entry:
            return await interaction.response.edit_message(
                content="Cargo não encontrado na prévia.", view=None
            )

        if not self.selected_role_id:
            return await interaction.response.send_message(
                "Selecione um cargo de destino antes de confirmar.",
                ephemeral=True,
            )

        role = interaction.guild.get_role(self.selected_role_id) if interaction.guild else None
        if not role:
            return await interaction.response.send_message(
                "Cargo de destino não existe mais no servidor.",
                ephemeral=True,
            )

        confirm_view = RecoverConfirmView(
            self.cog,
            self.owner_id,
            self.snapshot_id,
            self.selected_key,
            target_role_id=self.selected_role_id,
            source_group="missing_entries",
        )
        await interaction.response.edit_message(
            content=(
                f"Confirma atribuir {role.mention} para todos que tinham o cargo antigo "
                f"**{entry.display_name}**?\n"
                f"Membros previstos: **{len(entry.member_ids)}**"
            ),
            view=confirm_view,
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Atribuição cancelada.", view=None)


class ImportCog(commands.Cog):
    __cog_name__ = "Import"

    importar = app_commands.Group(
        name="importar", description="Comandos para importação de registros"
    )
    recover = app_commands.Group(
        name="recover", description="Comandos de recuperação de estado por logs"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.recover_snapshots: dict[str, dict] = {}
        super().__init__()

    @staticmethod
    def _normalize_role_name(role_name: str) -> str:
        return re.sub(r"\s+", " ", role_name.strip().lower())

    @staticmethod
    def _strip_markdown_wrappers(value: str) -> str:
        cleaned = value.strip()
        markdown_pairs = ("**", "__", "~~", "*", "_", "~")

        changed = True
        while changed and cleaned:
            changed = False
            for marker in markdown_pairs:
                if (
                    cleaned.startswith(marker)
                    and cleaned.endswith(marker)
                    and len(cleaned) > len(marker) * 2
                ):
                    cleaned = cleaned[len(marker) : -len(marker)].strip()
                    changed = True

        return cleaned.strip("`*_~")

    @staticmethod
    def _extract_member_ids(text: str) -> set[int]:
        mention_pattern = re.compile(r"<@!?(\d{17,20})>")
        target_mention_pattern = re.compile(
            r"(?:usu[aá]rio|usuario|user|member|membro|target|alvo)\s*(?:id)?\s*[:=\-]?\s*<@!?(\d{17,20})>",
            flags=re.IGNORECASE,
        )
        actor_mention_pattern = re.compile(
            r"(?:moderator|mod|staff|autor|actor|executor)\s*(?:id)?\s*[:=\-]?\s*<@!?(\d{17,20})>",
            flags=re.IGNORECASE,
        )
        target_keywords = (
            "usuário",
            "usuario",
            "user",
            "member",
            "membro",
            "target",
            "alvo",
        )
        actor_keywords = (
            "moderator",
            "mod",
            "staff",
            "autor",
            "actor",
            "executor",
        )

        target_ids: set[int] = set()
        all_mentions: list[int] = []
        has_actor_hint_in_text = False

        explicit_target_ids = {int(raw) for raw in target_mention_pattern.findall(text)}
        explicit_actor_ids = {int(raw) for raw in actor_mention_pattern.findall(text)}
        target_ids.update(explicit_target_ids - explicit_actor_ids)

        for line in text.splitlines():
            line_mentions = [int(raw) for raw in mention_pattern.findall(line)]
            if not line_mentions:
                continue

            lowered_line = line.lower()
            all_mentions.extend(line_mentions)
            has_target_hint = any(keyword in lowered_line for keyword in target_keywords)
            has_actor_hint = any(keyword in lowered_line for keyword in actor_keywords)
            if has_actor_hint:
                has_actor_hint_in_text = True

            line_explicit_targets = {
                int(raw) for raw in target_mention_pattern.findall(line)
            }
            line_explicit_actors = {int(raw) for raw in actor_mention_pattern.findall(line)}

            if has_target_hint and has_actor_hint:
                target_ids.update(line_explicit_targets - line_explicit_actors)
                if len(line_mentions) == 1 and not line_explicit_actors:
                    target_ids.add(line_mentions[0])
                continue

            if has_actor_hint:
                continue

            if line_explicit_targets:
                target_ids.update(line_explicit_targets - line_explicit_actors)
                continue

            if has_target_hint:
                if len(line_mentions) == 1:
                    target_ids.add(line_mentions[0])
                elif len(line_explicit_targets) == 1:
                    target_ids.update(line_explicit_targets)
                continue

            # Do not treat unlabeled single mentions as targets here: in many
            # embed log formats actor/moderator mentions appear on standalone
            # lines, which would otherwise be incorrectly restored.

        if target_ids:
            return target_ids

        if len(all_mentions) == 1 and not has_actor_hint_in_text:
            return {all_mentions[0]}

        return set()

    @staticmethod
    def _extract_footer_ids(text: str) -> set[int]:
        footer_id_pattern = re.compile(
            r"(?:^|\b)(?:user(?:\s*id)?|member(?:\s*id)?|usu[aá]rio(?:\s*id)?|usuario(?:\s*id)?|membro(?:\s*id)?)\s*[:#-]?\s*(\d{17,20})\b",
            flags=re.IGNORECASE,
        )
        return {int(raw) for raw in footer_id_pattern.findall(text)}

    @staticmethod
    def _extract_possible_role_names(text: str) -> set[str]:
        names: set[str] = set()
        role_suffixes = (
            " role",
            " roles",
            " cargo",
            " cargos",
        )

        def add_candidate(raw_candidate: str, *, strip_role_suffix: bool = False):
            candidate = re.sub(r"\s+", " ", raw_candidate).strip(" -_:,.;\n\t`'\"“”‘’")
            candidate = ImportCog._strip_markdown_wrappers(candidate)
            if not candidate:
                return

            if strip_role_suffix:
                lowered_candidate = candidate.lower()
                for suffix in role_suffixes:
                    if lowered_candidate.endswith(suffix):
                        candidate = candidate[: -len(suffix)].strip(" -_:,.;\n\t`'\"“”‘’")
                        lowered_candidate = candidate.lower()
                        break

            if candidate:
                names.add(candidate.lower())

        def split_role_list(raw_list: str, *, split_on_conjunctions: bool = False) -> list[str]:
            cleaned_list = raw_list
            if split_on_conjunctions:
                cleaned_list = re.sub(
                    r"\s+(?:and|e)\s+",
                    ",",
                    raw_list,
                    flags=re.IGNORECASE,
                )
            return [
                chunk
                for chunk in re.split(r",", cleaned_list)
                if chunk.strip()
            ]

        patterns = [
            r'cargo\s*[:\-]\s*[`"\']([^`"\'\n]{2,100})[`"\']',
            r'role\s*[:\-]\s*[`"\']([^`"\'\n]{2,100})[`"\']',
            r"cargo\s*[:\-]\s*\*\*([^*\n]{2,100})\*\*",
            r"role\s*[:\-]\s*\*\*([^*\n]{2,100})\*\*",
        ]
        for pattern in patterns:
            for candidate in re.findall(pattern, text, flags=re.IGNORECASE):
                add_candidate(candidate)

        # Alguns logs listam os cargos sempre entre crases:
        # `cargo1`, `_cargo2_`, `**cargo3**`
        # Nesses casos, extraímos cada bloco entre crases e limpamos markdown.
        for candidate in re.findall(r"`([^`\n]{2,100})`", text):
            add_candidate(candidate)

        lowered = text.lower()
        dyno_patterns = [
            r"was\s+given\s+the\s+(.+?)\s+role(s)?\b",
            r"was\s+given\s+the\s+roles\s+(.+?)(?:$|\n)",
            r"was\s+removed\s+from\s+the\s+(.+?)\s+role(s)?\b",
            r"recebeu\s+o\s+cargo(s)?\s+(.+?)(?:$|\n)",
            r"foi\s+removido\s+do\s+cargo(s)?\s+(.+?)(?:$|\n)",
        ]
        for pattern in dyno_patterns:
            for match in re.finditer(pattern, lowered, flags=re.IGNORECASE):
                groups = [group for group in match.groups() if group is not None]
                role_group = groups[-1]
                plural_marker = any(group.lower() == "s" for group in groups[:-1])
                split_on_conjunctions = plural_marker or "," in role_group
                for role_name in split_role_list(
                    role_group,
                    split_on_conjunctions=split_on_conjunctions,
                ):
                    add_candidate(role_name, strip_role_suffix=True)

        return names

    @staticmethod
    def _extract_dyno_footer_user_id(message: discord.Message) -> int | None:
        if not message.embeds:
            return None

        for embed in message.embeds:
            footer_text = (getattr(embed.footer, "text", "") or "").strip()
            if not footer_text:
                continue

            id_match = re.search(r"\bID\s*:\s*(\d{17,20})\b", footer_text, flags=re.IGNORECASE)
            if id_match:
                return int(id_match.group(1))

            generic_match = re.search(r"(\d{17,20})", footer_text)
            if generic_match:
                return int(generic_match.group(1))

        return None

    @staticmethod
    def _parse_dyno_role_change(
        text: str, footer_user_id: int | None = None
    ) -> tuple[str | None, set[int], set[str]]:
        normalized_text = re.sub(r"\s+", " ", text).strip()
        quoted_roles_pattern = re.compile(r"`([^`\n]{1,100})`")
        action: str | None = None

        if re.search(r"\bwas\s+given\s+the\b", normalized_text, flags=re.IGNORECASE):
            action = "add"
        elif re.search(
            r"\bwas\s+removed\s+from\s+the\b", normalized_text, flags=re.IGNORECASE
        ):
            action = "remove"

        if not action or not re.search(r"\broles?\b", normalized_text, flags=re.IGNORECASE):
            return None, set(), set()

        user_match = re.search(
            r"(?:<@!?(\d{17,20})>|@(\d{17,20}))",
            normalized_text,
            flags=re.IGNORECASE,
        )
        user_id = user_match.group(1) or user_match.group(2) if user_match else None
        if user_id:
            member_ids = {int(user_id)}
        elif footer_user_id:
            member_ids = {footer_user_id}
        else:
            member_ids = set()

        role_names = {
            ImportCog._strip_markdown_wrappers(role_name)
            for role_name in quoted_roles_pattern.findall(normalized_text)
            if ImportCog._strip_markdown_wrappers(role_name)
        }

        if member_ids and role_names:
            return action, member_ids, role_names

        return None, set(), set()

    @staticmethod
    def _detect_role_action(text: str) -> str | None:
        lowered = text.lower()
        added_words = (
            "adicion",
            "recebeu",
            "ganhou",
            "granted",
            "added",
            "given",
        )
        removed_words = (
            "remove",
            "perdeu",
            "retir",
            "revoked",
            "taken",
        )
        has_add = any(word in lowered for word in added_words)
        has_remove = any(word in lowered for word in removed_words)
        if has_add and not has_remove:
            return "add"
        if has_remove and not has_add:
            return "remove"
        return None

    @staticmethod
    def _message_to_text(message: discord.Message) -> str:
        chunks: list[str] = [message.content or ""]
        for embed in message.embeds:
            chunks.extend(
                [
                    embed.title or "",
                    embed.description or "",
                    getattr(embed.author, "name", "") or "",
                    getattr(embed.footer, "text", "") or "",
                ]
            )
            for field in embed.fields:
                chunks.append(field.name or "")
                chunks.append(field.value or "")
        return "\n".join(chunks)

    @staticmethod
    def _is_message_from_selected_bot(
        message: discord.Message, bot_choice: str, selected_bot_id: int
    ) -> bool:
        if message.author.id == selected_bot_id:
            return True

        if not message.webhook_id:
            return False

        expected_name = bot_choice.casefold()
        author_name = (message.author.name or "").casefold()
        global_name = (getattr(message.author, "global_name", "") or "").casefold()
        display_name = (message.author.display_name or "").casefold()
        return expected_name in {author_name, global_name, display_name}

    def _process_recovery_message(
        self,
        bot_choice: str,
        message: discord.Message,
        roles_by_id: dict[int, discord.Role],
        roles_by_name: dict[str, list[discord.Role]],
        entries: dict[str, RoleRecoveryEntry],
    ) -> str | None:
        text = self._message_to_text(message)
        footer_user_id = (
            self._extract_dyno_footer_user_id(message) if bot_choice == "Dyno" else None
        )
        parsed = self._parse_recovery_text(
            bot_choice,
            text,
            roles_by_id,
            roles_by_name,
            footer_user_id=footer_user_id,
        )
        self._apply_recovery_message(entries, parsed)
        return parsed.unmatched_log

    def _parse_recovery_text(
        self,
        bot_choice: str,
        text: str,
        roles_by_id: dict[int, discord.Role],
        roles_by_name: dict[str, list[discord.Role]],
        footer_user_id: int | None = None,
    ) -> ParsedRecoveryMessage:
        if not text.strip():
            return ParsedRecoveryMessage(action=None, member_ids=set(), candidates=[])

        if bot_choice == "Dyno":
            action, member_ids, role_names = self._parse_dyno_role_change(
                text, footer_user_id=footer_user_id
            )
            role_ids: set[int] = set()
            if not role_names:
                unknown_role_log = self._extract_unmatched_dyno_role_log(text)
                if unknown_role_log:
                    return ParsedRecoveryMessage(
                        action=None,
                        member_ids=set(),
                        candidates=[],
                        unmatched_log=unknown_role_log,
                    )
        else:
            member_ids = self._extract_member_ids(text)
            role_ids = {int(raw) for raw in re.findall(r"<@&(\d{17,20})>", text)}
            role_names = self._extract_possible_role_names(text)
            action = self._detect_role_action(text)

        if not role_ids and not role_names:
            return ParsedRecoveryMessage(action=None, member_ids=set(), candidates=[])

        candidates: list[ParsedRecoveryCandidate] = []
        for role_id in role_ids:
            role = roles_by_id.get(role_id)
            name = role.name if role else f"Cargo ID {role_id}"
            candidates.append(
                ParsedRecoveryCandidate(
                    key=f"id:{role_id}",
                    display_name=name,
                    role_id_from_log=role_id,
                    server_role=role,
                )
            )

        for role_name in role_names:
            normalized_role_name = self._normalize_role_name(role_name)
            matched_roles = roles_by_name.get(normalized_role_name, [])

            resolved_role: discord.Role | None = None
            resolved_role_id: int | None = None
            display_name = role_name
            is_ambiguous = False

            if len(matched_roles) == 1:
                resolved_role = matched_roles[0]
                resolved_role_id = resolved_role.id
                display_name = resolved_role.name
            elif len(matched_roles) > 1:
                is_ambiguous = True
                display_name = f"{role_name} (nome ambíguo)"

            candidates.append(
                ParsedRecoveryCandidate(
                    key=f"name:{normalized_role_name}",
                    display_name=display_name,
                    role_id_from_log=resolved_role_id,
                    server_role=resolved_role,
                    is_name_ambiguous=is_ambiguous,
                )
            )

        return ParsedRecoveryMessage(
            action=action,
            member_ids=member_ids,
            candidates=candidates,
        )

    @staticmethod
    def _apply_recovery_message(
        entries: dict[str, RoleRecoveryEntry], parsed: ParsedRecoveryMessage
    ) -> None:
        for candidate in parsed.candidates:
            entry = entries.get(candidate.key)
            if not entry:
                entry = RoleRecoveryEntry(
                    display_name=candidate.display_name,
                    role_id_from_log=candidate.role_id_from_log,
                    server_role=candidate.server_role,
                    member_ids=set(),
                    is_name_ambiguous=candidate.is_name_ambiguous,
                )
                entries[candidate.key] = entry
            elif not entry.server_role and candidate.server_role:
                entry.server_role = candidate.server_role
                entry.role_id_from_log = candidate.role_id_from_log
            if candidate.is_name_ambiguous:
                entry.is_name_ambiguous = True

            if not parsed.member_ids:
                continue

            if parsed.action == "remove":
                entry.member_ids.difference_update(parsed.member_ids)
            elif parsed.action in ("add", None):
                # "add" or ambiguous (None): include members to err on the
                # side of recovery completeness.
                entry.member_ids.update(parsed.member_ids)

    @staticmethod
    def _extract_unmatched_dyno_role_log(text: str) -> str | None:
        normalized_text = re.sub(r"\s+", " ", text).strip()
        if not normalized_text:
            return None

        # Only abort for lines that clearly look like Dyno role-change logs.
        # This avoids false-positives in other Dyno messages that mention
        # "role"/"roles" but are unrelated to member role changes.
        role_change_like_pattern = re.compile(
            r"(?:<@!?[^>]+>|@\S+).+?\bwas\s+(?:given|removed).+?\broles?\b",
            flags=re.IGNORECASE,
        )
        if role_change_like_pattern.search(normalized_text):
            return normalized_text[:400]

        return None

    @staticmethod
    def _finalize_recovery_snapshot(
        bot_choice: str, entries: dict[str, RoleRecoveryEntry]
    ) -> dict:
        found_entries = {key: entry for key, entry in entries.items() if entry.server_role}
        missing_entries = {
            key: entry for key, entry in entries.items() if not entry.server_role
        }

        return {
            "bot_name": bot_choice,
            "found_entries": found_entries,
            "missing_entries": missing_entries,
        }

    def _build_recovery_snapshot(
        self, guild: discord.Guild, bot_choice: str, messages: list[discord.Message]
    ) -> dict:
        roles_by_id = {role.id: role for role in guild.roles}
        roles_by_name: dict[str, list[discord.Role]] = {}
        for role in guild.roles:
            normalized_name = self._normalize_role_name(role.name)
            roles_by_name.setdefault(normalized_name, []).append(role)
        entries: dict[str, RoleRecoveryEntry] = {}

        for message in messages:
            self._process_recovery_message(
                bot_choice,
                message,
                roles_by_id,
                roles_by_name,
                entries,
            )

        return self._finalize_recovery_snapshot(bot_choice, entries)

    @staticmethod
    def _extract_user_id(field_value: str | None) -> int | None:
        if not field_value:
            return None

        match = re.search(r"(\d{17,20})", field_value)
        if match:
            return int(match.group(1))
        return None

    def _parse_dyno_warn(self, message: discord.Message):
        if message.author.id != DYNO_BOT_ID or not message.embeds:
            return None

        embed = message.embeds[0]
        embed_author_name = (getattr(embed.author, "name", "") or "").lower()

        if "warn" not in embed_author_name:
            return None

        user_id = None
        moderator_id = None
        reason = None

        for field in embed.fields:
            field_name = (field.name or "").lower()
            field_value = field.value or ""

            if "user" in field_name:
                user_id = self._extract_user_id(field_value)
            elif "moderator" in field_name:
                moderator_id = self._extract_user_id(field_value)
            elif "reason" in field_name or "motivo" in field_name:
                reason = field_value.strip()

        warn_date = embed.timestamp or message.created_at or now()
        if warn_date.tzinfo:
            warn_date = warn_date.replace(tzinfo=None)

        if user_id is None or reason is None:
            return None

        return {
            "user_id": user_id,
            "moderator_id": moderator_id,
            "reason": reason,
            "date": warn_date,
        }

    async def _fetch_user_by_id(
        self, guild: discord.Guild, user_id: int
    ) -> discord.Member | discord.User | None:
        member = guild.get_member(user_id)
        if member:
            return member

        try:
            return await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException, AttributeError):
            pass

        try:
            return await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return None

    @importar.command(
        name="warns", description="Importa warns enviados por bots em um canal"
    )
    @app_commands.describe(
        bot="Bot de onde importar os warns",
        canal="Canal que contém os registros de warn",
        quantidade="Quantidade máxima de warns novos a registrar (opcional)",
    )
    @app_commands.choices(bot=[app_commands.Choice(name="Dyno", value=str(DYNO_BOT_ID))])
    async def importar_warns(
        self,
        ctx: discord.Interaction,
        bot: app_commands.Choice[str],
        canal: discord.TextChannel,
        quantidade: app_commands.Range[int, 1, 500] | None = None,
    ):
        staff_roles = getStaffRoles(ctx.guild)
        if not any(role in ctx.user.roles for role in staff_roles):
            return await ctx.response.send_message(
                content="Apenas membros da staff podem usar este comando.",
                ephemeral=True,
            )

        await ctx.response.defer(ephemeral=True)

        mensagens_analisadas = 0
        warns_encontrados = 0
        novos = 0
        ignorados = 0
        total_warns = 0
        status_updates_disabled = False

        def build_status_content(title: str) -> str:
            return (
                f"## {title}\n"
                f"Mensagens analisadas: {mensagens_analisadas}\n"
                f"Total de warns: {total_warns} (duplicados + importados)\n"
                f"Warns já registrados: {total_warns}\n"
                f"warns importados: {novos}"
            )

        parsers = {str(DYNO_BOT_ID): self._parse_dyno_warn}
        parser = parsers.get(bot.value)
        if parser is None:
            return await ctx.followup.send(
                content="Bot de importação não suportado.", ephemeral=True
            )

        status_message = await ctx.followup.send(
            content=build_status_content("importando warnings..."),
            ephemeral=True,
        )
        last_update = time.monotonic()

        async def try_update_status(title: str):
            nonlocal last_update, status_updates_disabled

            if status_updates_disabled or time.monotonic() - last_update < 3:
                return

            try:
                await status_message.edit(content=build_status_content(title))
                last_update = time.monotonic()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                status_updates_disabled = True

        async for message in canal.history(limit=None, oldest_first=True):
            parsed_warn = parser(message)
            if not parsed_warn:
                mensagens_analisadas += 1
                await try_update_status("importando warnings...")
                continue

            if quantidade is not None and novos >= quantidade:
                break

            warns_encontrados += 1

            user = await self._fetch_user_by_id(ctx.guild, parsed_warn["user_id"])
            if not user:
                total_warns = warns_encontrados
                mensagens_analisadas += 1
                await try_update_status("importando warnings...")
                ignorados += 1
                continue

            moderator_user = None
            if parsed_warn["moderator_id"]:
                moderator_user = await self._fetch_user_by_id(
                    ctx.guild, parsed_warn["moderator_id"]
                )

            result = registerWarnIfAbsent(
                ctx.guild.id,
                user,
                parsed_warn["reason"],
                moderator_user,
                parsed_warn["date"],
            )

            if not result:
                ignorados += 1
                mensagens_analisadas += 1
                total_warns = warns_encontrados
                await try_update_status("importando warnings...")
                continue

            if result.get("created"):
                novos += 1

            total_warns = warns_encontrados
            mensagens_analisadas += 1
            await try_update_status("importando warnings...")

        final_content = (
            f"{build_status_content('importação concluida!')}\n"
            f"Ignorados por erro ou usuário não encontrado: {ignorados}"
        )

        if status_updates_disabled:
            try:
                await ctx.followup.send(content=final_content, ephemeral=True)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning(
                    "Falha ao enviar status final da importação de warns. "
                    "analisadas=%s encontrados=%s novos=%s ignorados=%s",
                    mensagens_analisadas,
                    warns_encontrados,
                    novos,
                    ignorados,
                )
            return

        try:
            await status_message.edit(content=final_content)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            try:
                await ctx.followup.send(content=final_content, ephemeral=True)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning(
                    "Falha ao enviar status final da importação de warns. "
                    "analisadas=%s encontrados=%s novos=%s ignorados=%s",
                    mensagens_analisadas,
                    warns_encontrados,
                    novos,
                    ignorados,
                )

    @recover.command(
        name="cargos",
        description="Detecta cargos em logs de bot e prepara recuperação do último estado",
    )
    @app_commands.describe(
        canal="Canal que contém os logs do bot",
        bot="Bot de origem dos logs",
    )
    @app_commands.choices(
        bot=[
            app_commands.Choice(name="Dyno", value="Dyno"),
            app_commands.Choice(name="Loritta", value="Loritta"),
            app_commands.Choice(name="mee6", value="mee6"),
        ]
    )
    async def recover_cargos(
        self,
        ctx: discord.Interaction,
        canal: discord.TextChannel,
        bot: app_commands.Choice[str],
    ):
        if not ctx.guild:
            return await ctx.response.send_message(
                content="Este comando só pode ser usado em um servidor.",
                ephemeral=True,
            )

        staff_roles = getStaffRoles(ctx.guild)
        if not any(role in ctx.user.roles for role in staff_roles):
            return await ctx.response.send_message(
                content="Apenas membros da staff podem usar este comando.",
                ephemeral=True,
            )

        bot_ids = {
            "Dyno": DYNO_BOT_ID,
            "Loritta": LORITTA_BOT_ID,
            "mee6": MEE6_BOT_ID,
        }
        selected_bot_id = bot_ids[bot.value]

        await ctx.response.send_message(
            content="🔎 Iniciando análise dos logs...",
            ephemeral=False,
        )
        status_message = await ctx.original_response()

        roles_by_id = {role.id: role for role in ctx.guild.roles}
        roles_by_name: dict[str, list[discord.Role]] = {}
        for role in ctx.guild.roles:
            normalized_name = self._normalize_role_name(role.name)
            roles_by_name.setdefault(normalized_name, []).append(role)

        entries: dict[str, RoleRecoveryEntry] = {}
        messages_scanned = 0
        bot_messages_found = 0
        last_update = time.monotonic()
        processing_batch_size = 200
        max_parallel_workers = 8
        pending_payloads: list[tuple[str, int | None]] = []

        async def upsert_status(content: str):
            nonlocal status_message

            old_status_message = status_message
            if old_status_message:
                try:
                    await old_status_message.edit(content=content)
                    return
                except (discord.NotFound, discord.HTTPException):
                    pass

            try:
                new_status_message = await ctx.channel.send(content=content)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning(
                    "Falha ao criar mensagem de status da recuperação de cargos. "
                    "verificadas=%s bot=%s candidatos=%s",
                    messages_scanned,
                    bot_messages_found,
                    len(entries),
                )
                return

            if old_status_message and old_status_message.id != new_status_message.id:
                try:
                    await old_status_message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

            status_message = new_status_message

        async def try_update_status(title: str):
            nonlocal last_update

            if time.monotonic() - last_update < 3:
                return

            await upsert_status(
                content=(
                    f"{title}\n"
                    f"Mensagens verificadas: **{messages_scanned}**\n"
                    f"Mensagens do bot encontradas: **{bot_messages_found}**\n"
                    f"Candidatos de cargo detectados: **{len(entries)}**"
                )
            )
            last_update = time.monotonic()

        async def process_pending_payloads() -> str | None:
            nonlocal pending_payloads
            if not pending_payloads:
                return None

            semaphore = asyncio.Semaphore(max_parallel_workers)

            async def parse_payload(payload: tuple[str, int | None]) -> ParsedRecoveryMessage:
                text, footer_user_id = payload
                async with semaphore:
                    return await asyncio.to_thread(
                        self._parse_recovery_text,
                        bot.value,
                        text,
                        roles_by_id,
                        roles_by_name,
                        footer_user_id,
                    )

            parsed_messages = await asyncio.gather(
                *(parse_payload(payload) for payload in pending_payloads)
            )
            pending_payloads = []

            for parsed in parsed_messages:
                if parsed.unmatched_log:
                    return parsed.unmatched_log
                self._apply_recovery_message(entries, parsed)

            return None

        try:
            async for message in canal.history(limit=None, oldest_first=True):
                messages_scanned += 1
                if self._is_message_from_selected_bot(message, bot.value, selected_bot_id):
                    bot_messages_found += 1
                    pending_payloads.append(
                        (
                            self._message_to_text(message),
                            self._extract_dyno_footer_user_id(message)
                            if bot.value == "Dyno"
                            else None,
                        )
                    )
                    if len(pending_payloads) >= processing_batch_size:
                        unmatched_log = await process_pending_payloads()
                        if unmatched_log:
                            await upsert_status(
                                "⚠️ Encontrei um log com a palavra `role` fora dos padrões suportados.\n"
                                "A análise foi interrompida para revisão manual.\n\n"
                                f"Mensagem identificada:\n```{unmatched_log}```"
                            )
                            return

                await try_update_status("🔎 Analisando logs...")
        except discord.Forbidden:
            await upsert_status("Sem permissão para ler o histórico deste canal.")
            return

        unmatched_log = await process_pending_payloads()
        if unmatched_log:
            await upsert_status(
                "⚠️ Encontrei um log com a palavra `role` fora dos padrões suportados.\n"
                "A análise foi interrompida para revisão manual.\n\n"
                f"Mensagem identificada:\n```{unmatched_log}```"
            )
            return

        snapshot = self._finalize_recovery_snapshot(bot.value, entries)
        final_status = (
            "🧠 Logs lidos. Prévia de recuperação de cargos pronta.\n"
            f"Mensagens verificadas: **{messages_scanned}**\n"
            f"Mensagens do bot encontradas: **{bot_messages_found}**\n"
            f"Candidatos de cargo detectados: **{len(entries)}**"
        )

        await upsert_status(final_status)
        snapshot_id = str(uuid4())
        self.recover_snapshots[snapshot_id] = snapshot

        view = RecoverRolesView(self, ctx.user.id, snapshot_id)
        try:
            await ctx.channel.send(
                content=f"{ctx.user.mention} prévia de recuperação pronta:",
                embed=view.build_embed(),
                view=view,
            )
        except discord.HTTPException:
            self.recover_snapshots.pop(snapshot_id, None)
            raise


async def setup(bot: commands.Bot):
    await bot.add_cog(ImportCog(bot))
