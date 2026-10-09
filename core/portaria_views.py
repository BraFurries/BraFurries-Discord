from __future__ import annotations

from datetime import date, datetime

import discord

from core.database import (
    get_form_flow,
    get_form_submission,
    get_portaria_base_config,
    get_portaria_invite_bypass_codes,
    getUserBirthday,
    get_user_community_status_invite_link,
    includeUser,
    list_pending_portaria_submissions,
    normalize_discord_invite_code,
    record_form_decision,
    rollback_form_decision,
)
from core.discord_events import getStaffRoles
from core.time_functions import now
from core.verifications import extract_date_from_text


PORTARIA_APPROVE_PREFIX = "portaria_approve:"
PORTARIA_REJECT_PREFIX = "portaria_reject:"
BIRTHDAY_KEYWORDS = ("nascimento", "anivers")
class PortariaDecisionView(discord.ui.View):
    def __init__(self, submission_id: int) -> None:
        super().__init__(timeout=None)
        self.submission_id = submission_id

        approve_button = discord.ui.Button(
            label="Aprovar",
            style=discord.ButtonStyle.success,
            custom_id=f"{PORTARIA_APPROVE_PREFIX}{submission_id}",
        )
        reject_button = discord.ui.Button(
            label="Reprovar",
            style=discord.ButtonStyle.danger,
            custom_id=f"{PORTARIA_REJECT_PREFIX}{submission_id}",
        )
        approve_button.callback = self._handle_approve
        reject_button.callback = self._handle_reject
        self.add_item(approve_button)
        self.add_item(reject_button)

    async def _handle_approve(self, interaction: discord.Interaction) -> None:
        await self._handle_decision(interaction, "approved")

    async def _handle_reject(self, interaction: discord.Interaction) -> None:
        await self._handle_decision(interaction, "rejected")

    async def _handle_decision(
        self,
        interaction: discord.Interaction,
        decision: str,
        rejection_reason: str | None = None,
    ) -> None:
        staff_roles = getStaffRoles(interaction.guild) if interaction.guild else []
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None or not any(role in member.roles for role in staff_roles):
            await interaction.response.send_message(
                content="Apenas membros da staff podem registrar decisões.",
                ephemeral=True,
            )
            return

        submission = get_form_submission(self.submission_id)
        if not submission:
            await interaction.response.send_message(
                content="Envio de formulário não encontrado.",
                ephemeral=True,
            )
            return
        if submission["status"] != "pending":
            await interaction.response.send_message(
                content="Este formulário já foi analisado.",
                ephemeral=True,
            )
            return

        if decision == "rejected" and rejection_reason is None and self._is_rejection_feedback_enabled(submission):
            await interaction.response.send_modal(PortariaRejectionFeedbackModal(self))
            return

        await interaction.response.defer(ephemeral=True)

        message = interaction.message
        if message is None and submission.get("message_id"):
            try:
                message = await interaction.channel.fetch_message(
                    int(submission["message_id"])
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None

        routing_error = PortariaDecisionView._validate_routing_requirements(
            interaction,
            submission,
            message,
            decision,
            block_when_approved_move_unavailable=True,
        )
        if routing_error:
            await interaction.followup.send(
                content=routing_error,
                ephemeral=True,
            )
            return

        decision_recorded = False
        if decision == "approved":
            if interaction.guild is None:
                await interaction.followup.send(
                    content="Não foi possível validar o servidor para aprovação.",
                    ephemeral=True,
                )
                return

            member = interaction.guild.get_member(int(submission["user_id"]))
            if member is None:
                try:
                    member = await interaction.guild.fetch_member(int(submission["user_id"]))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None

            if member is None:
                await interaction.followup.send(
                    content="Não foi possível localizar o membro para registro.",
                    ephemeral=True,
                )
                return

            birthday = self._extract_birthday_from_message(message)
            if isinstance(birthday, str):
                await interaction.followup.send(
                    content=birthday,
                    ephemeral=True,
                )
                return

            config = get_portaria_base_config(interaction.guild.id)
            idade_minima_entrada_ativa = bool(config.get("idade_minima_entrada_servidor_ativa"))
            idade_minima_entrada_anos = int(config.get("idade_minima_entrada_servidor_anos") or 0)

            if birthday is None:
                if idade_minima_entrada_ativa:
                    await interaction.followup.send(
                        content=(
                            "Não foi possível aprovar: o servidor exige idade mínima de entrada "
                            "e esta ficha não possui aniversário válido para validação."
                        ),
                        ephemeral=True,
                    )
                    return

                try:
                    record_form_decision(
                        self.submission_id,
                        decision,
                        interaction.user.id,
                    )
                except ValueError as exc:
                    await interaction.followup.send(
                        content=str(exc),
                        ephemeral=True,
                    )
                    return
                decision_recorded = True

                try:
                    includeUser(member, interaction.guild.id, datetime.now())
                except Exception as exc:
                    rollback_ok = rollback_form_decision(
                        self.submission_id,
                        "approved",
                        interaction.user.id,
                    )
                    if rollback_ok:
                        await interaction.followup.send(
                            content=(
                                "Não foi possível registrar o usuário. "
                                "A decisão foi revertida para pendente; você pode tentar novamente. "
                                f"Detalhes: {exc}"
                            ),
                            ephemeral=True,
                        )
                    else:
                        await interaction.followup.send(
                            content=(
                                "A decisão foi registrada, mas houve falha ao registrar o usuário "
                                "e não foi possível reverter automaticamente. "
                                "Revise manualmente o status da ficha e cargos do membro. "
                                f"Detalhes: {exc}"
                            ),
                            ephemeral=True,
                        )
                    return
            else:
                idade_anos = self._calculate_age(birthday, now().date())
                if idade_anos < 13:
                    rejection_reason = (
                        "Recusa automática: membro com menos de 13 anos "
                        "(Discord TOS)."
                    )
                    try:
                        record_form_decision(
                            self.submission_id,
                            "rejected",
                            interaction.client.user.id if interaction.client.user else interaction.user.id,
                        )
                    except ValueError as exc:
                        await interaction.followup.send(content=str(exc), ephemeral=True)
                        return

                    await self._notify_rejection_via_dm(interaction, submission, rejection_reason)
                    routed = await PortariaDecisionView._route_submission_message(
                        interaction,
                        submission,
                        message,
                        "rejected",
                        rejection_reason,
                        actor=interaction.client.user,
                        include_rejection_reason=True,
                    )
                    if not routed:
                        rollback_ok = rollback_form_decision(
                            self.submission_id,
                            "rejected",
                            interaction.client.user.id if interaction.client.user else interaction.user.id,
                        )
                        if rollback_ok:
                            await interaction.followup.send(
                                content=(
                                    "Não foi possível mover a ficha para o canal de recusadas configurado. "
                                    "A decisão foi revertida para pendente."
                                ),
                                ephemeral=True,
                            )
                        else:
                            await interaction.followup.send(
                                content=(
                                    "Não foi possível mover a ficha para o canal de recusadas configurado e "
                                    "a decisão não pôde ser revertida automaticamente. "
                                    "Revise o status manualmente."
                                ),
                                ephemeral=True,
                            )
                        return
                    await interaction.followup.send(
                        content=(
                            "Ficha recusada automaticamente: o membro informado possui menos de 13 anos, "
                            "em conformidade com os Termos do Discord."
                        ),
                        ephemeral=True,
                    )
                    return

                if idade_minima_entrada_ativa and idade_anos < idade_minima_entrada_anos:
                    rejection_reason = (
                        f"Recusa automática: idade abaixo da mínima configurada "
                        f"({idade_minima_entrada_anos} anos)."
                    )
                    try:
                        record_form_decision(
                            self.submission_id,
                            "rejected",
                            interaction.client.user.id if interaction.client.user else interaction.user.id,
                        )
                    except ValueError as exc:
                        await interaction.followup.send(content=str(exc), ephemeral=True)
                        return

                    await self._notify_rejection_via_dm(interaction, submission, rejection_reason)
                    routed = await PortariaDecisionView._route_submission_message(
                        interaction,
                        submission,
                        message,
                        "rejected",
                        rejection_reason,
                        actor=interaction.client.user,
                        include_rejection_reason=True,
                    )
                    if not routed:
                        rollback_ok = rollback_form_decision(
                            self.submission_id,
                            "rejected",
                            interaction.client.user.id if interaction.client.user else interaction.user.id,
                        )
                        if rollback_ok:
                            await interaction.followup.send(
                                content=(
                                    "Não foi possível mover a ficha para o canal de recusadas configurado. "
                                    "A decisão foi revertida para pendente."
                                ),
                                ephemeral=True,
                            )
                        else:
                            await interaction.followup.send(
                                content=(
                                    "Não foi possível mover a ficha para o canal de recusadas configurado e "
                                    "a decisão não pôde ser revertida automaticamente. "
                                    "Revise o status manualmente."
                                ),
                                ephemeral=True,
                            )
                        return
                    await interaction.followup.send(
                        content=(
                            "Ficha recusada automaticamente: o membro não atende a idade mínima "
                            "de entrada configurada para o servidor."
                        ),
                        ephemeral=True,
                    )
                    return

                approval_context = self._resolve_portaria_approval_context(
                    interaction,
                    member,
                )
                if isinstance(approval_context, str):
                    await interaction.followup.send(
                        content=approval_context,
                        ephemeral=True,
                    )
                    return

                age_in_days = (now().date() - birthday).days
                existing_birthday = getUserBirthday(interaction.guild.id, member)
                if existing_birthday and existing_birthday != birthday:
                    view = FormBirthdayConflictView(
                        requester_id=interaction.user.id,
                        submission_id=self.submission_id,
                        member=member,
                        message=message,
                        selected_birthday=birthday,
                        existing_birthday=existing_birthday,
                        approval_context=approval_context,
                    )
                    await interaction.followup.send(
                        content=(
                            f"Existe outro registro de aniversário no banco: "
                            f"{existing_birthday.strftime('%d/%m/%Y')}. "
                            "Deseja manter ou substituir pelo atual?"
                        ),
                        view=view,
                        ephemeral=True,
                    )
                    return

                try:
                    record_form_decision(
                        self.submission_id,
                        decision,
                        interaction.user.id,
                    )
                except ValueError as exc:
                    await interaction.followup.send(
                        content=str(exc),
                        ephemeral=True,
                    )
                    return
                decision_recorded = True

                success = await self._run_full_portaria_approval(
                    interaction,
                    member,
                    birthday,
                    age_in_days,
                    approval_context,
                    move_channel=False,
                )
                if not success:
                    rollback_ok = rollback_form_decision(
                        self.submission_id,
                        "approved",
                        interaction.user.id,
                    )
                    if rollback_ok:
                        await interaction.followup.send(
                            content=(
                                "Não foi possível finalizar a aprovação. "
                                "A decisão foi revertida para pendente; você pode tentar novamente."
                            ),
                            ephemeral=True,
                        )
                    else:
                        await interaction.followup.send(
                            content=(
                                "A decisão foi registrada, mas houve falha ao finalizar a aprovação "
                                "e não foi possível reverter automaticamente. "
                                "Revise manualmente o status da ficha e cargos do membro."
                            ),
                            ephemeral=True,
                        )
                    return

        if not decision_recorded:
            try:
                record_form_decision(self.submission_id, decision, interaction.user.id)
            except ValueError as exc:
                await interaction.followup.send(
                    content=str(exc),
                    ephemeral=True,
                )
                return

        if decision == "rejected":
            await self._notify_rejection_via_dm(interaction, submission, rejection_reason)

        routed = await PortariaDecisionView._route_submission_message(
            interaction,
            submission,
            message,
            decision,
            rejection_reason,
        )
        if not routed:
            rollback_ok = rollback_form_decision(
                self.submission_id,
                decision,
                interaction.user.id,
            )
            if rollback_ok:
                await interaction.followup.send(
                    content=(
                        "Não foi possível mover a ficha para o canal configurado. "
                        "A decisão foi revertida para pendente."
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    content=(
                        "Não foi possível mover a ficha para o canal configurado e "
                        "a decisão não pôde ser revertida automaticamente. "
                        "Revise o status manualmente."
                    ),
                    ephemeral=True,
                )
            return

        await interaction.followup.send(
            content="Decisão registrada com sucesso.",
            ephemeral=True,
        )

    @staticmethod
    async def _route_submission_message(
        interaction: discord.Interaction,
        submission: dict,
        message: discord.Message | None,
        decision: str,
        rejection_reason: str | None = None,
        actor: discord.abc.User | None = None,
        include_rejection_reason: bool | None = None,
    ) -> bool:
        if message is None:
            return False

        should_include_rejection_reason = include_rejection_reason
        if should_include_rejection_reason is None:
            should_include_rejection_reason = (
                decision == "rejected"
                and PortariaDecisionView._is_rejection_feedback_enabled(submission)
            )

        updated_embeds = _build_portaria_decision_embeds(
            message,
            decision,
            actor or interaction.user,
            rejection_reason,
            should_include_rejection_reason,
        )
        destination = PortariaDecisionView._resolve_destination_channel(
            interaction.guild,
            submission,
            decision,
        )
        if destination is None or destination.id == message.channel.id:
            try:
                await message.edit(embeds=updated_embeds or message.embeds, view=None)
                return True
            except (discord.Forbidden, discord.HTTPException):
                return False

        try:
            await destination.send(embeds=updated_embeds or message.embeds)
            await message.delete()
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    @staticmethod
    def _resolve_destination_channel(
        guild: discord.Guild | None,
        submission: dict,
        decision: str,
        require_specific: bool = False,
    ) -> discord.TextChannel | None:
        if guild is None:
            return None

        flow_id = submission.get("flow_id") if isinstance(submission, dict) else None
        if not flow_id:
            return None

        flow = get_form_flow(guild.id, int(flow_id))
        if not isinstance(flow, dict):
            return None
        if decision == "approved":
            destination_channel_id = flow.get("approved_target_channel_id")
        else:
            destination_channel_id = flow.get("rejected_target_channel_id")

        if not destination_channel_id and not require_specific:
            destination_channel_id = flow.get("target_channel_id")

        if not destination_channel_id:
            return None

        channel = guild.get_channel(int(destination_channel_id))
        return channel if isinstance(channel, discord.TextChannel) else None

    @staticmethod
    def _validate_routing_requirements(
        interaction: discord.Interaction,
        submission: dict,
        message: discord.Message | None,
        decision: str,
        block_when_approved_move_unavailable: bool = False,
    ) -> str | None:
        if interaction.guild is None:
            return "Não foi possível validar o servidor para encaminhar a ficha."
        if message is None:
            return "Não foi possível localizar a mensagem da ficha para encaminhamento."

        require_specific_destination = False
        if (
            decision == "approved"
            and block_when_approved_move_unavailable
            and isinstance(submission, dict)
        ):
            flow_id = submission.get("flow_id")
            if flow_id:
                flow = get_form_flow(interaction.guild.id, int(flow_id))
                require_specific_destination = bool(
                    isinstance(flow, dict) and flow.get("approved_target_channel_id")
                )

        destination = PortariaDecisionView._resolve_destination_channel(
            interaction.guild,
            submission,
            decision,
            require_specific=require_specific_destination,
        )
        if require_specific_destination and destination is None:
            return (
                "Não é possível concluir a aprovação: existe um canal de fichas aprovadas "
                "configurado, mas ele não foi encontrado."
            )
        if destination is None:
            return None
        if destination.id == message.channel.id:
            return None

        bot_user = interaction.client.user
        bot_member = interaction.guild.me or (
            interaction.guild.get_member(bot_user.id) if bot_user else None
        )
        if bot_member is None:
            return "Não foi possível validar as permissões do bot no canal de destino."

        permissions = destination.permissions_for(bot_member)
        if not permissions.send_messages:
            return "Não é possível concluir a decisão: o bot não pode enviar mensagens no canal de destino."
        if not permissions.embed_links:
            return "Não é possível concluir a decisão: o bot não pode enviar embeds no canal de destino."

        source_permissions = message.channel.permissions_for(bot_member)
        message_is_from_bot = (
            bot_user is not None
            and message.author is not None
            and message.author.id == bot_user.id
        )
        if not message_is_from_bot and not source_permissions.manage_messages:
            return (
                "Não é possível concluir a decisão: o bot não pode remover a mensagem da ficha "
                "do canal atual para movê-la ao canal configurado."
            )
        return None

    @staticmethod
    async def _notify_rejection_via_dm(
        interaction: discord.Interaction,
        submission: dict,
        rejection_reason: str | None = None,
    ) -> None:
        guild = interaction.guild
        user_id = int(submission["user_id"])

        target: discord.abc.User | None = None
        if guild is not None:
            target = guild.get_member(user_id)
            if target is None:
                try:
                    target = await guild.fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    target = None

        if target is None:
            try:
                target = await interaction.client.fetch_user(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        guild_name = guild.name if guild else "servidor desconhecido"
        reason_text = (rejection_reason or "Não informado.").strip()

        try:
            await target.send(
                f"Sua ficha da portaria foi recusada no servidor **{guild_name}**.\n"
                f"Motivo da reprovação: {reason_text}"
            )
        except (discord.Forbidden, discord.HTTPException):
            return

    @staticmethod
    def _resolve_portaria_approval_context(
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> dict | str:
        guild = interaction.guild
        if guild is None:
            return "Não foi possível validar o servidor para concluir a aprovação."

        moderation_cog = interaction.client.get_cog("ModerationCog")
        if moderation_cog is None:
            return "Não foi possível localizar o módulo de moderação para concluir a aprovação."

        config = get_portaria_base_config(guild.id)
        carteirinha_role_id = config.get("acesso_provisorio_role_id")
        visitante_role_id = config.get("visitante_role_id")
        maior_18_role_id = config.get("maior_18_role_id")
        menor_18_role_id = config.get("menor_18_role_id")
        acesso_provisorio_ativo = bool(config.get("aprovacao_acesso_provisorio_ativo"))
        acesso_provisorio_duracao_dias = int(config.get("aprovacao_acesso_provisorio_duracao_dias") or 15)

        carteirinha_provisoria = guild.get_role(carteirinha_role_id)
        cargo_visitante = guild.get_role(visitante_role_id)
        cargo_maior_18 = guild.get_role(maior_18_role_id)
        cargo_menor_18 = guild.get_role(menor_18_role_id)

        missing_items: list[str] = []
        if carteirinha_provisoria is None:
            missing_items.append("cargo acesso_provisorio")
        if cargo_visitante is None:
            missing_items.append("cargo visitante")
        if cargo_maior_18 is None:
            missing_items.append("cargo maior_18")
        if acesso_provisorio_ativo and acesso_provisorio_duracao_dias <= 0:
            missing_items.append("duração de acesso provisório (dias > 0)")

        if missing_items:
            return (
                "Configuração da portaria incompleta para concluir a aprovação. "
                "Faltando: "
                + ", ".join(missing_items)
                + ". Configure com /admin cargos setar."
            )

        channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None

        if channel is None:
            channel = guild.text_channels[0] if guild.text_channels else None

        if channel is None:
            return "Não foi possível localizar um canal válido para concluir a aprovação."

        conta_dias = (now().date() - member.created_at.date()).days
        idade_minima_prov_ativa = bool(config.get("idade_minima_conta_acesso_provisorio_ativa"))
        idade_minima_prov_dias = int(config.get("idade_minima_conta_acesso_provisorio_dias") or 30)

        if idade_minima_prov_ativa and idade_minima_prov_dias > 0:
            is_new_account = conta_dias < idade_minima_prov_dias
        else:
            is_new_account = conta_dias < 30

        invite_link_used = get_user_community_status_invite_link(guild.id, member)
        bypass_invites = {
            code.casefold() for code in get_portaria_invite_bypass_codes(guild.id)
        }
        if invite_link_used:
            invite_code = normalize_discord_invite_code(invite_link_used)
            if invite_code in bypass_invites:
                is_new_account = False

        return {
            "moderation_cog": moderation_cog,
            "channel": channel,
            "carteirinha_provisoria": carteirinha_provisoria,
            "cargo_visitante": cargo_visitante,
            "cargo_maior_18": cargo_maior_18,
            "cargo_menor_18": cargo_menor_18,
            "acesso_provisorio_ativo": acesso_provisorio_ativo,
            "acesso_provisorio_duracao_dias": acesso_provisorio_duracao_dias,
            "is_new_account": is_new_account,
        }

    @staticmethod
    async def _run_full_portaria_approval(
        interaction: discord.Interaction,
        member: discord.Member,
        birthday: date,
        age_in_days: int,
        approval_context: dict,
        birthday_action: str | None = None,
        move_channel: bool = True,
    ) -> bool:
        try:
            return await approval_context["moderation_cog"]._finalize_portaria_approval(
                interaction,
                member,
                birthday,
                age_in_days,
                approval_context["channel"],
                approval_context["carteirinha_provisoria"],
                approval_context["cargo_visitante"],
                approval_context["cargo_maior_18"],
                approval_context["cargo_menor_18"],
                approval_context["acesso_provisorio_ativo"],
                approval_context["acesso_provisorio_duracao_dias"],
                approval_context["is_new_account"],
                birthday_action,
                update_channel=move_channel,
            )
        except Exception:
            return False

    @staticmethod
    def _calculate_age(birthday: date, today: date) -> int:
        return today.year - birthday.year - (
            (today.month, today.day) < (birthday.month, birthday.day)
        )

    def _extract_birthday_from_message(
        self, message: discord.Message | None
    ) -> date | str | None:
        if message is None or not message.embeds:
            return "Não foi possível localizar as respostas do formulário."

        candidates: list[tuple[str, str]] = []
        for embed in message.embeds:
            for field in embed.fields:
                field_name = (field.name or "").casefold()
                if any(keyword in field_name for keyword in BIRTHDAY_KEYWORDS):
                    candidates.append((field.name, field.value))

        if not candidates:
            return None

        today = datetime.now().date()
        for _, raw_value in candidates:
            response_text = (raw_value or "").strip()
            birthday = extract_date_from_text(response_text)
            if not birthday:
                return (
                    "Data de nascimento inválida. "
                    "Use um formato válido, como dd/mm/aa(aa), dd/mmm/aaaa (ex: 17/out/1996) ou "
                    "d de mês por extenso de aa(aa)."
                )
            if birthday > today:
                return "Data de nascimento inválida: a data não pode estar no futuro."

            age_years = self._calculate_age(birthday, today)
            if age_years > 70:
                return "A data de nascimento informada indica idade acima de 70 anos."

            return birthday

        return "Não foi possível validar a data de nascimento informada."



    @staticmethod
    def _is_rejection_feedback_enabled(submission: dict) -> bool:
        flow_id = submission.get("flow_id")
        if not flow_id:
            return False

        try:
            guild_id = submission.get("guild_id")
            flow = get_form_flow(int(guild_id), int(flow_id))
        except (ValueError, TypeError):
            return False

        if not isinstance(flow, dict):
            return False

        return bool(flow.get("rejection_feedback_enabled"))


class PortariaRejectionFeedbackModal(discord.ui.Modal, title="Feedback da reprovação"):
    rejection_reason = discord.ui.TextInput(
        label="Motivo da reprovação:",
        style=discord.TextStyle.paragraph,
        placeholder=(
            "detalhe para o membro o motivo de sua ficha ter sido recusada. "
            "essa mensagem será enviada a ele"
        ),
        required=True,
        max_length=1000,
    )

    def __init__(self, decision_view: PortariaDecisionView) -> None:
        super().__init__()
        self.decision_view = decision_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.decision_view._handle_decision(
            interaction,
            "rejected",
            str(self.rejection_reason.value).strip(),
        )


class FormBirthdayConflictView(discord.ui.View):
    def __init__(
        self,
        requester_id: int,
        submission_id: int,
        member: discord.Member,
        message: discord.Message | None,
        selected_birthday: date,
        existing_birthday: date,
        approval_context: dict,
    ) -> None:
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.submission_id = submission_id
        self.member = member
        self.message = message
        self.selected_birthday = selected_birthday
        self.existing_birthday = existing_birthday
        self.approval_context = approval_context

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Apenas quem iniciou a aprovação pode escolher uma opção.",
                ephemeral=True,
            )
            return False
        return True

    async def _handle(self, interaction: discord.Interaction, action: str) -> None:
        await interaction.response.defer(ephemeral=True)

        birthday = self.selected_birthday
        age_in_days = (now().date() - birthday).days
        if action == "keep":
            birthday = self.existing_birthday
            age_in_days = (now().date() - birthday).days

        routing_error = PortariaDecisionView._validate_routing_requirements(
            interaction,
            get_form_submission(self.submission_id) or {},
            self.message,
            "approved",
            block_when_approved_move_unavailable=True,
        )
        if routing_error:
            await interaction.followup.send(
                content=routing_error,
                ephemeral=True,
            )
            return

        try:
            record_form_decision(
                self.submission_id,
                "approved",
                interaction.user.id,
            )
        except ValueError as exc:
            await interaction.followup.send(
                content=str(exc),
                ephemeral=True,
            )
            return

        success = await PortariaDecisionView._run_full_portaria_approval(
            interaction,
            self.member,
            birthday,
            age_in_days,
            self.approval_context,
            action,
            move_channel=False,
        )
        if not success:
            rollback_ok = rollback_form_decision(
                self.submission_id,
                "approved",
                interaction.user.id,
            )
            if rollback_ok:
                await interaction.followup.send(
                    content=(
                        "Não foi possível finalizar a aprovação. "
                        "A decisão foi revertida para pendente; você pode tentar novamente."
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    content=(
                        "A decisão foi registrada, mas houve falha ao finalizar a aprovação "
                        "e não foi possível reverter automaticamente. "
                        "Revise manualmente o status da ficha e cargos do membro."
                    ),
                    ephemeral=True,
                )
            return

        routed = await PortariaDecisionView._route_submission_message(
            interaction,
            get_form_submission(self.submission_id) or {},
            self.message,
            "approved",
        )
        if not routed:
            rollback_ok = rollback_form_decision(
                self.submission_id,
                "approved",
                interaction.user.id,
            )
            if rollback_ok:
                await interaction.followup.send(
                    content=(
                        "Não foi possível mover a ficha para o canal configurado. "
                        "A decisão foi revertida para pendente."
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    content=(
                        "Não foi possível mover a ficha para o canal configurado e "
                        "a decisão não pôde ser revertida automaticamente. "
                        "Revise o status manualmente."
                    ),
                    ephemeral=True,
                )
            return

        await interaction.followup.send(
            content="Decisão registrada com sucesso.",
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="Manter", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._handle(interaction, "keep")

    @discord.ui.button(label="Substituir", style=discord.ButtonStyle.primary)
    async def replace(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._handle(interaction, "replace")


def build_portaria_view(
    submission_id: int,
    disabled: bool = False,
) -> PortariaDecisionView:
    view = PortariaDecisionView(submission_id)
    if disabled:
        for item in view.children:
            item.disabled = True
    return view


def _build_portaria_decision_embeds(
    message: discord.Message,
    decision: str,
    actor: discord.abc.User,
    rejection_reason: str | None = None,
    include_rejection_reason: bool = False,
) -> list[discord.Embed] | None:
    if not message.embeds:
        return None

    embeds = list(message.embeds)
    base_embed = discord.Embed.from_dict(embeds[0].to_dict())
    decision_label = "Aprovado por" if decision == "approved" else "Rejeitado por"
    emoji = "✅" if decision == "approved" else "❌"
    base_title = base_embed.title or "Registro da portaria"
    if "Registro da portaria" in base_title:
        base_title = "Registro da portaria"
    base_embed.title = f"{emoji} {base_title}".strip()

    existing_fields = [
        field
        for field in base_embed.fields
        if field.name.casefold() not in {"aprovado por", "rejeitado por", "motivo da reprovação"}
    ]
    base_embed.clear_fields()
    for field in existing_fields:
        base_embed.add_field(
            name=field.name,
            value=field.value,
            inline=field.inline,
        )
    show_rejection_reason = decision == "rejected" and include_rejection_reason
    base_embed.add_field(
        name=decision_label,
        value=actor.mention,
        inline=show_rejection_reason,
    )
    if show_rejection_reason:
        reason_text = (rejection_reason or "Não informado.").strip()
        base_embed.add_field(
            name="Motivo da reprovação",
            value=reason_text,
            inline=True,
        )
    embeds[0] = base_embed
    return embeds


def register_pending_portaria_views(bot: discord.Client) -> None:
    for submission in list_pending_portaria_submissions():
        message_id = submission.get("message_id")
        if not message_id:
            continue
        try:
            bot.add_view(PortariaDecisionView(int(submission["id"])), message_id=message_id)
        except Exception:
            continue
