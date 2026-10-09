from discord.ext import commands
import discord
import os
from discord import app_commands
from core.database import getAllEvents, getEventsByState, getEventByName
from core.routine_functions import formatSingleEvent, formatEventList, getLocalId
from schemas.models.locals import CommonStateAbbrev, OtherStateAbbrev


class EventCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name='eventos', description='Lista todos os eventos registrados')
    @app_commands.describe(estado='Abreviação do estado para filtrar os eventos', outro_estado='Abreviação de estados menos comuns')
    async def listEvents(
        self,
        ctx: discord.Interaction,
        estado: CommonStateAbbrev | None = None,
        outro_estado: OtherStateAbbrev | None = None,
    ):
        await ctx.response.defer()
        selected_state = estado or outro_estado
        if selected_state:
            locale_id = await getLocalId(selected_state)
            result = getEventsByState(locale_id)
        else:
            result = getAllEvents()
        if result:
            formattedEvents = formatEventList(result)
            eventsResponse = []
            header = "Aqui estão os próximos eventos registrados"
            if selected_state:
                header += f" em {selected_state}"
            else:
                header += " (sem filtro)"
            for event in formattedEvents:
                if not eventsResponse:
                    eventsResponse.append(f'''{header}:\n'''+event+'\n‎')
                else:
                    eventsResponse.append('\n\n'+ event)
                    if event != formattedEvents[-1]:
                        eventsResponse[-1] += '\n‎'
            eventsResponse[-1] += f'''\n\n```Se você quiser ver mais detalhes sobre um evento, use o comando "/evento <nome do evento>"```\nAdicione tambem a nossa agenda de eventos ao seu google agenda e tenha todos os eventos na palma da sua mão! {os.getenv('GOOGLE_CALENDAR_LINK')}'''
            for message in eventsResponse:
                await ctx.followup.send(content=message) if message != eventsResponse[-1] else await ctx.channel.send(content=message)
        else:
            if selected_state:
                return await ctx.followup.send(content=f'Não há eventos registrados em {selected_state}... que tal ser o primeiro? :3')
            return await ctx.followup.send(content=f'Não há eventos registrados... que tal ser o primeiro? :3')

    @app_commands.command(name=f'evento', description=f'Mostra os detalhes de um evento')
    async def showEvent(self, ctx: discord.Interaction, event_name: str):
        if event_name.__len__() < 4:
            return await ctx.response.send_message(content='''Nome do evento inválido! você informou um nome com menos de 4 caracteres? <:catsip:851024825333186560>''', ephemeral=True)
        await ctx.response.defer()
        event = getEventByName(event_name)
        if event:
            eventEmbeded = formatSingleEvent(event)
            return await ctx.followup.send(embed=eventEmbeded)
        else:
            return await ctx.followup.send(content=f'Não há eventos registrados com esse nome. Tem certeza que digitou o nome certo?')


async def setup(bot: commands.Bot):
    await bot.add_cog(EventCog(bot))
