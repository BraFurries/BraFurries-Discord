import logging

from discord.ext import commands
import discord
from discord import app_commands
from typing import Literal
from datetime import datetime
from core.database import includeLocale, getAllLocals, getUsersByLocale
from core.database import includeBirthday, getAllBirthdays, getUserBirthday
from core.database import pooled_connection
from schemas.models.locals import CommonStateAbbrev, OtherStateAbbrev
from core.verifications import verifyDate
from schemas.types.months import MONTH_NAME_TO_NUMBER, Months



class InfoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    registrar = app_commands.Group(name='registrar', description='Comandos de registro')

    async def _send_public_after_ephemeral_defer(self, ctx: discord.Interaction, content: str):
        """Complete and remove the private response before sending publicly."""
        await ctx.edit_original_response(content=content)
        await ctx.delete_original_response()
        return await ctx.followup.send(content=content, ephemeral=False)

    @registrar.command(name='local', description='Registra o seu local')
    @app_commands.describe(local='Abreviação do estado', outro_local='Abreviação de estados menos comuns')
    async def registerLocal(
        self,
        ctx: discord.Interaction,
        local: CommonStateAbbrev | None = None,
        outro_local: OtherStateAbbrev | None = None,
    ):
        if local and outro_local:
            return await ctx.response.send_message(
                content='Informe apenas um tipo de estado por vez.',
                ephemeral=True,
            )

        state = local or outro_local
        if state is None:
            return await ctx.response.send_message(content='Você precisa informar um estado.', ephemeral=True)
        availableLocals = getAllLocals()
        await ctx.response.defer()
        result = includeLocale(ctx.guild.id, state, ctx.user, availableLocals)
        if result:
            for locale in availableLocals:
                if locale['locale_abbrev'] == state:
                    return await ctx.followup.send(content=f'você foi registrado em {locale["locale_name"]}!', ephemeral=False)
        else:
            return await ctx.followup.send(content=f'Não foi possível registrar você! você já está registrado em algum local?', ephemeral=True)

    @app_commands.command(name='furros_na_area', description='Lista todos os furries registrados em um local')
    @app_commands.describe(local='Abreviação do estado', outro_local='Abreviação de estados menos comuns')
    async def listFurries(
        self,
        ctx: discord.Interaction,
        local: CommonStateAbbrev | None = None,
        outro_local: OtherStateAbbrev | None = None,
    ):
        if local and outro_local:
            return await ctx.response.send_message(
                content='Informe apenas um tipo de estado por vez.',
                ephemeral=True,
            )

        state = local or outro_local
        if state is None:
            return await ctx.response.send_message(content='Você precisa informar um estado.', ephemeral=True)
        availableLocals = getAllLocals()
        await ctx.response.defer()
        result = getUsersByLocale(state, availableLocals)
        if result:
            for locale in availableLocals:
                if locale['locale_abbrev'] == state:
                    members = []
                    for member in result:
                        discord_user_id = member.get('discord_user_id')
                        fallback_name = (
                            member.get('display_name')
                            or member.get('username')
                            or member.get('stored_username')
                        )
                        if discord_user_id:
                            guild_member = ctx.guild.get_member(discord_user_id)
                            if guild_member:
                                members.append(guild_member.name)
                                continue
                        if fallback_name:
                            members.append(fallback_name)
                    if not members:
                        return await ctx.followup.send(
                            content=f'Não há furros registrados em {locale["locale_name"]}... que tal ser o primeiro? :3'
                        )

                    membersResponse = ',\n'.join(members)
                    return await ctx.followup.send(content=f'''Aqui estão os furros registrados em {locale["locale_name"]}:```{membersResponse}```''')
        else:
            for locale in availableLocals:
                if locale['locale_abbrev'] == state:
                    return await ctx.followup.send(content=f'Não há furros registrados em {locale["locale_name"]}... que tal ser o primeiro? :3')

    @registrar.command(name='aniversario', description='Registra seu aniversário')
    async def registerBirthday(self, ctx: discord.Interaction, data: str, mencionavel: Literal["sim", "não"]):
        birthdayAsDate = verifyDate(data)
        if not birthdayAsDate:
            return await ctx.response.send_message(content='''Data de nascimento inválida! Você informou uma data nos formatos "dd/mm/aaaa" ou "dd/mm/aa"? <:catsip:851024825333186560>''', ephemeral=True)

        mencionavel = True if mencionavel == "sim" else False
        await ctx.response.defer(ephemeral=True)
        try:
            existing_birthday = getUserBirthday(ctx.guild.id, ctx.user)
            if existing_birthday and existing_birthday != birthdayAsDate:
                return await ctx.followup.send(
                    content=(
                        "Você não tem permissão para alterar sua data de nascimento já registrada. "
                        "Solicite a atualização para a staff."
                    ),
                    ephemeral=True,
                )

            registered = includeBirthday(
                ctx.guild.id,
                birthdayAsDate,
                ctx.user,
                mencionavel,
                None,
                True,
                False,
            )
            if registered:
                return await self._send_public_after_ephemeral_defer(
                    ctx,
                    f'você foi registrado com o aniversário {birthdayAsDate.day:02}/{birthdayAsDate.month:02}!',
                )
            else:
                return await self._send_public_after_ephemeral_defer(
                    ctx,
                    'Algo deu errado... Avise o titio!',
                )
        except ValueError as e:
            # invalid birthdate range or type
            return await ctx.followup.send(content=f'Data de nascimento inválida! {e}', ephemeral=True)
        except Exception as e:
            if isinstance(e, PermissionError):
                return await ctx.followup.send(
                    content='Somente a staff pode alterar uma data de aniversário já cadastrada.',
                    ephemeral=True,
                )
            if str(e).__contains__('Duplicate entry'):
                return await ctx.followup.send(content=f'Você ja está registrado. Caso o seu aniversário não esteja aparecendo na lista, tente usar /{ctx.command.name} com mencionável = Sim', ephemeral=True)
            if str(e).__contains__('Changed Entry'):
                if mencionavel:
                    return await self._send_public_after_ephemeral_defer(
                        ctx,
                        'Seu aniversário foi atualizado para ser mencionável!',
                    )
                else:
                    return await self._send_public_after_ephemeral_defer(
                        ctx,
                        'Seu aniversário foi atualizado para não ser mencionável!',
                    )
            if str(e).__contains__('Birthday divergence:'):
                return await ctx.followup.send(
                    content=(
                        "Existe uma divergência entre o aniversário informado e o já registrado. "
                        "A staff foi notificada para revisar o caso."
                    ),
                    ephemeral=True,
                )
            logging.exception(
                "Erro ao registrar aniversário (guild_id=%s, user_id=%s).",
                ctx.guild.id,
                ctx.user.id,
            )
            return await ctx.followup.send(content='Algo deu errado... Avise o titio!', ephemeral=True)

    @app_commands.command(name='aniversarios', description='Lista todos os aniversários registrados')
    @app_commands.describe(mes='Filtra os aniversários registrados pelo mês informado')
    async def listBirthdays(self, ctx: discord.Interaction, mes: Months):
        await ctx.response.defer()
        result = getAllBirthdays(ctx.guild.id) or []
        month_number = MONTH_NAME_TO_NUMBER[mes]
        result = [
            birthday
            for birthday in result
            if birthday['birth_date'].month == month_number
        ]
        if result:
            for birthday in result:
                birthday['user'] = ctx.guild.get_member(birthday['user_id'])
            birthdaysResponse = ',\n'.join(
                f"{birthday['birth_date'].strftime('%d/%m')} - {birthday['user'].display_name}"
                for birthday in sorted(result, key=lambda birthday: (birthday['birth_date'].month, birthday['birth_date'].day)) if birthday['user'] != None)
            month_display = mes.capitalize()
            return await ctx.followup.send(content=f'Aqui estão os aniversários registrados em **{month_display}**:```{birthdaysResponse}```')
        return await ctx.followup.send(
            content='Não há aniversários registrados para este mês... que tal ser o primeiro? :3'
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(InfoCog(bot))
