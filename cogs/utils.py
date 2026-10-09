from core.database import assignTempRole
from datetime import timedelta, datetime
from core.time_functions import now
from core.discord_events import getStaffRoles
from discord import Interaction, Member, Role, app_commands
from discord.ext import commands
import discord
from settings import BOT_NAME
import requests
import os


class UtilsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()
        
    @app_commands.command(name='call_titio', description=f'Faz {BOT_NAME} chamar o titio')
    async def callAdmin(self, ctx: Interaction, message: str):
        requests.post(f"https://api.telegram.org/bot{os.getenv('TELEGRAM_TOKEN')}/sendMessage?chat_id={os.getenv('TELEGRAM_ADMIN')}&text={ctx.user.name}: {message}")
        resp = None
        try:
            resp = await ctx.response.send_message(content='O titio foi avisado! agora é só esperar :3', ephemeral=True)
            return resp
        except Exception:
            if resp:
                channel = await ctx.user.create_dm()
                await channel.send(content='O titio foi avisado! agora é só esperar :3')

    @app_commands.command(name='temp_role', description='Adiciona um cargo temporário a um membro')
    async def addTempRole(self, ctx: Interaction, member: Member, role: Role, duration: str, reason: str = None):
        if not ctx.user.guild_permissions.manage_roles:
            return await ctx.response.send_message(
                content='Apenas membros com permissão de gerenciar cargos podem usar este comando.',
                ephemeral=True,
            )
        staff_roles = getStaffRoles(ctx.guild)
        if role in staff_roles:
            return await ctx.response.send_message(
                content='Não é possível adicionar cargos da staff com este comando.',
                ephemeral=True,
            )
        if role >= ctx.user.top_role:
            return await ctx.response.send_message(
                content='Você só pode adicionar cargos abaixo do seu cargo mais alto.',
                ephemeral=True,
            )
        duration = duration.lower()
        if duration[-1] not in ['d', 's', 'm'] or not duration[:-1].isdigit() or len(duration) < 2:
            return await ctx.response.send_message(content='''Duração inválida! Você informou uma duração no formato "1d", "2s" ou "3m"?\nSiglas: d=dias, s=semanas, m=meses''', ephemeral=True)
        if int(duration[:-1]) == 0:
            return await ctx.response.send_message(content='''Duração inválida! Você informou uma duração maior que 0?''', ephemeral=True)
        if reason is None:
            return await ctx.response.send_message(content='''Você precisa informar um motivo para a adição do cargo''', ephemeral=True)
        if duration[-1] == 'd':
            duration = timedelta(days=int(duration[:-1]))
        elif duration[-1] == 's':
            duration = timedelta(weeks=int(duration[:-1]))
        else:
            duration = timedelta(days=int(duration[:-1]) * 30)
        expiration_date = now() + duration
        await ctx.response.send_message(content='Adicionando o cargo...', ephemeral=True)
        roleAssignment = await assignTempRole(ctx.guild_id, member, role.id, expiration_date, reason)
        if roleAssignment:
            await ctx.edit_original_response(content=f'O membro <@{member.id}> agora tem o cargo {role.name} por {duration}!')
            
async def setup(bot: commands.Bot):    await bot.add_cog(UtilsCog(bot))
