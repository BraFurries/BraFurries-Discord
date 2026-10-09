import asyncio
import logging
import random
from datetime import datetime, timedelta
from math import ceil
from typing import Any, Literal, Optional

from discord import (
    ButtonStyle,
    Embed,
    Interaction,
    Member,
    Message,
    app_commands,
    ui,
)
from discord.ext import commands

from core.database import (
    adjust_user_economy_balance,
    create_community_store_item,
    fetch_community_store_items,
    get_community_store_item,
    ensure_service_inventory_available,
    get_user_economy_balance,
    get_user_inventory_items,
    mark_service_inventory_usage,
    restore_service_inventory_usage,
    purchase_community_store_item,
    set_user_economy_balance,
    transfer_user_economy_balance,
    get_economy_config,
    update_community_store_item,
)

from core.service_commands import (
    ServiceCommandError,
    execute_service_command,
    has_service_command,
)

from core.database import getGuildAgeRoleIds
from core.notifications import notify_owner_and_user


BETTING_EMOJIS = ["🍒", "🍋", "🍉", "⭐", "💎", "🍀", "🎲", "🍇", "🍓", "🍍"]


def _has_adult_role(member: Member) -> bool:
    guild = getattr(member, "guild", None)
    if guild is None:
        return False

    adult_role_id = getGuildAgeRoleIds(guild.id).get("adultRoleId")
    if not adult_role_id:
        return False

    return any(role.id == adult_role_id for role in member.roles)


def _shuffle_emojis_with_matches(match_length: int) -> list[str]:
    base = BETTING_EMOJIS.copy()
    random.shuffle(base)

    if match_length <= 0:
        return base[:4]

    chosen = base[0]
    remaining_emojis = [emoji for emoji in base[1:] if emoji != chosen]

    if match_length >= 4:
        return [chosen] * 4

    result: list[str | None] = [None] * 4
    start = random.randint(0, 4 - match_length)
    result[start : start + match_length] = [chosen] * match_length

    fill_positions = [index for index, value in enumerate(result) if value is None]
    for index, emoji in zip(fill_positions, remaining_emojis):
        result[index] = emoji

    return [emoji for emoji in result if emoji is not None]


def format_duration_seconds(seconds: int) -> str:
    """Convert a duration in seconds to a compact human readable text."""

    try:
        total_seconds = int(seconds)
    except (TypeError, ValueError):
        return "0s"

    total_seconds = max(0, total_seconds)
    breakdown = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))
    parts: list[str] = []
    remainder = total_seconds

    for suffix, length in breakdown:
        value, remainder = divmod(remainder, length)
        if value:
            parts.append(f"{value}{suffix}")

    if not parts:
        parts.append("0s")

    return " ".join(parts)


class ServicePurchaseView(ui.View):
    """View responsável por permitir o uso imediato de um serviço adquirido."""

    def __init__(
        self,
        interaction: Interaction,
        *,
        item: dict[str, Any],
        inventory_entry: Optional[dict[str, Any]],
        store_item_id: int,
        duration: Optional[int],
        message_content: str,
    ) -> None:
        super().__init__(timeout=180)
        self.guild = interaction.guild
        self.buyer = interaction.user
        self.item = item
        self.inventory_entry = inventory_entry
        self.store_item_id = int(store_item_id)
        self.duration = duration
        self.base_message = message_content
        self.status_text: Optional[str] = None
        self.message: Message | None = None
        self._used = False

        if self.inventory_entry and self.inventory_entry.get("used_in"):
            self._update_status_from_inventory()

    def build_message(self) -> str:
        if self.status_text:
            return f"{self.base_message}\n\n{self.status_text}"
        return self.base_message

    async def _refresh_message(self) -> None:
        if self.message is not None:
            await self.message.edit(content=self.build_message(), view=self)

    def _disable_all_items(self) -> None:
        for child in self.children:
            child.disabled = True

    async def interaction_check(self, interaction: Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self.buyer.id:
            await interaction.response.send_message(
                "Apenas o comprador pode interagir com esta mensagem.",
                ephemeral=True,
            )
            return False
        return True

    def _update_status_from_inventory(self) -> None:
        if not self.inventory_entry:
            return

        used_in = self.inventory_entry.get("used_in")
        valid_until = self.inventory_entry.get("valid_until")

        if used_in:
            try:
                used_ts = int(used_in.timestamp())
            except (AttributeError, OSError, OverflowError):
                used_ts = None
            if used_ts:
                status_parts = [f"✅ Serviço utilizado em <t:{used_ts}:f>."]
            else:
                status_parts = ["✅ Serviço utilizado."]
            if valid_until:
                try:
                    expiration_ts = int(valid_until.timestamp())
                except (AttributeError, OSError, OverflowError):
                    expiration_ts = None
                if expiration_ts:
                    status_parts.append(
                        f"Válido até <t:{expiration_ts}:f>."
                    )
            else:
                status_parts.append("Sem data de expiração registrada.")
            self.status_text = " ".join(status_parts)

    @ui.button(label="Usar agora", style=ButtonStyle.primary)
    async def use_now(self, interaction: Interaction, _: ui.Button) -> None:  # type: ignore[override]
        if interaction.guild is None:
            await interaction.response.send_message(
                "Não foi possível identificar o servidor para ativar o serviço.",
                ephemeral=True,
            )
            return

        if self._used:
            await interaction.response.send_message(
                "Este serviço já foi utilizado.", ephemeral=True
            )
            return

        try:
            available_entry = ensure_service_inventory_available(
                interaction.guild.id,
                interaction.user,
                self.store_item_id,
            )
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        except Exception:  # pragma: no cover - defensive logging
            logging.exception(
                "Falha ao validar disponibilidade do serviço %s para o usuário %s",
                self.store_item_id,
                interaction.user.id,
            )
            await interaction.response.send_message(
                "Não foi possível verificar a disponibilidade deste serviço. Tente novamente mais tarde.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        self.inventory_entry = available_entry

        used_at = datetime.utcnow()
        valid_until = None
        if self.duration and self.duration > 0:
            valid_until = used_at + timedelta(seconds=int(self.duration))

        rollback_snapshot: Optional[dict] = None

        try:
            usage_update = mark_service_inventory_usage(
                interaction.guild.id,
                interaction.user,
                int(self.item["id"]),
                used_at=used_at,
                valid_until=valid_until,
            )
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception as error:  # pragma: no cover - defensive logging
            logging.exception(
                "Falha ao registrar o uso do serviço %s para o usuário %s",
                self.store_item_id,
                interaction.user.id,
            )
            message = str(error).strip() or (
                "Não foi possível atualizar o status do seu serviço. Tente novamente mais tarde."
            )
            await interaction.followup.send(message, ephemeral=True)
            return

        rollback_snapshot = usage_update.get("rollback_snapshot")
        self._used = True

        try:
            await execute_service_command(
                self.store_item_id,
                interaction=interaction,
                item=self.item,
                inventory_entry=self.inventory_entry,
                used_at=used_at,
                valid_until=valid_until,
            )
        except ServiceCommandError as error:
            try:
                restore_service_inventory_usage(
                    interaction.guild.id,
                    interaction.user,
                    int(self.item["id"]),
                    rollback_snapshot,
                )
            except Exception:  # pragma: no cover - defensive logging
                logging.exception(
                    "Falha ao restaurar inventário após erro no serviço %s",
                    self.store_item_id,
                )
            self._used = False
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:  # pragma: no cover - defensive logging
            try:
                restore_service_inventory_usage(
                    interaction.guild.id,
                    interaction.user,
                    int(self.item["id"]),
                    rollback_snapshot,
                )
            except Exception:  # pragma: no cover - defensive logging
                logging.exception(
                    "Falha ao restaurar inventário após exceção no serviço %s",
                    self.store_item_id,
                )
            self._used = False
            logging.exception(
                "Falha ao executar o comando do serviço %s", self.store_item_id
            )
            await interaction.followup.send(
                "Ocorreu um erro ao executar o serviço. Tente novamente mais tarde.",
                ephemeral=True,
            )
            return

        active_entry = usage_update.get("active_entry")
        if active_entry:
            self.inventory_entry = active_entry
        else:
            self.inventory_entry = None
        self._used = True
        self._disable_all_items()
        self._update_status_from_inventory()
        await self._refresh_message()

        success_message = "Serviço utilizado com sucesso!"
        recorded_valid_until = None
        if self.inventory_entry:
            recorded_valid_until = self.inventory_entry.get("valid_until")
        if recorded_valid_until is None:
            recorded_valid_until = valid_until
        if recorded_valid_until:
            try:
                expiration_ts = int(recorded_valid_until.timestamp())
            except (AttributeError, OSError, OverflowError):
                expiration_ts = None
            if expiration_ts:
                success_message += f" Válido até <t:{expiration_ts}:f>."

        await interaction.followup.send(success_message, ephemeral=True)

    @ui.button(label="Usar depois", style=ButtonStyle.secondary)
    async def use_later(self, interaction: Interaction, _: ui.Button) -> None:  # type: ignore[override]
        if self._used:
            await interaction.response.send_message(
                "Este serviço já foi utilizado.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Tudo bem! Você pode ativar este serviço clicando em 'Usar agora' enquanto esta mensagem estiver disponível.",
            ephemeral=True,
        )

        if not self.status_text:
            self.status_text = (
                "ℹ️ Serviço aguardando uso. Clique em 'Usar agora' quando quiser ativá-lo."
            )
            await self._refresh_message()

    async def on_timeout(self) -> None:
        if not self._used and not self.status_text:
            self.status_text = (
                "⏰ Esta mensagem expirou. Utilize o comando /eco inventario para ativar o serviço depois."
            )
        self._disable_all_items()
        await self._refresh_message()


class UseServiceButton(ui.Button):
    """Botão responsável por acionar o uso de um serviço do inventário."""

    def __init__(self, view: "InventoryView", entry: dict[str, Any]):
        item_name = entry.get("item_name") or f"Item #{entry['store_item_id']}"
        label = f"Usar {item_name[:32]}"
        super().__init__(style=ButtonStyle.primary, label=label)
        self.inventory_view = view
        self.entry = entry

    async def callback(self, interaction: Interaction) -> None:  # type: ignore[override]
        await self.inventory_view.handle_use_service(interaction, self.entry)


class InventoryNavigationButton(ui.Button):
    """Botão de navegação para a visualização de inventário."""

    def __init__(self, view: "InventoryView", *, direction: Literal[-1, 1]):
        label = "Página anterior" if direction < 0 else "Próxima página"
        super().__init__(style=ButtonStyle.secondary, label=label)
        self.inventory_view = view
        self.direction = direction

    async def callback(self, interaction: Interaction) -> None:  # type: ignore[override]
        await self.inventory_view.change_page(interaction, self.direction)


class InventoryView(ui.View):
    """Exibe o inventário do usuário com suporte a paginação e uso de serviços."""

    def __init__(self, interaction: Interaction, inventory: list[dict[str, Any]]):
        super().__init__(timeout=300)
        self.owner = interaction.user
        self.guild = interaction.guild
        self.inventory = inventory
        self.per_page = 5
        self.page = 0
        self.message: Message | None = None
        self.status_message: Optional[str] = None
        self._prev_button = InventoryNavigationButton(self, direction=-1)
        self._next_button = InventoryNavigationButton(self, direction=1)
        self._refresh_components()

    def total_pages(self) -> int:
        return max(1, ceil(len(self.inventory) / self.per_page))

    def _get_page_entries(self) -> list[dict[str, Any]]:
        start = self.page * self.per_page
        end = start + self.per_page
        return self.inventory[start:end]

    def build_embed(self) -> Embed:
        embed = Embed(
            title=f"Inventário de {self.owner.display_name}",
        )
        total_pages = self.total_pages()
        if total_pages > 1:
            embed.set_footer(text=f"Página {self.page + 1} de {total_pages}")

        if self.status_message:
            embed.description = self.status_message

        entries = self._get_page_entries()
        if not entries:
            embed.description = "Seu inventário está vazio no momento."
            return embed

        for entry in entries:
            item_name = entry.get("item_name") or f"Item #{entry['store_item_id']}"
            available_quantity = int(entry.get("available_quantity") or 0)
            active_quantity = int(entry.get("active_quantity") or 0)
            total_quantity = int(
                entry.get("total_quantity") or entry.get("quantity") or 0
            )
            lines: list[str] = []

            if entry.get("is_service"):
                plural_available = "s" if available_quantity != 1 else ""
                plural_active = "s" if active_quantity != 1 else ""
                lines.append(
                    f"Disponível para uso: **{available_quantity}** unidade{plural_available}"
                )
                lines.append(f"Em uso: **{active_quantity}** unidade{plural_active}")

                duration_value = entry.get("duration")
                if duration_value:
                    lines.append(
                        "Duração por uso: "
                        + format_duration_seconds(int(duration_value))
                    )

                active_usages = entry.get("active_usages") or []
                if active_usages:
                    lines.append("Usos ativos:")
                    for usage in active_usages:
                        usage_parts: list[str] = []
                        quantity = int(usage.get("quantity") or 0)
                        used_in = usage.get("used_in")
                        if used_in:
                            try:
                                used_ts = int(used_in.timestamp())
                            except (AttributeError, OSError, OverflowError):
                                used_ts = None
                            if used_ts:
                                usage_parts.append(f"Início: <t:{used_ts}:f>")
                            else:
                                usage_parts.append("Início registrado")
                        if quantity:
                            usage_parts.append(f"Qtd: {quantity}")
                        valid_until = usage.get("valid_until")
                        if valid_until:
                            try:
                                expiration_ts = int(valid_until.timestamp())
                            except (AttributeError, OSError, OverflowError):
                                expiration_ts = None
                            if expiration_ts:
                                usage_parts.append(
                                    f"Válido até: <t:{expiration_ts}:f>"
                                )
                        lines.append(
                            "• " + " — ".join(usage_parts) if usage_parts else "• Em uso"
                        )
                else:
                    lines.append("Nenhum uso em andamento.")
            else:
                plural_total = "s" if total_quantity != 1 else ""
                lines.append(
                    f"Quantidade total: **{total_quantity}** unidade{plural_total}"
                )

            value_text = "\n".join(lines)
            embed.add_field(
                name=f"#{entry['store_item_id']} — {item_name}",
                value=value_text,
                inline=False,
            )

        return embed

    async def _refresh_message(self) -> None:
        if self.message is not None:
            await self.message.edit(embed=self.build_embed(), view=self)

    def _replace_inventory(self, new_inventory: list[dict[str, Any]]) -> None:
        previous_page = self.page
        self.inventory = new_inventory
        total_pages = self.total_pages()
        if previous_page >= total_pages:
            self.page = max(0, total_pages - 1)
        else:
            self.page = previous_page
        self._refresh_components()

    def _refresh_components(self) -> None:
        self.clear_items()

        entries = self._get_page_entries()
        for entry in entries:
            if (
                entry.get("is_service")
                and int(entry.get("available_quantity") or 0) > 0
            ):
                self.add_item(UseServiceButton(self, entry))

        total_pages = self.total_pages()
        self._prev_button.disabled = self.page <= 0
        self._next_button.disabled = self.page >= total_pages - 1

        if total_pages > 1:
            self.add_item(self._prev_button)
            self.add_item(self._next_button)

    async def change_page(
        self, interaction: Interaction, direction: Literal[-1, 1]
    ) -> None:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message(
                "Apenas o dono do inventário pode navegar pelas páginas.",
                ephemeral=True,
            )
            return

        target_page = self.page + direction
        if 0 <= target_page < self.total_pages():
            self.page = target_page
            self._refresh_components()
            await interaction.response.defer(ephemeral=True)
            await self._refresh_message()
        else:
            await interaction.response.defer(ephemeral=True)

    async def handle_use_service(
        self, interaction: Interaction, entry: dict[str, Any]
    ) -> None:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message(
                "Apenas o dono do inventário pode utilizar este serviço.",
                ephemeral=True,
            )
            return

        if self.guild is None or interaction.guild is None:
            await interaction.response.send_message(
                "Não foi possível identificar o servidor para ativar o serviço.",
                ephemeral=True,
            )
            return

        store_item_id = int(entry["store_item_id"])

        available_quantity = int(entry.get("available_quantity") or 0)
        if available_quantity <= 0:
            await interaction.response.send_message(
                "Não há unidades disponíveis deste serviço para serem usadas.",
                ephemeral=True,
            )
            return

        try:
            available_entry = ensure_service_inventory_available(
                interaction.guild.id,
                interaction.user,
                store_item_id,
            )
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        except Exception:  # pragma: no cover - defensive logging
            logging.exception(
                "Falha ao validar disponibilidade do serviço %s para o usuário %s",
                store_item_id,
                interaction.user.id,
            )
            await interaction.response.send_message(
                "Não foi possível verificar a disponibilidade deste serviço. Tente novamente mais tarde.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        entry_data = dict(entry)
        entry_data.update(available_entry)

        try:
            item = get_community_store_item(self.guild.id, store_item_id)
        except ValueError as error:
            metadata = entry_data.get("service_metadata") or {}
            item = {
                "id": store_item_id,
                "item_name": metadata.get("item_name")
                or entry_data.get("item_name")
                or f"Item #{store_item_id}",
                "is_service": True,
                "allow_multiple": entry_data.get("allow_multiple", False),
                "duration": metadata.get("duration") or entry_data.get("duration"),
            }
            missing_error = str(error)
        except Exception:  # pragma: no cover - defensive logging
            logging.exception(
                "Falha ao carregar os dados do serviço %s para o servidor %s",
                store_item_id,
                self.guild.id if self.guild else "desconhecido",
            )
            await interaction.followup.send(
                "Não foi possível carregar os dados deste serviço. Tente novamente mais tarde.",
                ephemeral=True,
            )
            return
        else:
            metadata = entry_data.get("service_metadata") or {}
            if metadata:
                if not item.get("item_name") and metadata.get("item_name"):
                    item["item_name"] = metadata.get("item_name")
                if item.get("duration") is None and metadata.get("duration") is not None:
                    item["duration"] = metadata.get("duration")
            missing_error = None

        used_at = datetime.utcnow()
        duration_value = entry_data.get("duration") or item.get("duration")
        valid_until = None
        if duration_value:
            try:
                duration_seconds = int(duration_value)
            except (TypeError, ValueError):
                duration_seconds = None
            if duration_seconds and duration_seconds > 0:
                valid_until = used_at + timedelta(seconds=duration_seconds)

        rollback_snapshot: Optional[dict] = None

        try:
            usage_update = mark_service_inventory_usage(
                interaction.guild.id,
                interaction.user,
                store_item_id,
                used_at=used_at,
                valid_until=valid_until,
            )
        except ValueError as error:
            message = str(error)
            if missing_error:
                message = f"{missing_error}\n{message}"
            await interaction.followup.send(message, ephemeral=True)
            return
        except Exception as error:  # pragma: no cover - defensive logging
            logging.exception(
                "Falha ao registrar o uso do serviço %s para o usuário %s",
                store_item_id,
                interaction.user.id,
            )
            detailed_message = str(error).strip() or (
                "Não foi possível atualizar o status do seu serviço. Tente novamente mais tarde."
            )
            if missing_error:
                detailed_message = f"{missing_error}\n{detailed_message}"
            await interaction.followup.send(detailed_message, ephemeral=True)
            return

        rollback_snapshot = usage_update.get("rollback_snapshot")

        try:
            await execute_service_command(
                store_item_id,
                interaction=interaction,
                item=item,
                inventory_entry=entry_data,
                used_at=used_at,
                valid_until=valid_until,
            )
        except ServiceCommandError as error:
            message = str(error)
            if missing_error:
                message = f"{missing_error}\n{message}"
            try:
                restore_service_inventory_usage(
                    interaction.guild.id,
                    interaction.user,
                    store_item_id,
                    rollback_snapshot,
                )
            except Exception:  # pragma: no cover - defensive logging
                logging.exception(
                    "Falha ao restaurar inventário após erro no serviço %s",
                    store_item_id,
                )
            await interaction.followup.send(message, ephemeral=True)
            return
        except Exception:  # pragma: no cover - defensive logging
            try:
                restore_service_inventory_usage(
                    interaction.guild.id,
                    interaction.user,
                    store_item_id,
                    rollback_snapshot,
                )
            except Exception:  # pragma: no cover - defensive logging
                logging.exception(
                    "Falha ao restaurar inventário após exceção no serviço %s",
                    store_item_id,
                )
            logging.exception(
                "Falha ao executar o comando do serviço %s", store_item_id
            )
            await interaction.followup.send(
                "Ocorreu um erro ao executar o serviço. Tente novamente mais tarde.",
                ephemeral=True,
            )
            return

        active_entry = usage_update.get("active_entry")

        try:
            updated_inventory = get_user_inventory_items(
                interaction.guild.id, interaction.user
            )
        except Exception:  # pragma: no cover - defensive logging
            logging.exception(
                "Falha ao recarregar o inventário após uso do serviço %s",
                store_item_id,
            )
            updated_inventory = None

        if updated_inventory is not None:
            self._replace_inventory(updated_inventory)
            await self._refresh_message()

        success_message = "Serviço utilizado com sucesso!"
        recorded_valid_until = None
        if active_entry:
            recorded_valid_until = active_entry.get("valid_until")
        if recorded_valid_until is None:
            recorded_valid_until = valid_until
        if recorded_valid_until:
            try:
                expiration_ts = int(recorded_valid_until.timestamp())
            except (AttributeError, OSError, OverflowError):
                expiration_ts = None
            if expiration_ts:
                success_message += f" Válido até <t:{expiration_ts}:f>."

        await interaction.followup.send(success_message, ephemeral=True)

    async def interaction_check(self, interaction: Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message(
                "Apenas o dono do inventário pode interagir com esta mensagem.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        self.clear_items()
        await self._refresh_message()


class CoinRainView(ui.View):
    """Interactive view that distributes coins thrown by a member."""

    def __init__(
        self, interaction: Interaction, amount: int, *, timeout: float | None = 120
    ):
        super().__init__(timeout=timeout)
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.owner = interaction.user
        self.total = amount
        self.remaining = amount
        self.message: Message | None = None
        self._claims: dict[int, int] = {}
        self._claim_responses: dict[int, Message] = {}
        self._lock = asyncio.Lock()
        self._finished = False
        self._refunded_amount = 0
        self._summary_lines: list[str] | None = None
        self.owner_refund_text: str | None = None

    def build_message(self) -> str:
        message_lines = [
            f"{self.owner.mention} jogou **{self.total}** moedas para o alto!"
        ]

        if self._finished and self._refunded_amount > 0:
            message_lines.append(
                "A chuva terminou e as moedas restantes foram estornadas para o dono. ✅"
            )
        elif self.remaining > 0 and not self._finished:
            message_lines.append(
                f"Restam **{self.remaining}** moedas no chão. Clique no botão abaixo para pegar."
            )
        else:
            message_lines.append("Todas as moedas foram coletadas! ✅")

        if self.owner_refund_text and not self._finished:
            message_lines.append(self.owner_refund_text)

        if self._summary_lines:
            message_lines.append("")
            message_lines.extend(self._summary_lines)

        return "\n".join(message_lines)

    async def _refresh_message(self) -> None:
        if self.message is not None:
            await self.message.edit(content=self.build_message(), view=self)

    def _build_summary_lines(self, *, timed_out: bool) -> list[str]:
        summary_lines: list[str] = [
            f"A chuva de moedas de {self.owner.mention} terminou!"
        ]

        if self._claims:
            sorted_claims = sorted(
                self._claims.items(), key=lambda item: item[1], reverse=True
            )
            for user_id, amount in sorted_claims:
                member = self.guild.get_member(user_id) if self.guild is not None else None
                mention = member.mention if member is not None else f"<@{user_id}>"
                plural = "s" if amount > 1 else ""
                summary_lines.append(f"{mention} — **{amount}** moeda{plural}")
        else:
            summary_lines.append(
                "As moedas ficaram no chão, mas ninguém pegou nenhuma."
            )

        if timed_out:
            summary_lines.append("O tempo da chuva terminou.")
        if self._refunded_amount > 0:
            summary_lines.append(
                f"{self.owner.mention} recebeu o estorno de **{self._refunded_amount}** moedas não coletadas."
            )

        return summary_lines

    async def _finalize_distribution(self, *, timed_out: bool = False) -> None:
        async with self._lock:
            if self._finished:
                return
            self._finished = True
            self.clear_items()
            self._summary_lines = self._build_summary_lines(timed_out=timed_out)
        await self._refresh_message()
        self.stop()

    @ui.button(label="Pegar moeda", style=ButtonStyle.success, emoji="🪙")
    async def claim_coin(self, interaction: Interaction, button: ui.Button) -> None:  # type: ignore[override]
        if interaction.guild is None or self.guild is None or interaction.guild.id != self.guild.id:
            await interaction.response.send_message(
                "Você não pode participar desta chuva de moedas.", ephemeral=True
            )
            return

        async with self._lock:
            if self._finished or self.remaining <= 0:
                await interaction.response.send_message(
                    "Não há mais moedas disponíveis para coletar.", ephemeral=True
                )
                return

            self.remaining -= 1
            current_remaining = self.remaining
            new_amount = self._claims.get(interaction.user.id, 0) + 1
            self._claims[interaction.user.id] = new_amount

        try:
            adjust_user_economy_balance(interaction.guild.id, interaction.user, 1)
        except ValueError as error:
            async with self._lock:
                updated_amount = self._claims.get(interaction.user.id, 0) - 1
                if updated_amount <= 0:
                    self._claims.pop(interaction.user.id, None)
                else:
                    self._claims[interaction.user.id] = updated_amount
                self.remaining += 1
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        if new_amount == 1:
            message = "Você pegou **1** moeda!"
        else:
            plural = "s" if new_amount > 1 else ""
            message = (
                f"Você já pegou **{new_amount}** moeda{plural} nesta chuva!"
            )

        previous_response = self._claim_responses.get(interaction.user.id)

        if previous_response is None:
            await interaction.response.send_message(message, ephemeral=True)
            try:
                self._claim_responses[interaction.user.id] = (
                    await interaction.original_response()
                )
            except Exception:
                self._claim_responses.pop(interaction.user.id, None)
        else:
            await interaction.response.defer(ephemeral=True)
            try:
                await previous_response.edit(content=message)
            except Exception:
                new_response = await interaction.followup.send(
                    message, ephemeral=True
                )
                self._claim_responses[interaction.user.id] = new_response

        await self._refresh_message()

        if current_remaining == 0:
            await self._finalize_distribution()

    async def on_timeout(self) -> None:
        refund_amount = 0

        async with self._lock:
            if self._finished:
                return
            refund_amount = self.remaining
            if refund_amount > 0:
                self._refunded_amount = refund_amount
                self.remaining = 0

        if refund_amount > 0 and self.guild is not None:
            adjust_user_economy_balance(self.guild.id, self.owner, refund_amount)
            self.owner_refund_text = (
                f"{self.owner.mention} recebeu o estorno de **{refund_amount}** moedas não coletadas."
            )

        await self._finalize_distribution(timed_out=True)


class BetRequestView(ui.View):
    def __init__(
        self,
        *,
        inviter: Member,
        invited: Member,
        amount: int | None,
        on_accept,
        on_decline,
        origin_interaction: Interaction,
        forced_no_bet: bool = False,
    ) -> None:
        super().__init__(timeout=120)
        self.inviter = inviter
        self.invited = invited
        self.amount = amount
        self.on_accept = on_accept
        self.on_decline = on_decline
        self.origin_interaction = origin_interaction
        self.forced_no_bet = forced_no_bet
        self.message: Message | None = None
        self._handled = False

    async def interaction_check(self, interaction: Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self.invited.id:
            await interaction.response.send_message(
                "Apenas o membro convidado pode responder a esta aposta.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                logging.debug("Falha ao atualizar mensagem expirada de aposta", exc_info=True)

    @ui.button(label="Aceitar", style=ButtonStyle.success)
    async def accept(self, interaction: Interaction, _: ui.Button) -> None:  # type: ignore[override]
        if self._handled:
            await interaction.response.send_message(
                "Esta aposta já foi processada.", ephemeral=True
            )
            return
        self._handled = True
        await self.on_accept(interaction, self)

    @ui.button(label="Recusar", style=ButtonStyle.danger)
    async def decline(self, interaction: Interaction, _: ui.Button) -> None:  # type: ignore[override]
        if self._handled:
            await interaction.response.send_message(
                "Esta aposta já foi processada.", ephemeral=True
            )
            return
        self._handled = True
        await self.on_decline(interaction, self)


class EconomyErrorNotifier:
    async def _notify_economy_error(
        self,
        ctx: Interaction,
        error: Exception,
        *,
        use_followup: bool,
        command_params: Optional[dict[str, Any]] = None,
        user_message: Optional[str] = None,
        ephemeral: bool = True,
    ) -> None:
        await notify_owner_and_user(
            ctx,
            error,
            user_message
            or "Não foi possível concluir sua solicitação no momento. A equipe foi notificada.",
            use_followup=use_followup,
            command_params=command_params,
            ephemeral=ephemeral,
        )


class EconomyCog(EconomyErrorNotifier, commands.GroupCog, name="eco"):
    ADULT_BET_WARNING = "🛡️ Apostas com valor são 18+. Rodamos a versão sem aposta pra você. 😉"

    @staticmethod
    def _build_adult_warning(restrictions: list[str]) -> str:
        """Create a single underage warning message with unique restriction reasons."""

        unique_messages = list(dict.fromkeys(restrictions))
        unique_messages.append(EconomyCog.ADULT_BET_WARNING)
        return "\n".join(unique_messages)

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    @staticmethod
    def _validate_bet_value(valor: int) -> Optional[str]:
        if valor < 5 or valor > 100:
            return "O valor deve estar entre 5 e 100 moedas."
        if valor % 5 != 0:
            return "O valor precisa ser múltiplo de 5."
        return None

    @staticmethod
    def _determine_bet_multiplier(guild_id: int) -> int:
        odds = get_economy_config(guild_id)
        tiers = [
            (3, odds.get("bet_odds_3x", 0)),
            (2, odds.get("bet_odds_2x", 0)),
            (1, odds.get("bet_odds_1x", 0)),
        ]
        roll = random.uniform(0, 100)
        cumulative = 0.0
        for multiplier, chance in tiers:
            cumulative += max(0.0, float(chance))
            if roll <= cumulative:
                return multiplier
        return 0

    async def _animate_slot_machine(
        self, message: Message, embed: Embed, emojis: list[str]
    ) -> None:
        embed.set_field_at(0, name="Roleta", value="⬛ ⬛ ⬛ ⬛", inline=False)
        await message.edit(embed=embed)

        await asyncio.sleep(4)

        current = ["⬛", "⬛", "⬛", "⬛"]
        for index, emoji in enumerate(emojis):
            current[index] = emoji
            embed.set_field_at(0, name="Roleta", value=" ".join(current), inline=False)
            await message.edit(embed=embed)
            if index < len(emojis) - 1:
                await asyncio.sleep(2)

    async def _handle_duel_acceptance(
        self,
        interaction: Interaction,
        view: BetRequestView,
        amount: int | None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "Não foi possível identificar o servidor desta aposta.", ephemeral=True
            )
            return

        initiator = view.inviter
        challenged = view.invited

        adult_restriction_triggered = view.forced_no_bet
        if amount is not None and not adult_restriction_triggered:
            adult_restrictions = []
            if not _has_adult_role(initiator):
                adult_restrictions.append(
                    "Você precisa ser maior de idade para apostar contra outro membro."
                )
            if not _has_adult_role(challenged):
                adult_restrictions.append(
                    f"{challenged.mention} precisa ser maior de idade para participar da aposta."
                )

            if adult_restrictions:
                adult_restriction_triggered = True
                amount = None
                warning_message = self._build_adult_warning(adult_restrictions)
                await view.origin_interaction.followup.send(
                    warning_message, ephemeral=True
                )

        initiator_balance = challenged_balance = None
        if amount is not None:
            initiator_balance = get_user_economy_balance(guild.id, initiator)
            challenged_balance = get_user_economy_balance(guild.id, challenged)
            if initiator_balance < amount or challenged_balance < amount:
                await interaction.followup.send(
                    "Um dos participantes não possui saldo suficiente para esta aposta.",
                    ephemeral=True,
                )
                return

            initiator_debited = False
            try:
                adjust_user_economy_balance(guild.id, initiator, -amount)
                initiator_debited = True
                adjust_user_economy_balance(guild.id, challenged, -amount)
            except ValueError as error:
                if initiator_debited:
                    adjust_user_economy_balance(guild.id, initiator, amount)
                await self._notify_economy_error(
                    interaction,
                    error,
                    use_followup=True,
                    command_params={
                        "desafiante": initiator.id,
                        "desafiado": challenged.id,
                        "valor": amount,
                    },
                    user_message=str(error),
                )
                return

        if view.message:
            try:
                await view.message.delete()
            except Exception:
                logging.debug("Não foi possível remover mensagem de convite de aposta", exc_info=True)

        coin_result = random.choice(["cara", "coroa"])
        winner = initiator if coin_result == "cara" else challenged
        winnings = 0
        if amount is not None:
            winnings = amount * 2
            adjust_user_economy_balance(guild.id, winner, winnings)

        spinning_embed = Embed(title="Jogo de moeda", colour=0xF1C40F)
        spinning_embed.description = "Jogando moeda..."
        status_message = await interaction.followup.send(embed=spinning_embed)

        await asyncio.sleep(4)

        result_embed = Embed(title="Resultado do jogo", colour=0x2ECC71)
        result_embed.description = (
            f"🪙 Resultado: **{coin_result}**.\n" f"Vencedor: {winner.mention}"
        )

        await status_message.edit(embed=result_embed)

        if amount is not None:
            initiator_balance = get_user_economy_balance(guild.id, initiator)
            challenged_balance = get_user_economy_balance(guild.id, challenged)
            result_embeds = []
            for member, balance in (
                (initiator, initiator_balance),
                (challenged, challenged_balance),
            ):
                net_result = amount if member == winner else -amount
                colour = 0x2ECC71 if net_result > 0 else 0xE74C3C
                description = (
                    f"Você ganhou **{net_result}** moedas."
                    if net_result > 0
                    else f"Você perdeu **{abs(net_result)}** moedas."
                )
                member_embed = Embed(
                    title="Resultado da aposta",
                    description=description,
                    colour=colour,
                )
                member_embed.add_field(
                    name="Saldo atual",
                    value=f"**{balance}** moedas.",
                    inline=False,
                )
                result_embeds.append(member_embed)

            await interaction.followup.send(embeds=result_embeds, ephemeral=True)
            if view.origin_interaction.user.id != interaction.user.id:
                await view.origin_interaction.followup.send(
                    embeds=result_embeds, ephemeral=True
                )
        view.stop()

    async def _handle_duel_decline(
        self, interaction: Interaction, view: BetRequestView
    ) -> None:
        await interaction.response.send_message(
            "A aposta foi recusada.", ephemeral=True
        )
        if view.message:
            try:
                await view.message.edit(view=None)
            except Exception:
                logging.debug("Falha ao atualizar mensagem de aposta recusada", exc_info=True)
        view.stop()

    @app_commands.command(
        name="apostar", description="Aposte suas moedas sozinho ou contra outro membro."
    )
    @app_commands.describe(
        valor="Valor opcional a ser apostado (múltiplos de 5 até 100).",
        membro="Membro opcional para apostar contra.",
    )
    async def apostar(
        self,
        interaction: Interaction,
        valor: int | None = None,
        membro: Member | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return

        validation_error = None
        if valor is not None:
            validation_error = self._validate_bet_value(valor)
        if validation_error:
            await interaction.response.send_message(validation_error, ephemeral=True)
            return

        adult_messages: list[str] = []
        if valor is not None and not _has_adult_role(interaction.user):
            adult_messages.append(
                "Você precisa ser maior de idade para realizar apostas."
            )
            valor = None

        if valor is not None and membro and not _has_adult_role(membro):
            adult_messages.append(
                f"{membro.mention} precisa ser maior de idade para participar da aposta."
            )
            valor = None

        adult_restriction_triggered = bool(adult_messages)
        use_followup = False
        if adult_restriction_triggered:
            warning_message = self._build_adult_warning(adult_messages)
            await interaction.response.send_message(warning_message, ephemeral=True)
            use_followup = True

        async def send_reply(*args, **kwargs):
            if use_followup:
                await interaction.followup.send(*args, **kwargs)
            else:
                await interaction.response.send_message(*args, **kwargs)

        async def send_message_get_response(*args, **kwargs):
            if use_followup:
                return await interaction.followup.send(*args, wait=True, **kwargs)
            await interaction.response.send_message(*args, **kwargs)
            return await interaction.original_response()

        if membro and membro.id == interaction.user.id:
            await send_reply(
                "Você não pode desafiar a si mesmo. Use o comando sem mencionar ninguém para apostar sozinho.",
                ephemeral=True,
            )
            return

        if membro:
            initiator_balance = target_balance = None
            if valor is not None:
                initiator_balance = get_user_economy_balance(
                    interaction.guild.id, interaction.user
                )
                target_balance = get_user_economy_balance(interaction.guild.id, membro)

                if initiator_balance < valor:
                    await send_reply(
                        "Você não possui saldo suficiente para essa aposta.",
                        ephemeral=True,
                    )
                    return

                if target_balance < valor:
                    await send_reply(
                        f"{membro.mention} não possui saldo suficiente para essa aposta.",
                        ephemeral=True,
                    )
                    return

            async def on_accept(accept_interaction: Interaction, view: BetRequestView):
                await self._handle_duel_acceptance(accept_interaction, view, valor)

            async def on_decline(decline_interaction: Interaction, view: BetRequestView):
                await self._handle_duel_decline(decline_interaction, view)

            view = BetRequestView(
                inviter=interaction.user,
                invited=membro,
                amount=valor,
                on_accept=on_accept,
                on_decline=on_decline,
                origin_interaction=interaction,
                forced_no_bet=adult_restriction_triggered,
            )

            invite_embed = Embed(
                title="Convite de jogo" if valor is None else "Aposta pendente",
                colour=0xF1C40F,
            )
            invite_embed.description = (
                f"{interaction.user.mention} convidou {membro.mention} para jogar cara ou coroa sem aposta.\n"
                "Somente o membro mencionado pode aceitar."
                if valor is None
                else (
                    f"{interaction.user.mention} convidou {membro.mention} para uma aposta de **{valor}** moedas.\n"
                    "Somente o membro mencionado pode aceitar."
                )
            )

            view.message = await send_message_get_response(embed=invite_embed, view=view)
            return

        balance = get_user_economy_balance(interaction.guild.id, interaction.user)
        if valor is not None and balance < valor:
            await send_reply(
                "Você não possui saldo suficiente para essa aposta.",
                ephemeral=True,
            )
            return

        if valor is not None:
            try:
                adjust_user_economy_balance(
                    interaction.guild.id, interaction.user, -valor
                )
            except ValueError as error:
                await self._notify_economy_error(
                    interaction,
                    error,
                    use_followup=False,
                    command_params={"valor": valor},
                    user_message=str(error),
                )
                return

        multiplier = self._determine_bet_multiplier(interaction.guild.id)
        match_length = 4 if multiplier == 3 else 3 if multiplier == 2 else 2 if multiplier == 1 else 0
        slot_emojis = _shuffle_emojis_with_matches(match_length)

        embed = Embed(title="Jogo na roleta", colour=0x9B59B6)
        embed.description = "Girando roleta..."
        embed.add_field(name="Roleta", value="⬛ ⬛ ⬛ ⬛", inline=False)

        message = await send_message_get_response(embed=embed)

        await self._animate_slot_machine(message, embed, slot_emojis)

        bet_amount = valor or 0
        payout = bet_amount * multiplier
        colour = 0xE74C3C
        if payout > 0:
            colour = 0x2ECC71
            if valor is not None:
                adjust_user_economy_balance(
                    interaction.guild.id, interaction.user, payout
                )
            win_text = {
                1: "Parabéns! você conseguiu uma dupla (1x).",
                2: "Parabéns! você conseguiu um trio (2x)!",
                3: "Sensacional! você conseguiu um full (3x)!",
            }.get(multiplier, "Parabéns! você ganhou!")
            result_text = f"🎉 {win_text}"
        else:
            result_text = "💔 Você perdeu."

        embed.colour = colour
        embed.description = result_text
        embed.set_field_at(0, name="Roleta", value=" ".join(slot_emojis), inline=False)

        await message.edit(embed=embed)

        if valor is not None:
            new_balance = get_user_economy_balance(
                interaction.guild.id, interaction.user
            )
            net_result = payout - valor
            await interaction.followup.send(
                (
                    f"Valor apostado: **{valor}** moedas.\n"
                    f"Multiplicador: {multiplier}x.\n"
                    f"Resultado: {'+' if net_result >= 0 else ''}{net_result} moedas.\n"
                    f"Saldo atual: **{new_balance}** moedas."
                ),
                ephemeral=True,
            )

    @app_commands.command(name="saldo", description="Exibe o saldo registrado na economia do servidor.")
    @app_commands.describe(
        membro="Informe para consultar o saldo de outro membro (somente equipe).",
    )
    async def saldo(self, interaction: Interaction, membro: Member | None = None):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Este comando só pode ser usado dentro de um servidor."
            )
            return

        target = membro or interaction.user
        if (
            membro
            and membro.id != interaction.user.id
            and not interaction.user.guild_permissions.manage_guild
        ):
            await interaction.response.send_message(
                "Somente a equipe pode consultar o saldo de outros membros."
            )
            return

        await interaction.response.defer()
        balance = get_user_economy_balance(interaction.guild.id, target)
        if target.id == interaction.user.id:
            message = f"Seu saldo atual é de **{balance}** moedas."
        else:
            message = f"O saldo de {target.mention} é de **{balance}** moedas."
        await interaction.followup.send(message)

    @app_commands.command(name="transferir", description="Transfere saldo para outro membro.")
    @app_commands.describe(membro="Membro que receberá o saldo.", valor="Valor a ser transferido.")
    async def transferir(self, interaction: Interaction, membro: Member, valor: int):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Este comando só pode ser usado dentro de um servidor."
            )
            return

        if membro.id == interaction.user.id:
            await interaction.response.send_message(
                "Você não pode transferir saldo para você mesmo."
            )
            return

        if valor <= 0:
            await interaction.response.send_message(
                "Informe um valor positivo para transferir."
            )
            return

        await interaction.response.defer()
        try:
            novo_saldo_origem, _ = transfer_user_economy_balance(
                interaction.guild.id,
                interaction.user,
                membro,
                valor,
            )
        except ValueError as error:
            await self._notify_economy_error(
                interaction,
                error,
                use_followup=True,
                command_params={"membro": membro.id, "valor": valor},
                user_message=str(error),
            )
            return

        await interaction.followup.send(
            f"Transferência concluída! Você enviou **{valor}** moedas para {membro.mention}. "
            f"Seu novo saldo é **{novo_saldo_origem}** moedas.",
        )

    @app_commands.command(name="loja", description="Exibe os itens disponíveis na loja da comunidade.")
    @app_commands.describe(
        item_id="Identificador do item na loja (opcional).",
        mostrar_indisponiveis="Se marcado, também exibe itens indisponíveis.",
    )
    async def loja(
        self,
        interaction: Interaction,
        item_id: Optional[int] = None,
        mostrar_indisponiveis: bool = False,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            if item_id is not None:
                item = get_community_store_item(interaction.guild.id, item_id)
                items = [item]
            else:
                items = fetch_community_store_items(interaction.guild.id)
        except ValueError as error:
            await self._notify_economy_error(
                interaction,
                error,
                use_followup=True,
                command_params={
                    "item_id": item_id,
                    "mostrar_indisponiveis": mostrar_indisponiveis,
                },
                user_message=str(error),
            )
            return

        if not items:
            await interaction.followup.send(
                "Nenhum item está disponível na loja no momento.",
                ephemeral=True,
            )
            return

        def _is_item_available(store_item: dict) -> bool:
            if store_item["is_service"]:
                return True
            available_value = store_item.get("quantity_available")
            if available_value is None:
                return True
            return int(available_value) > 0

        if item_id is None:
            if not mostrar_indisponiveis:
                items = [item for item in items if _is_item_available(item)]
            else:
                available_items = [item for item in items if _is_item_available(item)]
                unavailable_items = [
                    item for item in items if not _is_item_available(item)
                ]
                items = available_items + unavailable_items

        if not items:
            await interaction.followup.send(
                "Nenhum item está disponível na loja no momento.",
                ephemeral=True,
            )
            return

        embed = Embed(
            title="Loja da Comunidade",
            description=(
                "Confira os itens atualmente disponíveis para compra."
                if item_id is None
                else f"Detalhes do item #{item_id}."
            ),
        )

        first_item_image: Optional[str] = None
        if item_id is not None and items:
            image_url = items[0].get("item_image_url")
            if isinstance(image_url, str) and image_url.strip():
                first_item_image = image_url.strip()

        for item in items:
            price = int(item["item_price"])
            if item["is_service"]:
                quantity_text = "Serviço comunitário"
            else:
                total_value = item.get("quantity_total")
                available_value = item.get("quantity_available")
                if total_value is None:
                    if available_value is None:
                        quantity_text = "Disponibilidade: ilimitada"
                    else:
                        quantity_text = f"Disponível: {int(available_value)}"
                else:
                    available = int(available_value or 0)
                    total = int(total_value)
                    quantity_text = f"Disponível: {available}/{total}"

            owner_text = "Vendido pela comunidade"
            if item["owner_user_id"]:
                owner_name = item.get("owner_display_name") or "Membro da comunidade"
                owner_text = f"Vendido por: {owner_name}"

            details = [f"Preço: **{price}** moedas", quantity_text, owner_text]
            if item["is_service"]:
                duration_value = item.get("duration")
                if duration_value:
                    details.append(
                        "Duração após uso: "
                        + format_duration_seconds(int(duration_value))
                    )
            else:
                allow_multiple_text = (
                    "Permite múltiplas unidades"
                    if item["allow_multiple"]
                    else "Apenas uma por usuário"
                )
                details.append(allow_multiple_text)

            embed.add_field(
                name=f"#{item['id']} — {item['item_name']}",
                value="\n".join(details),
                inline=False,
            )

        if first_item_image:
            embed.set_image(url=first_item_image)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="comprar",
        description="Compra um item da loja da comunidade utilizando suas moedas.",
    )
    @app_commands.describe(
        item_id="Identificador do item na loja.",
        quantidade="Quantidade desejada (1 para serviços).",
    )
    async def comprar(
        self, interaction: Interaction, item_id: int, quantidade: Optional[int] = 1
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return

        quantidade = quantidade or 1

        await interaction.response.defer(ephemeral=True)

        try:
            result = purchase_community_store_item(
                interaction.guild.id, interaction.user, item_id, quantidade
            )
        except ValueError as error:
            await self._notify_economy_error(
                interaction,
                error,
                use_followup=True,
                command_params={"item_id": item_id, "quantidade": quantidade},
                user_message=str(error),
            )
            return

        item = result["item"]
        bought_quantity = result["quantity"]
        new_balance = result["buyer_balance"]

        item_name = item["item_name"]
        price = int(item["item_price"])
        total_spent = price * bought_quantity

        if item["is_service"]:
            inventory_entry = result.get("inventory_entry")
            service_duration = result.get("service_duration") or item.get("duration")
            store_item_id = int(item["id"])

            if not has_service_command(store_item_id):
                await interaction.followup.send(
                    "Este serviço não possui um comportamento implementado no momento. A compra foi cancelada.",
                    ephemeral=True,
                )
                return

            if not inventory_entry:
                await interaction.followup.send(
                    (
                        "O serviço foi comprado, porém não foi possível registrá-lo no seu inventário. "
                        "Entre em contato com a equipe para verificar a situação."
                    ),
                    ephemeral=True,
                )
                return

            base_message = (
                f"Compra concluída! Você adquiriu **{item_name}** por **{total_spent}** moedas.\n"
                "O serviço está disponível no seu inventário. Deseja usá-lo agora?\n"
                f"Seu novo saldo é **{new_balance}** moedas."
            )
            if service_duration:
                base_message += (
                    "\nDuração após uso: "
                    + format_duration_seconds(int(service_duration))
                    + "."
                )

            view = ServicePurchaseView(
                interaction,
                item=item,
                inventory_entry=inventory_entry,
                store_item_id=store_item_id,
                duration=int(service_duration) if service_duration else None,
                message_content=base_message,
            )

            message = await interaction.followup.send(
                view.build_message(), view=view, ephemeral=True
            )
            view.message = message
            return
        else:
            plural = "s" if bought_quantity > 1 else ""
            quantity_text = (
                f"Você recebeu **{bought_quantity}** unidade{plural} do item."
            )

        await interaction.followup.send(
            (
                f"Compra concluída! Você adquiriu **{item_name}** por **{total_spent}** moedas.\n"
                f"{quantity_text}\nSeu novo saldo é **{new_balance}** moedas."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="inventario",
        description="Mostra os itens presentes no seu inventário.",
    )
    async def inventario(self, interaction: Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            inventory = get_user_inventory_items(
                interaction.guild.id, interaction.user
            )
        except ValueError as error:
            await self._notify_economy_error(
                interaction,
                error,
                use_followup=True,
                command_params={"membro": interaction.user.id},
                user_message=str(error),
            )
            return

        if not inventory:
            await interaction.followup.send(
                "Seu inventário está vazio no momento.",
                ephemeral=True,
            )
            return

        view = InventoryView(interaction, inventory)
        message = await interaction.followup.send(
            embed=view.build_embed(),
            view=view,
            ephemeral=True,
        )
        view.message = message

    @app_commands.command(
        name="chuva",
        description="Joga moedas no chão para que outros membros possam coletá-las.",
    )
    @app_commands.describe(valor="Quantidade de moedas que será jogada no chão.")
    async def chuva(self, interaction: Interaction, valor: int):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return

        if valor <= 0:
            await interaction.response.send_message(
                "Informe um valor maior que zero para jogar moedas.",
                ephemeral=True,
            )
            return

        try:
            novo_saldo = adjust_user_economy_balance(
                interaction.guild.id, interaction.user, -valor
            )
        except ValueError:
            saldo_atual = get_user_economy_balance(interaction.guild.id, interaction.user)
            await interaction.response.send_message(
                (
                    "Você não possui moedas suficientes para jogar essa quantidade. "
                    f"Seu saldo atual é de **{saldo_atual}** moedas."
                ),
                ephemeral=True,
            )
            return

        view = CoinRainView(interaction, valor)
        await interaction.response.send_message(view.build_message(), view=view)
        view.message = await interaction.original_response()
        await interaction.followup.send(
            f"Saldo restante: **{novo_saldo}** moedas.",
            ephemeral=True,
        )


class EconomyAdminCog(EconomyErrorNotifier, commands.GroupCog, name="admin-eco"):
    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    @app_commands.command(
        name="set-saldo",
        description="Define ou adiciona saldo ao registro de um membro.",
    )
    @app_commands.describe(
        membro="Membro que terá o saldo alterado.",
        valor="Valor utilizado na operação.",
        operacao="Escolha se o valor será definido ou somado ao saldo atual.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_set_saldo(
        self,
        interaction: Interaction,
        membro: Member,
        valor: int,
        operacao: Literal["definir", "adicionar"] = "definir",
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return

        if valor < 0:
            await interaction.response.send_message(
                "Informe um valor maior ou igual a zero.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            if operacao == "adicionar":
                novo_saldo = adjust_user_economy_balance(interaction.guild.id, membro, valor)
                feedback = (
                    f"Saldo atualizado! {membro.mention} recebeu **{valor}** moedas e agora possui "
                    f"**{novo_saldo}** moedas."
                )
            else:
                novo_saldo = set_user_economy_balance(interaction.guild.id, membro, valor)
                feedback = (
                    f"Saldo definido! {membro.mention} agora possui exatamente **{novo_saldo}** moedas."
                )
        except ValueError as error:
            await self._notify_economy_error(
                interaction,
                error,
                use_followup=True,
                command_params={
                    "membro": membro.id,
                    "valor": valor,
                    "operacao": operacao,
                },
                user_message=str(error),
            )
            return

        await interaction.followup.send(feedback, ephemeral=True)

    @app_commands.command(
        name="loja-adicionar",
        description="Adiciona um item à loja da comunidade.",
    )
    @app_commands.describe(
        nome="Nome exibido na loja.",
        preco="Preço do item em moedas.",
        quantidade="Quantidade total disponível (quando aplicável).",
        quantidade_disponivel="Quantidade inicialmente disponível (opcional).",
        permitir_multiplos="Permite que usuários comprem múltiplas unidades.",
        servico="Indica que o item é um serviço da comunidade.",
        duracao_servico="Duração do serviço em segundos após o uso.",
        vendedor="Membro responsável pelo item (para vendas de usuários).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_loja_adicionar(
        self,
        interaction: Interaction,
        nome: str,
        preco: int,
        quantidade: Optional[int] = None,
        quantidade_disponivel: Optional[int] = None,
        permitir_multiplos: bool = False,
        servico: bool = False,
        duracao_servico: Optional[int] = None,
        vendedor: Optional[Member] = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return

        if servico and vendedor is not None:
            await interaction.response.send_message(
                "Serviços não possuem vendedor específico.",
                ephemeral=True,
            )
            return

        if servico and (duracao_servico is None or duracao_servico <= 0):
            await interaction.response.send_message(
                "Informe a duração do serviço em segundos ao cadastrar um serviço.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            item = create_community_store_item(
                interaction.guild.id,
                item_name=nome,
                item_price=preco,
                quantity_total=quantidade,
                quantity_available=quantidade_disponivel,
                allow_multiple=permitir_multiplos,
                is_service=servico,
                duration=duracao_servico,
                owner=vendedor,
            )
        except ValueError as error:
            await self._notify_economy_error(
                interaction,
                error,
                use_followup=True,
                command_params={
                    "nome": nome,
                    "preco": preco,
                    "quantidade": quantidade,
                    "quantidade_disponivel": quantidade_disponivel,
                    "permitir_multiplos": permitir_multiplos,
                    "servico": servico,
                    "duracao_servico": duracao_servico,
                    "vendedor": vendedor.id if vendedor else None,
                },
                user_message=str(error),
            )
            return

        detalhes = [f"Preço: **{item['item_price']}** moedas"]
        if item["is_service"]:
            detalhes.append("Tipo: Serviço comunitário")
            if item.get("duration"):
                detalhes.append(
                    "Duração após uso: "
                    + format_duration_seconds(int(item["duration"]))
                )
        else:
            total = item.get("quantity_total")
            available = item.get("quantity_available")
            if total is None:
                if available is None:
                    detalhes.append("Quantidade disponível: Ilimitada")
                else:
                    detalhes.append(f"Quantidade disponível: {int(available)}")
            else:
                detalhes.append(
                    f"Quantidade total: {int(total)} (disponível: {int(available or 0)})"
                )
            detalhes.append(
                "Permite múltiplas unidades"
                if item["allow_multiple"]
                else "Apenas uma por usuário"
            )

        await interaction.followup.send(
            (
                f"Item **{item['item_name']}** adicionado com sucesso à loja (ID `{item['id']}`).\n"
                + "\n".join(detalhes)
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="loja-editar",
        description="Edita os dados de um item existente na loja.",
    )
    @app_commands.describe(
        item_id="Identificador do item na loja.",
        nome="Novo nome para o item.",
        preco="Novo preço em moedas.",
        quantidade="Nova quantidade total disponível.",
        permitir_multiplos="Atualiza se o item permite múltiplas unidades.",
        servico="Atualiza o item para serviço comunitário.",
        duracao_servico="Atualiza a duração do serviço em segundos.",
        vendedor="Define um novo vendedor para o item.",
        remover_vendedor="Remove o vendedor associado ao item.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_loja_editar(
        self,
        interaction: Interaction,
        item_id: int,
        nome: Optional[str] = None,
        preco: Optional[int] = None,
        quantidade: Optional[int] = None,
        permitir_multiplos: Optional[bool] = None,
        servico: Optional[bool] = None,
        duracao_servico: Optional[int] = None,
        vendedor: Optional[Member] = None,
        remover_vendedor: bool = False,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return

        if remover_vendedor and vendedor is not None:
            await interaction.response.send_message(
                "Informe apenas vendedor ou remover_vendedor, não ambos.",
                ephemeral=True,
            )
            return

        if servico and vendedor is not None:
            await interaction.response.send_message(
                "Serviços não podem ter vendedor associado.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            item = update_community_store_item(
                interaction.guild.id,
                item_id,
                item_name=nome,
                item_price=preco,
                quantity_total=quantidade,
                allow_multiple=permitir_multiplos,
                is_service=servico,
                duration=duracao_servico,
                owner=(False if remover_vendedor or servico else vendedor),
            )
        except ValueError as error:
            await self._notify_economy_error(
                interaction,
                error,
                use_followup=True,
                command_params={
                    "item_id": item_id,
                    "nome": nome,
                    "preco": preco,
                    "quantidade": quantidade,
                    "permitir_multiplos": permitir_multiplos,
                    "servico": servico,
                    "duracao_servico": duracao_servico,
                    "vendedor": vendedor.id if vendedor else None,
                    "remover_vendedor": remover_vendedor,
                },
                user_message=str(error),
            )
            return

        detalhes = [f"Preço: **{item['item_price']}** moedas"]
        if item["is_service"]:
            detalhes.append("Tipo: Serviço comunitário")
            if item.get("duration"):
                detalhes.append(
                    "Duração após uso: "
                    + format_duration_seconds(int(item["duration"]))
                )
        else:
            total = item.get("quantity_total")
            available = item.get("quantity_available")
            if total is None:
                if available is None:
                    detalhes.append("Quantidade disponível: Ilimitada")
                else:
                    detalhes.append(f"Quantidade disponível: {int(available)}")
            else:
                detalhes.append(
                    f"Quantidade total: {int(total)} (disponível: {int(available or 0)})"
                )
            detalhes.append(
                "Permite múltiplas unidades"
                if item["allow_multiple"]
                else "Apenas uma por usuário"
            )

        await interaction.followup.send(
            (
                f"Item **{item['item_name']}** atualizado com sucesso!\n"
                + "\n".join(detalhes)
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
    await bot.add_cog(EconomyAdminCog(bot))
