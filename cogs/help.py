from discord.ext import commands
from discord import app_commands
import discord
from settings import BOT_NAME
from core.discord_events import getStaffRoles

class HelpCog(commands.Cog):
    OVERVIEW = {
        "geral": (
            f"**{BOT_NAME} — visão geral**\n"
            "• **Criador:** Fernando FR (titio).\n"
            "• **Origem:** projeto criado originalmente para a comunidade **BraFurries** no Discord.\n"
            "• **Objetivo:** facilitar gestão, organização e engajamento da comunidade com comandos de utilidade,"
            " moderação, eventos, interação e progressão (XP).\n\n"
            "**Funções disponíveis (resumo):**\n"
            "• Registro de local e aniversários\n"
            "• Eventos comunitários (consulta)\n"
            "• Sistema de XP e ranking\n"
            "• Ferramentas de moderação\n"
            "• Recursos VIP e utilidades\n\n"
            "**Restrições e permissões:**\n"
            "• Algumas funções são exclusivas para staff/moderação/admin.\n"
            "• Certos comandos dependem de cargos/permissões do Discord (ex.: gerenciar cargos).\n"
            "• Configurações podem variar entre servidores conforme a administração local.\n\n"
            "**Privacidade e proteção de dados:**\n"
            "• O bot usa dados mínimos para funcionar (ex.: IDs do Discord e informações fornecidas por você em comandos).\n"
            "• Registros são usados para recursos internos do servidor (ex.: aniversários, local, XP e eventos).\n"
            "• Evite enviar dados sensíveis em comandos. Administradores devem limitar acesso ao banco/logs e ao token do bot.\n\n"
            "Use `/ajuda assunto:<tema>` para detalhes de um assunto específico."
        ),
        "staff": (
            f"**{BOT_NAME} — visão geral da staff**\n"
            "Esta seção reúne comandos operacionais usados pela equipe para moderação,"
            " registro assistido e suporte diário da comunidade.\n\n"
            "**Como usar:**\n"
            "• `/ajuda-staff` para ver este resumo.\n"
            "• `/ajuda-staff assunto:<tema>` para detalhes por tópico.\n\n"
            "**Escopo da staff:**\n"
            "• Comandos sensíveis só aparecem/funcionam para cargos autorizados.\n"
            "• Dono do servidor e administradores também possuem acesso.\n"
            "• Sempre registre ações com motivo para manter auditoria interna."
        ),
        "admin": (
            f"**{BOT_NAME} — visão geral administrativa**\n"
            "Esta seção cobre configuração estrutural do bot no servidor: canais, cargos,"
            " segurança, automações e parâmetros avançados.\n\n"
            "**Como usar:**\n"
            "• `/ajuda-admin` para ver este resumo.\n"
            "• `/ajuda-admin assunto:<tema>` para detalhes por bloco administrativo.\n\n"
            "**Escopo administrativo:**\n"
            "• Comandos `admin` alteram comportamento global do bot no servidor.\n"
            "• Recomenda-se restringir uso apenas a pessoas de confiança.\n"
            "• Revise periodicamente permissões e configurações críticas."
        ),
    }

    TOPICOS_AJUDA = {
            "registro_local": (
                "**Registro de local**\n"
                "**O que faz:** registra o estado/local do membro para facilitar buscas com `furros_na_area`.\n"
                "**Como configurar/usar:** use `/registrar local` e informe um estado válido.\n"
                "**Observação:** cada membro deve registrar apenas um local por vez."
            ),
            "aniversarios": (
                "**Aniversários**\n"
                "**O que faz:** permite registrar e listar aniversários da comunidade.\n"
                "**Como configurar/usar:**\n"
                "• `/registrar aniversario` para registrar o próprio aniversário.\n"
                "• `/aniversarios` para listar por mês.\n"
                "• Staff pode registrar terceiros com comandos administrativos relacionados."
            ),
            "eventos": (
                "**Eventos**\n"
                "**O que faz:** consulta eventos registrados da comunidade.\n"
                "**Como configurar/usar:**\n"
                "• Use `/eventos` e `/evento` para consulta.\n"
                "• O gerenciamento administrativo de eventos não é mais realizado pelo Coddy."
            ),
            "xp": (
                "**Sistema de XP**\n"
                "**O que faz:** acompanha pontuação e ranking dos membros.\n"
                "**Como configurar/usar:**\n"
                "• `/xp` para consultar saldo.\n"
                "• `/xp_ranking` para ranking.\n"
                "• `admin-xp` para ajustes administrativos (adicionar/subtrair/definir)."
            ),
            "vip": (
                "**Recursos VIP**\n"
                "**O que faz:** permite customizações para membros VIP (ex.: identidade visual de cargo).\n"
                "**Como configurar/usar:**\n"
                "• Configure cargos VIP no servidor.\n"
                "• Ajuste o prefixo de cargo VIP nas configurações do bot quando aplicável.\n"
                "• Use comandos de customização disponíveis para membros elegíveis."
            ),
            "moderacao": (
                "**Moderação**\n"
                "**O que faz:** oferece comandos de controle comunitário (avisos, análise de perfil, portaria etc.).\n"
                "**Como configurar/usar:**\n"
                "• Garanta que a equipe tenha permissões adequadas no Discord.\n"
                "• Restrinja comandos sensíveis a cargos de staff.\n"
                "• Revise logs e ações periodicamente para manter consistência."
            ),
            "privacidade": (
                "**Privacidade e dados**\n"
                "O bot utiliza dados operacionais para entregar funcionalidades (IDs, preferências e registros internos).\n"
                "Boas práticas: coletar só o necessário, proteger credenciais, limitar acesso ao banco,"
                " revisar permissões da staff e não solicitar dados sensíveis em comandos públicos."
            ),
            "sobre_o_bot": (
                f"**Sobre o {BOT_NAME}**\n"
                "É um bot multifunção voltado para comunidade furry, originalmente da BraFurries.\n"
                "Combina organização de membros, automações, interação social e apoio à moderação."
            ),
        }
    
    TOPICOS_AJUDA_STAFF = {
        "comandos_disponiveis": (
            "**Comandos disponíveis para staff**\n"
            "• `/chat_analisar_membro` — análise de mensagens por membro.\n"
            "• `/chat_resumir` — resumo de conversa recente de canal.\n"
            "• `/notas_add` — adiciona nota interna em membro.\n"
            "• `/warn` e `/ban` — ações disciplinares.\n"
            "• `/portaria_aprovar` — aprovação manual de entrada.\n"
            "• `/registrar_usuario` — registra usuário e permite informar aprovação/aniversário.\n"
            "• Eventos permanecem disponíveis apenas para consulta pelo Coddy."
        ),
        "habilitados_apenas_staff": (
            "**Comandos habilitados apenas para staff**\n"
            "Estes comandos exigem cargo de staff cadastrado:\n"
            "• `/ajuda-staff`\n"
            "• `/chat_analisar_membro`\n"
            "• `/chat_resumir`\n"
            "• `/notas_add`\n"
            "• `/portaria_aprovar`\n"
            "• Comandos de moderação sensível (`/warn`, `/ban`) conforme regra de permissão local."
        ),
        "boas_praticas": (
            "**Boas práticas da staff**\n"
            "• Sempre informar motivo em ações moderativas.\n"
            "• Evitar exposição de dados sensíveis em canais públicos.\n"
            "• Priorizar registros claros para facilitar revisão da equipe."
        ),
    }
    
    TOPICOS_AJUDA_ADMIN = {
        "comandos_disponiveis": (
            "**Comandos disponíveis para admin**\n"
            "• `/admin ...` (grupo principal de configuração do servidor).\n"
            "• `/admin canais ...` (canais de IA e aniversários).\n"
            "• `/admin cargos|portaria ...` (estrutura da portaria).\n"
            "• `/admin vip ...` e `/admin bump ...`.\n"
            "• `/admin staff ...` (gerenciamento de cargos de staff).\n"
            "• `/admin hashtags ...` e `/admin threads ...`.\n"
            "• `/security ...` (whitelist e pendências sensíveis).\n"
            "• `/ajuda-admin`."
        ),
        "staff_e_permissoes": (
            "**Staff e permissões**\n"
            "• Configure os cargos com `/admin staff registrar`.\n"
            "• Revise com `/admin staff listar` e remova com `/admin staff remover`.\n"
            "• Cargos de staff habilitam comandos restritos da equipe no restante do bot."
        ),
        "seguranca": (
            "**Segurança administrativa**\n"
            "• Limite quem pode usar comandos de configuração global.\n"
            "• Use `/security whitelist_list` para auditar exceções sensíveis.\n"
            "• Revise logs e alterações críticas periodicamente."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    @staticmethod
    def _owner_or_admin_guard(ctx: discord.Interaction) -> bool:
        if ctx.guild is None or not isinstance(ctx.user, discord.Member):
            return False
        if ctx.user.id == ctx.guild.owner_id:
            return True
        return ctx.user.guild_permissions.administrator

    def _return_topic_choices(self, topicos: dict[str, str], current: str) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=nome.replace("_", " ").title(), value=nome)
            for nome in topicos.keys()
            if current.lower() in nome.lower()
        ]

    async def topico_autocomplete_unificado(
        self,
        interaction: discord.Interaction,
        current: str
    ) -> list[app_commands.Choice[str]]:
        nome_comando = interaction.command.name

        if nome_comando == 'ajuda-admin':
            topicos = self.TOPICOS_AJUDA_ADMIN
        elif nome_comando == 'ajuda-staff':
            topicos = self.TOPICOS_AJUDA_STAFF
        else:
            topicos = self.TOPICOS_AJUDA
            
        return self._return_topic_choices(topicos, current)
    
    def _build_overview(self, context: str) -> str:
        return self.OVERVIEW.get(context, "Visão geral do bot.")
    
    async def _return_topic_content(self, ctx: discord.Interaction, topicos: dict[str, str], contexto: str, assunto: str | None) -> str:
        if assunto is None:
            return await ctx.response.send_message(self._build_overview(contexto), ephemeral=True)
        conteudo = topicos[assunto]
        return await ctx.response.send_message(conteudo, ephemeral=True)

    @app_commands.command(name='ajuda', description='Mostra ajuda geral do bot')
    @app_commands.describe(assunto='Assunto para detalhar')
    @app_commands.autocomplete(assunto=topico_autocomplete_unificado)
    async def ajuda(self, ctx: discord.Interaction, assunto: str | None = None):
        return await self._return_topic_content(ctx, self.TOPICOS_AJUDA, "geral", assunto)

    @app_commands.command(name='ajuda-staff', description='Mostra ajuda geral do bot')
    @app_commands.describe(assunto='Assunto para detalhar')
    @app_commands.autocomplete(assunto=topico_autocomplete_unificado)
    async def ajuda_staff(self, ctx: discord.Interaction, assunto: str | None = None):
        if ctx.guild is None or not isinstance(ctx.user, discord.Member):
            return await ctx.response.send_message(
                "Você não tem permissão para usar este comando.",
                ephemeral=True,
            )

        if self._owner_or_admin_guard(ctx):
            return await self._return_topic_content(ctx, self.TOPICOS_AJUDA_STAFF, "staff", assunto)

        staff_roles = getStaffRoles(ctx.guild)
        if not staff_roles:
            return await ctx.response.send_message(
                "Nenhum cargo de staff está configurado no banco para este servidor.",
                ephemeral=True,
            )

        if not any(role in ctx.user.roles for role in staff_roles):
            return await ctx.response.send_message(
                "Você não tem permissão para usar este comando.",
                ephemeral=True,
            )

        return await self._return_topic_content(ctx, self.TOPICOS_AJUDA_STAFF, "staff", assunto)

    @app_commands.command(name='ajuda-admin', description='Mostra ajuda para comandos administrativos (staff)')
    @app_commands.describe(assunto='Assunto para detalhar')
    @app_commands.autocomplete(assunto=topico_autocomplete_unificado)
    async def ajuda_admin(self, ctx: discord.Interaction, assunto: str | None = None):
        if not self._owner_or_admin_guard(ctx):
            return await ctx.response.send_message(
                "Você não tem permissão para usar este comando.",
                ephemeral=True,
            )
        return await self._return_topic_content(ctx, self.TOPICOS_AJUDA_ADMIN, "admin", assunto)

async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
