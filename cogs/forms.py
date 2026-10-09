from core.database import (
    assignTempRole,
    create_form_submission,
    create_form_published_message,
    create_form_flow,
    get_form_flow,
    get_form_flow_by_name,
    get_form_questions,
    get_form_submission,
    get_form_submission_by_message,
    get_pending_portaria_submission_for_user,
    get_portaria_account_release_override,
    get_portaria_invite_bypass_codes,
    get_portaria_base_config,
    get_user_community_status_invite_link,
    async_includeUser,
    list_form_flows,
    normalize_discord_invite_code,
    record_form_decision,
    reset_form_submission_to_pending,
    replace_form_questions,
    fix_form_submission_message_binding,
    update_form_submission_message_id,
    update_form_flow,
)
from core.discord_events import getStaffRoles
from core.form_views import FORM_FLOW_CUSTOM_ID_PREFIX, FormFlowButtonView
from core.portaria_views import (
    PORTARIA_APPROVE_PREFIX,
    PORTARIA_REJECT_PREFIX,
    build_portaria_view,
)
from core.time_functions import now
from discord import Interaction, app_commands
from discord.ext import commands
import asyncio
import logging
import discord
from datetime import datetime, timedelta
import re
from typing import Literal


BIRTHDAY_KEYWORDS = ("nascimento", "anivers")

EMBED_FIELD_NAME_LIMIT = 256
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_TOTAL_LIMIT = 6000
RESPONDENT_VALUE_RESERVE = 128
FOOTER_TEXT_RESERVE = 24


logger = logging.getLogger(__name__)


def _limited_field_name(question_text: str, suffix: str = "") -> str:
    """Return a non-empty Discord field name, leaving room for a suffix."""
    name = question_text.strip() or "Pergunta"
    available = EMBED_FIELD_NAME_LIMIT - len(suffix)
    if len(name) > available:
        name = name[: max(1, available - 3)].rstrip() + "..."
    return f"{name}{suffix}"


def _embed_title(flow_data: dict) -> str:
    if flow_data.get("type") == "portaria":
        return "Registro da portaria"
    return f"📋 Respostas: {flow_data['name']}"[:256]


def _embed_character_count(embed: discord.Embed) -> int:
    """Count every string that Discord includes in its 6,000 character limit."""
    return len(embed)


def _response_length_limits(
    flow_data: dict,
    questions: list[dict],
) -> dict[int, int]:
    """Share the remaining embed budget fairly among the form questions."""
    if not questions:
        return {}

    fixed_size = (
        len(_embed_title(flow_data))
        + len("Respondente")
        + RESPONDENT_VALUE_RESERVE
        + sum(len(_limited_field_name(question["question_text"])) for question in questions)
    )
    if flow_data.get("type") == "portaria":
        fixed_size += FOOTER_TEXT_RESERVE

    response_budget = max(1, EMBED_TOTAL_LIMIT - fixed_size)
    base_limit, remainder = divmod(response_budget, len(questions))
    return {
        question["id"]: min(
            EMBED_FIELD_VALUE_LIMIT,
            base_limit + (1 if index < remainder else 0),
        )
        for index, question in enumerate(questions)
    }


def _portaria_footer_text(member_id: int) -> str:
    return f"ID: {member_id}"


async def _resolve_guild_channel(
    guild: discord.Guild | None,
    channel_id: int | None,
) -> discord.abc.GuildChannel | discord.Thread | None:
    if guild is None or not channel_id:
        return None

    channel = guild.get_channel(int(channel_id))
    if channel is not None:
        return channel

    try:
        fetched_channel = await guild.fetch_channel(int(channel_id))
    except (discord.Forbidden, discord.HTTPException, discord.NotFound, ValueError):
        return None

    if isinstance(fetched_channel, (discord.abc.GuildChannel, discord.Thread)):
        return fetched_channel
    return None


class FormFlowModal(discord.ui.Modal):
    def __init__(self, flow_data: dict, questions: list[dict], target_channel: discord.TextChannel):
        modal_title = f"Formulário: {flow_data['name']}"
        super().__init__(title=modal_title[:45])
        self.flow_data = flow_data
        self.questions = questions
        self.target_channel = target_channel
        self.inputs: dict[int, discord.ui.TextInput] = {}
        response_limits = _response_length_limits(flow_data, questions)

        for question in questions:
            label = question["question_text"].strip() or "Pergunta"
            if len(label) > 45:
                label = f"{label[:42]}..."
            input_field = discord.ui.TextInput(
                label=label,
                custom_id=f"form_question:{question['id']}",
                required=bool(question.get("required")),
                placeholder=(question.get("placeholder_text") or None)[:100]
                if question.get("placeholder_text")
                else None,
                style=discord.TextStyle.paragraph,
                max_length=response_limits[question["id"]],
            )
            self.inputs[question["id"]] = input_field
            self.add_item(input_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            await interaction.followup.send(
                content="Este formulário só pode ser respondido dentro de um servidor.",
                ephemeral=True,
            )
            return

        target_channel = await _resolve_guild_channel(interaction.guild, self.target_channel.id)
        if target_channel is None:
            await interaction.followup.send(
                content="O canal de destino não existe mais ou não está acessível.",
                ephemeral=True,
            )
            return

        bot_member = target_channel.guild.me
        if bot_member is None:
            bot_member = target_channel.guild.get_member(interaction.client.user.id)
        if bot_member is None:
            await interaction.followup.send(
                content="Não foi possível validar as permissões do bot neste canal.",
                ephemeral=True,
            )
            return

        permissions = target_channel.permissions_for(bot_member)
        if not permissions.send_messages:
            await interaction.followup.send(
                content="Não tenho permissão para enviar respostas neste canal.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=_embed_title(self.flow_data),
            color=discord.Color.blurple(),
            timestamp=now(),
        )
        respondente_value = interaction.user.mention
        if self.flow_data.get("type") == "portaria":
            member = interaction.user if isinstance(interaction.user, discord.Member) else None
            thumbnail_url = None
            if member and member.guild_avatar:
                thumbnail_url = member.guild_avatar.url
            elif interaction.user.avatar:
                thumbnail_url = interaction.user.avatar.url
            else:
                thumbnail_url = interaction.user.display_avatar.url
            embed.set_thumbnail(url=thumbnail_url)
            discord_joined = member.created_at.date() if member else None
            guild_joined = member.joined_at.date() if member and member.joined_at else None
            if discord_joined and guild_joined:
                same_day = discord_joined == guild_joined
                warning = " ⚠️" if same_day else ""
                respondente_value = (
                    f"{interaction.user.mention}\n"
                    f"Discord: {discord_joined:%d/%m/%Y} | "
                    f"Servidor: {guild_joined:%d/%m/%Y}{warning}"
                )
            else:
                respondente_value = (
                    f"{interaction.user.mention}\n"
                    "Discord: Não disponível | Servidor: Não disponível"
                )
            embed.set_footer(text=_portaria_footer_text(interaction.user.id))
        embed.add_field(name="Respondente", value=respondente_value, inline=False)

        responses = {}
        for question in self.questions:
            input_field = self.inputs.get(question["id"])
            response_text = input_field.value if input_field else ""
            responses[question["id"]] = response_text

        for question in self.questions:
            response_text = responses[question["id"]]
            embed.add_field(
                name=_limited_field_name(question["question_text"]),
                value=response_text if response_text.strip() else "Não respondido.",
                inline=False,
            )

        if _embed_character_count(embed) > EMBED_TOTAL_LIMIT:
            largest_question = max(
                self.questions,
                key=lambda question: len(responses.get(question["id"], "")),
            )
            question_name = _limited_field_name(largest_question["question_text"])
            await interaction.followup.send(
                content=(
                    "A ficha ultrapassa o limite do Discord. Reduza a resposta de "
                    f"**{question_name}** e tente novamente. Seu texto não foi alterado."
                ),
                ephemeral=True,
            )
            return

        decision_view = None
        submission_id = None
        if self.flow_data.get("type") == "portaria":
            pending_submission = get_pending_portaria_submission_for_user(
                interaction.user.id,
                target_channel.guild.id,
            )
            if pending_submission:
                await interaction.followup.send(
                    content=(
                        "Você já possui uma ficha da portaria em análise. "
                        "Aguarde a equipe concluir antes de enviar uma nova."
                    ),
                    ephemeral=True,
                )
                return
            submission_id = create_form_submission(
                self.flow_data["id"],
                interaction.user.id,
                target_channel.guild.id,
                target_channel.id,
            )
            decision_view = build_portaria_view(submission_id)

        try:
            sent_message = await target_channel.send(
                embed=embed,
                view=decision_view,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                content="Não tenho permissão para enviar respostas neste canal.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.followup.send(
                content="Não foi possível enviar sua resposta no momento. Tente novamente mais tarde.",
                ephemeral=True,
            )
            return

        if submission_id is not None:
            update_form_submission_message_id(submission_id, sent_message.id)
            interaction.client.add_view(decision_view, message_id=sent_message.id)
        await interaction.followup.send(
            content="Sua resposta foi enviada! Obrigado por preencher o formulário.",
            ephemeral=True,
        )


class FormsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    formulario = app_commands.Group(
        name="formulario",
        description="Comandos para gerenciar formulários.",
    )

    @staticmethod
    def _parse_questions(questions_raw: str) -> list[dict]:
        if not questions_raw:
            raise ValueError("Informe ao menos uma pergunta válida.")

        raw_parts = re.split(r"[;\n]+", questions_raw)
        questions: list[dict] = []
        for raw_question in raw_parts:
            text = raw_question.strip()
            if not text:
                continue
            required = text.startswith("*")
            if required:
                text = text.lstrip("*").strip()
            if not text:
                continue
            question_text = text
            placeholder_text = None
            if "|" in text:
                question_part, placeholder_part = text.split("|", 1)
                question_text = question_part.strip()
                placeholder_text = placeholder_part.strip() or None

            if not question_text:
                continue

            questions.append(
                {
                    "question_text": question_text,
                    "placeholder_text": placeholder_text,
                    "required": required,
                }
            )

        if not questions:
            raise ValueError("Informe ao menos uma pergunta válida.")
        if len(questions) > 5:
            raise ValueError("Limite máximo de 5 perguntas por formulário.")
        return questions

    @staticmethod
    def _staff_guard(ctx: Interaction) -> bool:
        if ctx.guild is None or not isinstance(ctx.user, discord.Member):
            return False
        staff_roles = getStaffRoles(ctx.guild)
        return any(role in ctx.user.roles for role in staff_roles)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return

        data = interaction.data or {}
        custom_id = data.get("custom_id")
        if not custom_id or not custom_id.startswith(FORM_FLOW_CUSTOM_ID_PREFIX):
            return

        if interaction.guild is None:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    content="Este formulário só pode ser usado dentro de um servidor.",
                    ephemeral=True,
                )
            return

        flow_id_part = custom_id.split(":", 1)[1]
        if not flow_id_part.isdigit():
            await interaction.response.send_message(
                content="Não foi possível identificar o formulário.",
                ephemeral=True,
            )
            return

        try:
            flow_data = get_form_flow(interaction.guild.id, int(flow_id_part))
        except ValueError:
            await interaction.response.send_message(
                content="Fluxo de formulário não encontrado.",
                ephemeral=True,
            )
            return

        target_channel_id = flow_data.get("target_channel_id")
        target_channel = await _resolve_guild_channel(interaction.guild, target_channel_id)
        if target_channel is None:
            await interaction.response.send_message(
                content="Não foi possível localizar o canal para enviar as respostas.",
                ephemeral=True,
            )
            return

        if flow_data.get("type") == "portaria":
            account_release_override = get_portaria_account_release_override(
                interaction.guild.id,
                interaction.user.id,
            )
            invite_bypass_detected = await self._is_portaria_invite_bypassed(interaction)
            manual_direct_approval = (
                bool(account_release_override)
                and not bool(account_release_override.get("requires_form", 1))
            )
            direct_approval_handled = await self._handle_portaria_direct_approval_if_disabled(
                interaction=interaction,
                flow_data=flow_data,
                target_channel=target_channel,
                force_auto_approval=(invite_bypass_detected or manual_direct_approval),
            )
            if direct_approval_handled:
                return

            account_age_validation = await self._validate_portaria_account_age_for_form(interaction)
            if account_age_validation:
                await interaction.response.send_message(
                    content=account_age_validation["member_message"],
                    ephemeral=True,
                )
                asyncio.create_task(
                    self._log_portaria_account_age_restriction(
                        interaction=interaction,
                        flow_data=flow_data,
                        reason=account_age_validation["rejection_reason"],
                    )
                )
                asyncio.create_task(
                    self._record_portaria_auto_decision(
                        interaction=interaction,
                        flow_data=flow_data,
                        target_channel=target_channel,
                        decision="rejected",
                    )
                )
                return

        questions = get_form_questions(flow_data["id"])
        if not questions:
            await interaction.response.send_message(
                content="Este formulário ainda não possui perguntas configuradas.",
                ephemeral=True,
            )
            return

        if len(questions) > 5:
            await interaction.response.send_message(
                content="Este formulário possui mais de 5 perguntas e precisa ser dividido.",
                ephemeral=True,
            )
            return

        if flow_data.get("type") == "portaria":
            birthday_requirement_error = self._validate_portaria_birthday_requirement(interaction, questions)
            if birthday_requirement_error:
                await interaction.response.send_message(
                    content=birthday_requirement_error,
                    ephemeral=True,
                )
                return

        modal = FormFlowModal(flow_data, questions, target_channel)
        try:
            await interaction.response.send_modal(modal)
        except discord.NotFound:
            logger.warning("Interação expirou antes de abrir o modal do formulário.")
            return

    async def _handle_portaria_direct_approval_if_disabled(
        self,
        interaction: discord.Interaction,
        flow_data: dict,
        target_channel: discord.TextChannel,
        force_auto_approval: bool = False,
    ) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False

        config = get_portaria_base_config(interaction.guild.id)
        formulario_portaria_ativo = bool(config.get("formulario_portaria_ativo", 1))
        if formulario_portaria_ativo and not force_auto_approval:
            return False

        await interaction.response.defer(ephemeral=True)

        pending_submission = get_pending_portaria_submission_for_user(
            interaction.user.id,
            interaction.guild.id,
        )
        if pending_submission:
            await interaction.followup.send(
                content=(
                    "Você já possui uma ficha da portaria em análise. "
                    "Aguarde a equipe concluir antes de tentar novamente."
                ),
                ephemeral=True,
            )
            return True

        conta_dias = (now().date() - interaction.user.created_at.date()).days
        idade_minima_prov_ativa = bool(config.get("idade_minima_conta_acesso_provisorio_ativa"))
        idade_minima_prov_dias = int(config.get("idade_minima_conta_acesso_provisorio_dias") or 30)
        if idade_minima_prov_ativa and idade_minima_prov_dias > 0:
            is_new_account = conta_dias < idade_minima_prov_dias
        else:
            is_new_account = conta_dias < 30

        account_release_override = get_portaria_account_release_override(
            interaction.guild.id,
            interaction.user.id,
        )

        invite_link_used = get_user_community_status_invite_link(
            interaction.guild.id,
            interaction.user,
        )
        bypass_invites = {
            code.casefold() for code in get_portaria_invite_bypass_codes(interaction.guild.id)
        }
        if invite_link_used:
            invite_code = normalize_discord_invite_code(invite_link_used)
            if invite_code in bypass_invites:
                is_new_account = False

        if account_release_override:
            is_new_account = (
                str(account_release_override.get("access_mode") or "").casefold() == "provisorio"
            )

        acesso_provisorio_ativo = bool(config.get("aprovacao_acesso_provisorio_ativo"))
        acesso_provisorio_duracao_dias = int(config.get("aprovacao_acesso_provisorio_duracao_dias") or 15)
        acesso_provisorio_role_id = config.get("acesso_provisorio_role_id")
        acesso_provisorio_role = (
            interaction.guild.get_role(acesso_provisorio_role_id)
            if acesso_provisorio_role_id
            else None
        )

        if is_new_account and acesso_provisorio_ativo:
            if acesso_provisorio_role is None:
                await interaction.followup.send(
                    content=(
                        "Não foi possível aplicar aprovação automática: "
                        "o cargo de acesso provisório não está configurado."
                    ),
                    ephemeral=True,
                )
                return True
            if acesso_provisorio_duracao_dias <= 0:
                await interaction.followup.send(
                    content=(
                        "Não foi possível aplicar aprovação automática: "
                        "a duração do acesso provisório deve ser maior que zero."
                    ),
                    ephemeral=True,
                )
                return True

        visitante_role_id = config.get("visitante_role_id")
        visitante_role = interaction.guild.get_role(visitante_role_id) if visitante_role_id else None
        is_provisional_approval = is_new_account and acesso_provisorio_ativo

        approved_channel_id = flow_data.get("approved_target_channel_id") or flow_data.get(
            "target_channel_id"
        )
        approved_channel = await _resolve_guild_channel(interaction.guild, approved_channel_id)
        if not isinstance(approved_channel, discord.TextChannel):
            await interaction.followup.send(
                content=(
                    "Sua aprovação automática foi rejeitada: "
                    "não foi possível localizar o canal de aprovados configurado."
                ),
                ephemeral=True,
            )
            return True

        bot_member = interaction.guild.me
        if bot_member is None and interaction.client.user:
            bot_member = interaction.guild.get_member(interaction.client.user.id)
        if bot_member is None:
            await interaction.followup.send(
                content=(
                    "Sua aprovação automática foi rejeitada: "
                    "não foi possível validar as permissões do bot no canal de aprovados."
                ),
                ephemeral=True,
            )
            return True

        approved_permissions = approved_channel.permissions_for(bot_member)
        if not approved_permissions.send_messages or not approved_permissions.embed_links:
            await interaction.followup.send(
                content=(
                    "Sua aprovação automática foi rejeitada: "
                    "o bot não possui permissão para enviar a ficha no canal de aprovados."
                ),
                ephemeral=True,
            )
            return True

        log_embed = discord.Embed(
            title="✅ Registro da portaria",
            color=discord.Color.green(),
            timestamp=now(),
        )
        log_embed.add_field(name="Respondente", value=interaction.user.mention, inline=False)
        log_embed.add_field(
            name="Motivo",
            value=(
                (
                    "Convite com bypass configurado detectado; "
                    if force_auto_approval
                    else "Formulário da portaria está desativado; "
                )
                + (
                    "acesso provisório aplicado por conta recente."
                    if is_provisional_approval
                    else "aprovação completa feita ao clicar no botão."
                )
            ),
            inline=False,
        )
        log_embed.set_footer(text=_portaria_footer_text(interaction.user.id))
        try:
            await approved_channel.send(embed=log_embed)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                content=(
                    "Sua aprovação automática foi rejeitada: "
                    "não foi possível encaminhar a ficha para o canal de aprovados."
                ),
                ephemeral=True,
            )
            return True

        try:
            await async_includeUser(interaction.user, interaction.guild.id, datetime.now())
        except Exception as exc:
            await interaction.followup.send(
                content=f"Não foi possível registrar sua aprovação automática agora. Detalhes: {exc}",
                ephemeral=True,
            )
            return True

        if is_provisional_approval:
            try:
                if visitante_role and visitante_role not in interaction.user.roles:
                    await interaction.user.add_roles(
                        visitante_role,
                        reason="Aprovação automática provisória com formulário da portaria desativado",
                    )
                if acesso_provisorio_role not in interaction.user.roles:
                    await interaction.user.add_roles(
                        acesso_provisorio_role,
                        reason="Aprovação automática provisória com formulário da portaria desativado",
                    )
                expiration_date = now() + timedelta(days=acesso_provisorio_duracao_dias)
                await assignTempRole(
                    interaction.guild.id,
                    interaction.user,
                    acesso_provisorio_role.id,
                    expiration_date,
                    "Carteirinha provisória",
                )
                if visitante_role is not None:
                    await assignTempRole(
                        interaction.guild.id,
                        interaction.user,
                        visitante_role.id,
                        expiration_date,
                        "Cargo visitante temporário",
                    )
            except (discord.Forbidden, discord.HTTPException, ValueError) as exc:
                await interaction.followup.send(
                    content=(
                        "Não foi possível aplicar o acesso provisório automaticamente. "
                        "Tente novamente após ajustar permissões/cargos da portaria. "
                        f"Detalhes: {exc}"
                    ),
                    ephemeral=True,
                )
                return True
        elif visitante_role and visitante_role in interaction.user.roles:
            try:
                await interaction.user.remove_roles(
                    visitante_role,
                    reason="Aprovação automática com formulário da portaria desativado",
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                await interaction.followup.send(
                    content=(
                        "Sua aprovação automática não pôde ser concluída porque "
                        "não foi possível remover o cargo de visitante. "
                        f"Detalhes: {exc}"
                    ),
                    ephemeral=True,
                )
                return True

        await interaction.followup.send(
            content=(
                "✅ Convite com bypass detectado: você foi registrado com "
                f"**acesso provisório** por {acesso_provisorio_duracao_dias} dia(s)."
                if force_auto_approval and is_provisional_approval
                else "✅ Convite com bypass detectado: você foi registrado e aprovado automaticamente."
                if force_auto_approval
                else "✅ Formulário da portaria desativado: você foi registrado com "
                f"**acesso provisório** por {acesso_provisorio_duracao_dias} dia(s)."
                if is_provisional_approval
                else "✅ Formulário da portaria desativado: você foi registrado e aprovado automaticamente."
            ),
            ephemeral=True,
        )
        await self._record_portaria_auto_decision(
            interaction=interaction,
            flow_data=flow_data,
            target_channel=target_channel,
            decision="approved",
        )
        return True

    async def _record_portaria_auto_decision(
        self,
        interaction: discord.Interaction,
        flow_data: dict,
        target_channel: discord.abc.GuildChannel | discord.Thread,
        decision: Literal["approved", "rejected"],
    ) -> None:
        """Registra aprovações/recusas automáticas no histórico de decisões da portaria."""

        if interaction.guild is None:
            return

        actor_id = None
        if interaction.client and interaction.client.user:
            actor_id = interaction.client.user.id
        elif interaction.guild.me:
            actor_id = interaction.guild.me.id
        if actor_id is None:
            return

        try:
            submission_id = create_form_submission(
                flow_id=int(flow_data["id"]),
                user_id=int(interaction.user.id),
                guild_id=int(interaction.guild.id),
                channel_id=int(target_channel.id),
            )
            record_form_decision(submission_id, decision, int(actor_id))
        except Exception:
            return

    @staticmethod
    async def _is_portaria_invite_bypassed(interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False

        invite_link_used = await asyncio.to_thread(
            get_user_community_status_invite_link,
            interaction.guild.id,
            interaction.user,
        )
        if not invite_link_used:
            return False

        bypass_codes = await asyncio.to_thread(
            get_portaria_invite_bypass_codes,
            interaction.guild.id,
        )
        bypass_invites = {code.casefold() for code in bypass_codes}
        invite_code = normalize_discord_invite_code(invite_link_used)
        return bool(invite_code and invite_code in bypass_invites)

    async def _log_portaria_account_age_restriction(
        self,
        interaction: discord.Interaction,
        flow_data: dict,
        reason: str,
    ) -> None:
        if interaction.guild is None:
            return

        rejected_channel_id = (
            flow_data.get("rejected_target_channel_id")
            or flow_data.get("target_channel_id")
        )
        if not rejected_channel_id:
            return

        rejected_channel = interaction.guild.get_channel(int(rejected_channel_id))
        if not isinstance(rejected_channel, discord.TextChannel):
            return

        bot_user = interaction.client.user
        responsible = bot_user.mention if bot_user else "Bot da portaria"
        embed = discord.Embed(
            title="❌ Registro da portaria",
            color=discord.Color.red(),
            timestamp=now(),
        )
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        thumbnail_url = interaction.user.display_avatar.url
        if member and member.guild_avatar:
            thumbnail_url = member.guild_avatar.url
        elif interaction.user.avatar:
            thumbnail_url = interaction.user.avatar.url
        embed.set_thumbnail(url=thumbnail_url)

        discord_joined = member.created_at.date() if member else None
        guild_joined = member.joined_at.date() if member and member.joined_at else None
        if discord_joined and guild_joined:
            same_day = discord_joined == guild_joined
            warning = " ⚠️" if same_day else ""
            respondente_value = (
                f"{interaction.user.mention}\n"
                f"Discord: {discord_joined:%d/%m/%Y} | "
                f"Servidor: {guild_joined:%d/%m/%Y}{warning}"
            )
        else:
            respondente_value = (
                f"{interaction.user.mention}\n"
                "Discord: Não disponível | Servidor: Não disponível"
            )
        embed.add_field(
            name="Respondente",
            value=respondente_value,
            inline=False,
        )
        embed.add_field(
            name="Motivo da reprovação",
            value=reason.strip() or "Não informado.",
            inline=False,
        )
        embed.add_field(
            name="Rejeitado por",
            value=responsible,
            inline=False,
        )
        embed.set_footer(text=_portaria_footer_text(interaction.user.id))

        try:
            await rejected_channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            return


    @staticmethod
    async def _validate_portaria_account_age_for_form(interaction: discord.Interaction) -> dict | None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return None

        if await FormsCog._is_portaria_invite_bypassed(interaction):
            return None

        override = await asyncio.to_thread(
            get_portaria_account_release_override,
            interaction.guild.id,
            interaction.user.id,
        )
        if override:
            return None

        config = await asyncio.to_thread(get_portaria_base_config, interaction.guild.id)
        minimum_active = bool(config.get("idade_minima_conta_ficha_ativa"))
        minimum_days = int(config.get("idade_minima_conta_ficha_dias") or 0)
        if not minimum_active or minimum_days <= 0:
            return None

        account_age_days = (datetime.utcnow().date() - interaction.user.created_at.date()).days
        if account_age_days >= minimum_days:
            return None

        return {
            "member_message": (
                "Sua conta ainda não atende os requisitos da portaria para abrir a ficha. "
                f"Idade mínima exigida: **{minimum_days} dia(s)**. "
                f"Sua conta possui **{account_age_days} dia(s)**."
            ),
            "rejection_reason": (
                "Recusa automática: idade da conta abaixo do mínimo configurado para abertura da ficha "
                f"({account_age_days}/{minimum_days} dia(s))."
            ),
        }


    @staticmethod
    def _validate_portaria_birthday_requirement(
        interaction: discord.Interaction,
        questions: list[dict],
    ) -> str | None:
        if interaction.guild is None:
            return None

        config = get_portaria_base_config(interaction.guild.id)
        birthday_required = bool(config.get("idade_minima_entrada_servidor_ativa"))
        if not birthday_required:
            return None

        for question in questions:
            name = (question.get("question_text") or "").casefold()
            if any(keyword in name for keyword in BIRTHDAY_KEYWORDS):
                if bool(question.get("required")):
                    return None
                return (
                    "A configuração de idade mínima de entrada no servidor está ativa, "
                    "mas a pergunta de aniversário da portaria não está obrigatória. "
                    "Marque essa pergunta como obrigatória no formulário."
                )

        return (
            "A configuração de idade mínima de entrada no servidor está ativa, "
            "então o formulário da portaria precisa ter uma pergunta obrigatória de data de nascimento."
        )

    @staticmethod
    def _resolve_flow(guild_id: int, flow_reference: str) -> dict:
        flow_reference = (flow_reference or "").strip()
        if flow_reference.isdigit():
            return get_form_flow(guild_id, int(flow_reference))
        return get_form_flow_by_name(guild_id, flow_reference)

    @formulario.command(
        name="publicar",
        description="Publica um botão de formulário em um canal.",
    )
    @app_commands.describe(
        flow="ID ou nome do fluxo",
        channel="Canal onde o formulário será publicado",
        message="Mensagem enviada junto com o botão (use \\n para pular linha)",
        button_text="Texto do botão",
    )
    async def publish_form(
        self,
        ctx: Interaction,
        flow: str,
        channel: discord.TextChannel | None = None,
        message: str | None = None,
        button_text: str | None = None,
    ) -> None:
        if not self._staff_guard(ctx):
            await ctx.response.send_message(
                content="Apenas membros da staff podem usar este comando.",
                ephemeral=True,
            )
            return

        try:
            flow_data = self._resolve_flow(ctx.guild.id, flow)
        except ValueError:
            await ctx.response.send_message(
                content="Fluxo de formulário não encontrado.",
                ephemeral=True,
            )
            return

        target_channel = channel
        if target_channel is None:
            target_channel_id = flow_data.get("target_channel_id")
            if not target_channel_id:
                await ctx.response.send_message(
                    content="Este fluxo não possui um canal padrão configurado.",
                    ephemeral=True,
                )
                return
            target_channel = await _resolve_guild_channel(ctx.guild, target_channel_id)

        if target_channel is None:
            await ctx.response.send_message(
                content="Não foi possível localizar o canal informado para publicar o formulário.",
                ephemeral=True,
            )
            return

        permissions = target_channel.permissions_for(ctx.user)
        if not permissions.send_messages:
            await ctx.response.send_message(
                content="Você não tem permissão para postar neste canal.",
                ephemeral=True,
            )
            return

        bot_member = ctx.guild.me if ctx.guild else None
        if bot_member is None:
            bot_member = ctx.guild.get_member(self.bot.user.id) if ctx.guild else None
        if bot_member is None:
            await ctx.response.send_message(
                content="Não foi possível validar as permissões do bot neste canal.",
                ephemeral=True,
            )
            return

        bot_permissions = target_channel.permissions_for(bot_member)
        if not bot_permissions.send_messages:
            await ctx.response.send_message(
                content="Não tenho permissão para publicar o formulário neste canal.",
                ephemeral=True,
            )
            return

        default_message = (
            f"📋 **{flow_data['name']}**\nClique no botão para abrir o formulário."
        )
        message_text = (message or "")
        if message_text:
            message_text = message_text.replace("\\n", "\n")
        elif message is None:
            message_text = default_message
        else:
            message_text = ""

        button_label = (button_text or "").strip() or "Abrir formulário"
        if len(button_label) > 80:
            await ctx.response.send_message(
                content="O texto do botão deve ter no máximo 80 caracteres.",
                ephemeral=True,
            )
            return
        view = FormFlowButtonView(flow_data["id"], label=button_label)
        sent_message = await target_channel.send(
            content=message_text if message_text != "" else None,
            view=view,
        )
        create_form_published_message(
            flow_data["id"],
            sent_message.id,
            target_channel.id,
            ctx.guild.id,
        )
        self.bot.add_view(view, message_id=sent_message.id)
        await ctx.response.send_message(
            content=f"Formulário publicado em {target_channel.mention}.",
            ephemeral=True,
        )

    @formulario.command(
        name="criar",
        description="Cria um fluxo de formulário.",
    )
    @app_commands.describe(
        name="Nome do fluxo",
        flow_type="Tipo do fluxo (ex: portaria)",
        target_channel="Canal onde as respostas serão enviadas",
        questions=(
            "Perguntas separadas por ; "
            "(use * no início para obrigatória e | para o texto interno do campo)"
        ),
    )
    async def create_flow(
        self,
        ctx: Interaction,
        name: str,
        flow_type: str,
        target_channel: discord.TextChannel,
        questions: str,
    ) -> None:
        if not self._staff_guard(ctx):
            await ctx.response.send_message(
                content="Apenas membros da staff podem usar este comando.",
                ephemeral=True,
            )
            return

        flow_name = name.strip()
        if not flow_name:
            await ctx.response.send_message(
                content="Informe um nome válido para o fluxo.",
                ephemeral=True,
            )
            return

        flow_kind = flow_type.strip().lower()
        if not flow_kind:
            await ctx.response.send_message(
                content="Informe um tipo válido para o fluxo.",
                ephemeral=True,
            )
            return

        try:
            parsed_questions = self._parse_questions(questions)
        except ValueError as exc:
            await ctx.response.send_message(
                content=str(exc),
                ephemeral=True,
            )
            return

        flow_id = create_form_flow(ctx.guild.id, flow_name, flow_kind, target_channel.id)
        replace_form_questions(flow_id, parsed_questions)
        await ctx.response.send_message(
            content=(
                f"Fluxo criado com sucesso! ID: **{flow_id}**\n"
                f"Nome: **{flow_name}**\n"
                f"Tipo: **{flow_kind}**\n"
                f"Canal de respostas: {target_channel.mention}\n"
                f"Perguntas configuradas: **{len(parsed_questions)}**"
            ),
            ephemeral=True,
        )

    @formulario.command(
        name="listar",
        description="Lista todos os fluxos de formulários.",
    )
    async def list_flows(self, ctx: Interaction) -> None:
        if not self._staff_guard(ctx):
            await ctx.response.send_message(
                content="Apenas membros da staff podem usar este comando.",
                ephemeral=True,
            )
            return

        flows = list_form_flows(ctx.guild.id)
        if not flows:
            await ctx.response.send_message(
                content="Nenhum fluxo de formulário cadastrado.",
                ephemeral=True,
            )
            return

        def _channel_text(channel_id: int | None) -> str:
            channel = ctx.guild.get_channel(int(channel_id)) if ctx.guild and channel_id else None
            if channel:
                return channel.mention
            return f"<#{channel_id}>" if channel_id else "padrão"

        lines = []
        for flow in flows:
            channel_text = _channel_text(flow.get("target_channel_id"))
            approved_text = _channel_text(flow.get("approved_target_channel_id"))
            rejected_text = _channel_text(flow.get("rejected_target_channel_id"))
            lines.append(
                f"**{flow['id']}** • {flow['name']} • tipo `{flow['type']}` • respostas {channel_text} • aprovadas {approved_text} • recusadas {rejected_text} • feedback recusa `{'on' if flow.get('rejection_feedback_enabled') else 'off'}`"
            )

        await ctx.response.send_message(
            content="📋 **Fluxos de formulário**\n" + "\n".join(lines),
            ephemeral=True,
        )

    @formulario.command(
        name="configurar_portaria",
        description="Define os canais de encaminhamento das fichas da portaria.",
    )
    @app_commands.describe(
        flow="ID ou nome do fluxo da portaria",
        approved_channel="Canal para fichas aprovadas",
        rejected_channel="Canal para fichas recusadas",
        rejection_feedback_enabled="Ativa/desativa formulário de motivo ao recusar",
    )
    async def configure_portaria_channels(
        self,
        ctx: Interaction,
        flow: str,
        approved_channel: discord.TextChannel | None = None,
        rejected_channel: discord.TextChannel | None = None,
        rejection_feedback_enabled: bool | None = None,
    ) -> None:
        if not self._staff_guard(ctx):
            await ctx.response.send_message(
                content="Apenas membros da staff podem usar este comando.",
                ephemeral=True,
            )
            return

        try:
            flow_data = self._resolve_flow(ctx.guild.id, flow)
        except ValueError:
            await ctx.response.send_message(
                content="Fluxo de formulário não encontrado.",
                ephemeral=True,
            )
            return

        if flow_data.get("type") != "portaria":
            await ctx.response.send_message(
                content="Este comando é exclusivo para fluxos do tipo `portaria`.",
                ephemeral=True,
            )
            return

        if approved_channel is None and rejected_channel is None and rejection_feedback_enabled is None:
            await ctx.response.send_message(
                content="Informe ao menos um campo para configurar.",
                ephemeral=True,
            )
            return

        update_data: dict[str, int | bool] = {}
        if approved_channel is not None:
            update_data["approved_target_channel_id"] = approved_channel.id
        if rejected_channel is not None:
            update_data["rejected_target_channel_id"] = rejected_channel.id
        if rejection_feedback_enabled is not None:
            update_data["rejection_feedback_enabled"] = rejection_feedback_enabled

        update_form_flow(ctx.guild.id, flow_data["id"], **update_data)

        updated_flow = get_form_flow(ctx.guild.id, flow_data["id"])
        approved_text = (
            approved_channel.mention
            if approved_channel
            else f"<#{updated_flow.get('approved_target_channel_id')}>"
            if updated_flow.get("approved_target_channel_id")
            else "Canal padrão de respostas"
        )
        rejected_text = (
            rejected_channel.mention
            if rejected_channel
            else f"<#{updated_flow.get('rejected_target_channel_id')}>"
            if updated_flow.get("rejected_target_channel_id")
            else "Canal padrão de respostas"
        )
        feedback_text = "Ativado" if updated_flow.get("rejection_feedback_enabled") else "Desativado"
        await ctx.response.send_message(
            content=(
                f"Configuração da portaria atualizada para o fluxo **{updated_flow['name']}** (ID {updated_flow['id']}).\n"
                f"Aprovadas → {approved_text}\n"
                f"Recusadas → {rejected_text}\n"
                f"Feedback de recusa → {feedback_text}"
            ),
            ephemeral=True,
        )

    @formulario.command(
        name="editar",
        description="Edita um fluxo de formulário.",
    )
    @app_commands.describe(
        flow="ID ou nome do fluxo",
        name="Novo nome do fluxo",
        flow_type="Novo tipo do fluxo",
        target_channel="Novo canal de respostas",
        questions=(
            "Perguntas separadas por ; "
            "(use * no início para obrigatória e | para o texto interno do campo)"
        ),
    )
    async def edit_flow(
        self,
        ctx: Interaction,
        flow: str,
        name: str | None = None,
        flow_type: str | None = None,
        target_channel: discord.TextChannel | None = None,
        questions: str | None = None,
    ) -> None:
        if not self._staff_guard(ctx):
            await ctx.response.send_message(
                content="Apenas membros da staff podem usar este comando.",
                ephemeral=True,
            )
            return

        try:
            flow_data = self._resolve_flow(ctx.guild.id, flow)
        except ValueError:
            await ctx.response.send_message(
                content="Fluxo de formulário não encontrado.",
                ephemeral=True,
            )
            return

        new_name = name.strip() if name else None
        new_type = flow_type.strip().lower() if flow_type else None
        new_channel_id = target_channel.id if target_channel else None
        parsed_questions = None
        if questions is not None:
            try:
                parsed_questions = self._parse_questions(questions)
            except ValueError as exc:
                await ctx.response.send_message(
                    content=str(exc),
                    ephemeral=True,
                )
                return

        if not any([new_name, new_type, new_channel_id, parsed_questions]):
            await ctx.response.send_message(
                content="Informe ao menos um campo para atualizar.",
                ephemeral=True,
            )
            return

        flow_updates: dict[str, str | int] = {}
        if new_name:
            flow_updates["name"] = new_name
        if new_type:
            flow_updates["flow_type"] = new_type
        if new_channel_id:
            flow_updates["target_channel_id"] = new_channel_id

        if flow_updates:
            try:
                update_form_flow(
                    ctx.guild.id, flow_data["id"],
                    **flow_updates,
                )
            except ValueError as exc:
                await ctx.response.send_message(
                    content=str(exc),
                    ephemeral=True,
                )
                return

        if parsed_questions is not None:
            replace_form_questions(flow_data["id"], parsed_questions)

        channel_text = (
            target_channel.mention
            if target_channel
            else f"<#{flow_data['target_channel_id']}>"
        )
        question_text = (
            f"\nPerguntas configuradas: **{len(parsed_questions)}**"
            if parsed_questions is not None
            else ""
        )
        await ctx.response.send_message(
            content=(
                f"Fluxo atualizado: **{flow_data['id']}**\n"
                f"Nome: **{new_name or flow_data['name']}**\n"
                f"Tipo: **{new_type or flow_data['type']}**\n"
                f"Canal de respostas: {channel_text}"
                f"{question_text}"
            ),
            ephemeral=True,
        )

    @formulario.command(
        name="auditar_ficha_portaria",
        description="Analisa e corrige o vínculo/estado de uma ficha da portaria.",
    )
    @app_commands.describe(
        message_id="ID da mensagem da ficha na portaria",
        submission_id="ID do envio (opcional, para validar/corrigir um envio específico)",
        corrigir="Se ativo, tenta corrigir automaticamente o estado da ficha",
    )
    async def audit_portaria_submission(
        self,
        ctx: Interaction,
        message_id: str,
        submission_id: str | None = None,
        corrigir: bool = False,
    ) -> None:
        if not self._staff_guard(ctx):
            await ctx.response.send_message(
                content="Apenas membros da staff podem usar este comando.",
                ephemeral=True,
            )
            return

        if ctx.guild is None:
            await ctx.response.send_message(
                content="Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return

        if not message_id.isdigit():
            await ctx.response.send_message(
                content="O `message_id` informado é inválido.",
                ephemeral=True,
            )
            return

        submission_id_int = None
        if submission_id is not None:
            if not submission_id.isdigit():
                await ctx.response.send_message(
                    content="O `submission_id` informado é inválido.",
                    ephemeral=True,
                )
                return
            submission_id_int = int(submission_id)

        await ctx.response.defer(ephemeral=True)

        message_id_int = int(message_id)
        target_submission = (
            get_form_submission(submission_id_int)
            if submission_id_int is not None
            else get_form_submission_by_message(message_id_int, ctx.guild.id)
        )

        if not target_submission:
            await ctx.followup.send(
                content=(
                    "Nenhum envio foi encontrado com os dados informados.\n"
                    "Dica: confirme o ID da mensagem e, se necessário, informe também o `submission_id`."
                ),
                ephemeral=True,
            )
            return

        submission_message_mismatch = (
            submission_id_int is not None
            and int(target_submission.get("message_id") or 0) != message_id_int
        )
        submission_message_before = target_submission.get("message_id")

        if int(target_submission.get("guild_id") or 0) != ctx.guild.id:
            return await ctx.followup.send(content="A ficha não pertence a este servidor.", ephemeral=True)
        flow = get_form_flow(ctx.guild.id, int(target_submission["flow_id"]))
        if flow.get("type") != "portaria":
            await ctx.followup.send(
                content="O envio informado não pertence a um fluxo do tipo `portaria`.",
                ephemeral=True,
            )
            return

        async def _fetch_submission_message() -> discord.Message | None:
            channel_id = int(target_submission.get("channel_id") or 0)
            channels_to_try: list[discord.TextChannel] = []
            if channel_id:
                channel = ctx.guild.get_channel(channel_id)
                if isinstance(channel, discord.TextChannel):
                    channels_to_try.append(channel)
            if isinstance(ctx.channel, discord.TextChannel):
                channels_to_try.append(ctx.channel)

            tried_ids: set[int] = set()
            for channel in channels_to_try:
                if channel.id in tried_ids:
                    continue
                tried_ids.add(channel.id)
                try:
                    return await channel.fetch_message(message_id_int)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
            return None

        def _extract_component_button_ids(message: discord.Message | None) -> list[str]:
            if message is None:
                return []
            button_ids: list[str] = []
            for row in message.components:
                for component in row.children:
                    if isinstance(component, discord.Button) and component.custom_id:
                        button_ids.append(component.custom_id)
            return button_ids

        def _infer_discord_status(
            message: discord.Message | None,
            current_submission_id: int,
        ) -> tuple[str, dict[str, bool | int]]:
            if message is None:
                return "mensagem_ausente", {
                    "has_components": False,
                    "has_expected_buttons": False,
                    "buttons_functional": False,
                    "has_approved_marker": False,
                    "has_rejected_marker": False,
                }

            button_ids = _extract_component_button_ids(message)
            expected_approve = f"{PORTARIA_APPROVE_PREFIX}{current_submission_id}"
            expected_reject = f"{PORTARIA_REJECT_PREFIX}{current_submission_id}"
            has_approve = expected_approve in button_ids
            has_reject = expected_reject in button_ids
            has_components = len(button_ids) > 0
            buttons_functional = has_approve and has_reject

            has_approved_marker = False
            has_rejected_marker = False
            if message.embeds:
                embed = message.embeds[0]
                title = (embed.title or "").casefold()
                if "✅" in (embed.title or "") or "aprovado" in title:
                    has_approved_marker = True
                if "❌" in (embed.title or "") or "rejeitado" in title or "reprovado" in title:
                    has_rejected_marker = True

                for field in embed.fields:
                    field_name = (field.name or "").casefold()
                    if "aprovado por" in field_name:
                        has_approved_marker = True
                    if "rejeitado por" in field_name or "reprovado por" in field_name:
                        has_rejected_marker = True

            if has_rejected_marker:
                status = "rejected"
            elif has_approved_marker:
                status = "approved"
            elif buttons_functional:
                status = "pending"
            else:
                status = "sem_acao"

            return status, {
                "has_components": has_components,
                "has_expected_buttons": has_approve and has_reject,
                "buttons_functional": buttons_functional,
                "has_approved_marker": has_approved_marker,
                "has_rejected_marker": has_rejected_marker,
            }

        async def _move_message_if_needed(
            message: discord.Message | None,
            destination_channel_id: int | None,
        ) -> tuple[discord.Message | None, bool]:
            if message is None or not destination_channel_id:
                return message, False

            if message.channel.id == int(destination_channel_id):
                return message, False

            destination = ctx.guild.get_channel(int(destination_channel_id))
            if not isinstance(destination, discord.TextChannel):
                return message, False

            try:
                moved_message = await destination.send(
                    content=message.content or None,
                    embeds=message.embeds or None,
                    files=[await attachment.to_file() for attachment in message.attachments],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await message.delete()
                return moved_message, True
            except (discord.Forbidden, discord.HTTPException):
                return message, False

        submission_message = await _fetch_submission_message()
        discord_status, discord_checks = _infer_discord_status(
            submission_message,
            int(target_submission["id"]),
        )

        status_before = str(target_submission.get("status") or "desconhecido")
        binding_mismatch = (
            int(target_submission.get("guild_id") or 0) != ctx.guild.id
            or (
                submission_message is not None
                and int(target_submission.get("channel_id") or 0) != submission_message.channel.id
            )
            or int(target_submission.get("message_id") or 0) != message_id_int
        )

        fixes_applied: list[str] = []
        if corrigir:
            expected_status = discord_status if discord_status in {"pending", "approved", "rejected"} else "pending"

            if status_before != "pending":
                if reset_form_submission_to_pending(int(target_submission["id"])):
                    fixes_applied.append("status redefinido para `pending` e decisões removidas")

            if expected_status in {"approved", "rejected"}:
                try:
                    record_form_decision(
                        int(target_submission["id"]),
                        expected_status,
                        ctx.user.id,
                    )
                    fixes_applied.append(f"status sincronizado para `{expected_status}` com base no Discord")
                except ValueError:
                    pass

            flow_target_channel_id = None
            if expected_status == "approved":
                flow_target_channel_id = flow.get("approved_target_channel_id") or flow.get("target_channel_id")
            elif expected_status == "rejected":
                flow_target_channel_id = flow.get("rejected_target_channel_id") or flow.get("target_channel_id")
            else:
                flow_target_channel_id = flow.get("target_channel_id")

            submission_message, moved = await _move_message_if_needed(
                submission_message,
                int(flow_target_channel_id) if flow_target_channel_id else None,
            )
            if moved:
                fixes_applied.append("ficha movida para o canal correto de acordo com o status no Discord")

            binding_mismatch = (
                int(target_submission.get("guild_id") or 0) != ctx.guild.id
                or (
                    submission_message is not None
                    and int(target_submission.get("channel_id") or 0) != submission_message.channel.id
                )
                or int(target_submission.get("message_id") or 0)
                != (submission_message.id if submission_message else message_id_int)
            )

            if binding_mismatch:
                bound = fix_form_submission_message_binding(
                    submission_id=int(target_submission["id"]),
                    guild_id=ctx.guild.id,
                    channel_id=submission_message.channel.id if submission_message else int(target_submission.get("channel_id") or 0),
                    message_id=submission_message.id if submission_message else message_id_int,
                )
                if bound:
                    fixes_applied.append("vínculo de guild/canal/mensagem corrigido")
                    binding_mismatch = False

            if submission_message is not None:
                try:
                    if expected_status == "pending":
                        view = build_portaria_view(int(target_submission["id"]))
                        await submission_message.edit(view=view)
                        self.bot.add_view(view, message_id=submission_message.id)
                        fixes_applied.append("botões de aprovação/reprovação restaurados na ficha pendente")
                    else:
                        await submission_message.edit(view=None)
                        fixes_applied.append("botões removidos da ficha já analisada")
                except (discord.Forbidden, discord.HTTPException):
                    pass

            target_submission = get_form_submission(int(target_submission["id"])) or target_submission

        status_after = str(target_submission.get("status") or "desconhecido")
        summary_lines = [
            "🔎 **Diagnóstico da ficha da portaria**",
            f"- Submission ID: `{target_submission['id']}`",
            f"- Message ID: `{message_id_int}`",
            f"- Fluxo: `{flow.get('name')}` (tipo `{flow.get('type')}`)",
            f"- Status no banco: `{status_before}`{' → `' + status_after + '`' if corrigir else ''}",
            f"- Status detectado no Discord: `{discord_status}`",
            (
                "- Checagem de botões no Discord: "
                f"contém_componentes=`{'sim' if discord_checks['has_components'] else 'não'}`; "
                f"botões_esperados=`{'sim' if discord_checks['has_expected_buttons'] else 'não'}`; "
                f"funcionais=`{'sim' if discord_checks['buttons_functional'] else 'não'}`"
            ),
            f"- Vínculo guild/canal/mensagem consistente: `{'não' if binding_mismatch else 'sim'}`",
        ]
        if submission_message_mismatch:
            summary_lines.append(
                "- Atenção: o `submission_id` informado estava vinculado a outra mensagem no banco "
                f"(`{submission_message_before}`) e foi tratado como inconsistência."
            )
        if corrigir:
            summary_lines.append(
                "- Correções aplicadas: "
                + (", ".join(fixes_applied) if fixes_applied else "nenhuma (nada para ajustar)")
            )
            summary_lines.append(
                "- Próximo passo: teste novamente os botões **Aprovar** e **Reprovar** na ficha."
            )

        await ctx.followup.send(
            content="\n".join(summary_lines),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(FormsCog(bot))
