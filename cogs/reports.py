from datetime import timedelta
from datetime import timezone
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from core.database import (
    get_portaria_form_decision_report,
    list_portaria_auto_rejected_submissions,
)
from core.time_functions import now
from core.monthly_activity import (
    get_trending_games,
    get_games_for_months,
    previous_months,
    current_month,
)
from core.discord_events import getStaffRoles


class ReportsCog(commands.Cog):
    relatorios = app_commands.Group(name='relatorios', description='Comandos de relatórios')

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    @relatorios.command(name='atividades_em_alta', description='Mostra os jogos mais jogados do mês')
    async def show_trending(self, ctx: discord.Interaction):
        await ctx.response.defer()
        games = get_trending_games(ctx.guild.id)

        if not games:
            await ctx.followup.send(content='Nenhuma atividade registrada neste mês.')
            return

        sorted_games = sorted(games.items(), key=lambda x: x[1], reverse=True)[:10]
        embed = discord.Embed(title='Jogos em alta', color=discord.Color.green())

        for index, (name, seconds) in enumerate(sorted_games, start=1):
            duration = str(timedelta(seconds=seconds))
            embed.add_field(name=f'{index}. {name}', value=duration, inline=False)

        await ctx.followup.send(embed=embed)

    @relatorios.command(name='relatorio_atividades', description='Mostra um relatório de jogos registrados')
    async def activity_report(
        self,
        ctx: discord.Interaction,
        periodo: Literal[
            'mes_atual',
            'ultimo_mes',
            'ultimos_3_meses',
            'ultimos_6_meses',
            'ultimo_ano',
        ] = 'mes_atual',
    ):
        await ctx.response.defer()

        if periodo == 'mes_atual':
            months = [current_month()]
            title = f'Atividades de {months[0]}'
        elif periodo == 'ultimo_mes':
            months = previous_months(2)[1:]
            title = f'Atividades de {months[0]}'
        elif periodo == 'ultimos_3_meses':
            months = previous_months(3)
            title = 'Atividades dos últimos 3 meses'
        elif periodo == 'ultimos_6_meses':
            months = previous_months(6)
            title = 'Atividades dos últimos 6 meses'
        else:  # ultimo_ano
            months = previous_months(12)
            title = 'Atividades do último ano'

        games = get_games_for_months(ctx.guild.id, months)

        if not games:
            await ctx.followup.send(content='Nenhuma atividade registrada.')
            return

        sorted_games = sorted(games.items(), key=lambda x: x[1], reverse=True)[:10]
        embed = discord.Embed(title=title, color=discord.Color.green())
        for index, (name, seconds) in enumerate(sorted_games, start=1):
            duration = str(timedelta(seconds=seconds))
            embed.add_field(name=f'{index}. {name}', value=duration, inline=False)

        await ctx.followup.send(embed=embed)

    @relatorios.command(name='portaria', description='Gera um relatório de atividades na portaria')
    async def portaria_report(self, ctx: discord.Interaction, periodo: Literal['semana', 'mês']):
        if periodo == 'semana':
            initial_date = now() - timedelta(days=7)
        elif periodo == 'mês':
            initial_date = now() - timedelta(days=30)

        final_date = now()
        await ctx.response.send_message(content='Gerando relatório...')
        try:
            staff_roles = getStaffRoles(ctx.guild)
            staff_members = {}
            for role in staff_roles:
                for member in role.members:
                    staff_members[member.id] = member

            members_stats = []
            for staff in staff_members.values():
                members_stats.append({
                    'id': staff.id,
                    'name': staff.display_name,
                    'ticketsAttended': 0,
                })

            report_data = get_portaria_form_decision_report(
                ctx.guild.id,
                initial_date,
                final_date,
            )
            decisions_by_staff = {
                int(item["decided_by"]): int(item["total"])
                for item in report_data.get("decisions_by_staff", [])
            }

            for staff in members_stats:
                staff["ticketsAttended"] = decisions_by_staff.get(staff["id"], 0)

            total_tickets = int(report_data.get("total_decisions", 0))
            total_rejected_tickets = int(report_data.get("total_rejected_decisions", 0))
            total_approved_tickets = max(total_tickets - total_rejected_tickets, 0)
            auto_rejected_submissions = list_portaria_auto_rejected_submissions(
                ctx.guild.id,
                initial_date,
                final_date,
            )
            total_auto_rejected_tickets = len(auto_rejected_submissions)
            average_auto_rejected_account_age = await self._calculate_average_account_age_days(
                ctx,
                auto_rejected_submissions,
            )

            response = 'Relatório de atividades na portaria:\n'
            members_stats = sorted(members_stats, key=lambda m: m["ticketsAttended"], reverse=True)
            for staff in members_stats:
                response += '**{0:32}**  {1:10} tickets atendidos\n'.format(staff["name"], staff["ticketsAttended"])
            response += f'\n**Periodo: {periodo}**'
            response += f'\n**Total de tickets: {total_tickets}**'
            response += f'\n**Total de tickets aprovados: {total_approved_tickets}**'
            response += f'\n**Total de tickets reprovados: {total_rejected_tickets}**'
            response += f'\n**Total de tickets reprovados automaticamente: {total_auto_rejected_tickets}**'
            if average_auto_rejected_account_age is not None:
                response += (
                    '\n**Idade média das contas (recusas automáticas): '
                    f'{average_auto_rejected_account_age:.1f} dias**'
                )
            else:
                response += '\n**Idade média das contas (recusas automáticas): sem dados**'
            await ctx.edit_original_response(content=response)
        except Exception:
            await ctx.edit_original_response(content='Erro ao gerar o relatório!')

    async def _calculate_average_account_age_days(
        self,
        ctx: discord.Interaction,
        auto_rejected_submissions: list[dict],
    ) -> float | None:
        if not auto_rejected_submissions:
            return None

        users_cache: dict[int, discord.abc.User] = {}
        total_age_days = 0
        counted = 0

        for submission in auto_rejected_submissions:
            user_id = int(submission.get("user_id") or 0)
            decided_at = submission.get("decided_at")
            if not user_id or decided_at is None:
                continue

            user = users_cache.get(user_id)
            if user is None:
                user = ctx.guild.get_member(user_id)
                if user is None:
                    try:
                        user = await self.bot.fetch_user(user_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        user = None
                if user is None:
                    continue
                users_cache[user_id] = user

            created_at = getattr(user, "created_at", None)
            if created_at is None:
                continue

            if decided_at.tzinfo is None:
                decided_at = decided_at.replace(tzinfo=timezone.utc)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            age_days = (decided_at.date() - created_at.date()).days
            if age_days < 0:
                continue

            total_age_days += age_days
            counted += 1

        if counted == 0:
            return None
        return total_age_days / counted


async def setup(bot: commands.Bot):
    await bot.add_cog(ReportsCog(bot))
