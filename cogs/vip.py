import re
import logging
from io import BytesIO

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from core.auxiliar_functions import edit_role_colors
from core.database import getGuildVipRoleIds, saveCustomRole
from core.notifications import notify_owner_and_user
from core.routine_functions import colorIsAvailable, getVIPConfigurations, vipCustomRoleMutation

logger = logging.getLogger(__name__)


def _get_configured_vip_roles(guild: discord.Guild) -> list[discord.Role]:
    configured_role_ids = getGuildVipRoleIds(guild.id)
    if not configured_role_ids:
        return []

    vip_roles = [guild.get_role(role_id) for role_id in configured_role_ids]
    valid_vip_roles = [role for role in vip_roles if role is not None]
    if not valid_vip_roles:
        logger.warning(
            'Não foi possível encontrar cargos VIP configurados na guild_id=%s: role_ids=%s',
            guild.id,
            configured_role_ids,
        )
    return valid_vip_roles


class VipCog(commands.Cog):
    vip = app_commands.Group(name="vip", description="Comandos relacionados a VIP")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    async def _notify_vip_error(
        self,
        ctx: discord.Interaction,
        error: Exception,
        *,
        use_followup: bool,
        command_params: dict | None = None,
        user_message: str = "Não foi possível concluir sua solicitação no momento. A staff foi notificada.",
        ephemeral: bool = True,
    ):
        await notify_owner_and_user(
            ctx,
            error,
            user_message,
            use_followup=use_followup,
            command_params=command_params,
            ephemeral=ephemeral,
        )

    @staticmethod
    def _has_role_icon_feature(guild: discord.Guild) -> bool:
        return "ROLE_ICONS" in set(guild.features)

    @staticmethod
    def _has_secondary_color_feature(guild: discord.Guild) -> bool:
        role_color_features = {"ROLE_COLORS", "ROLE_GRADIENTS", "ENHANCED_ROLE_COLORS"}
        return bool(role_color_features.intersection(set(guild.features)))

    @staticmethod
    def _is_missing_vip_feature_error(error: Exception) -> bool:
        if isinstance(error, aiohttp.ClientResponseError):
            return error.status in (401, 403)
        if isinstance(error, discord.Forbidden):
            return True
        if isinstance(error, discord.HTTPException):
            return error.status == 403
        return False
        
    @vip.command(name="customizar", description="customiza o cargo VIP do membro")
    @app_commands.describe(
        cor='Cor primária em formato Hex. Use 0 para padrão',
        cor2='Cor secundária em formato Hex. Use 0 para padrão',
        icone='Ícone do cargo (emoji do servidor ou padrão). Use 0 para padrão',
    )
    async def customizeVipRole(self, ctx: discord.Interaction, cor: str = None, cor2: str = None, icone: str = None):
        if cor is None and cor2 is None and icone is None:
            return await ctx.response.send_message(
                content=(
                    'Você precisa informar pelo menos uma cor ou um ícone para customizar o cargo VIP.\n'
                    'Use o valor "0" nos campos para voltar às configurações padrão. '
                ),
                ephemeral=True,
            )
        vip_role_ids = {role.id for role in getVIPConfigurations(ctx.guild)["VIPRoles"] if role is not None}
        userVipRoles = [role.id for role in ctx.user.roles if role.id in vip_role_ids]
        if not userVipRoles:
            return await ctx.response.send_message(content='Você não é vip! você não pode fazer isso', ephemeral=True)

        # keep raw values to detect explicit resets
        raw_cor, raw_cor2, raw_icone = cor, cor2, icone
        command_params = {"cor": raw_cor, "cor2": raw_cor2, "icone": raw_icone}
        emoji = None
        icon_value = None
        # interpret value "0" as a request to reset the field
        cor = None if cor == "0" else cor
        cor2 = None if cor2 == "0" else cor2
        icone = None if icone == "0" else icone

        if cor is not None:
            if not re.match(r'^#(?:[a-fA-F0-9]{3}){1,2}$', cor):
                return await ctx.response.send_message(content='''# Cor invalida!\nVocê precisa informar uma cor no formato Hex (#000000).\nVocê pode procurar por uma cor em https://htmlcolorcodes.com/color-picker/ e testa-la usando o comando "?color #000000"''', ephemeral=False)
            if not await colorIsAvailable(cor, ctx.guild):
                return await ctx.response.send_message(content='''Cor inválida! você precisa informar uma cor que não seja muito parecida com a cor de algum cargo da staff''', ephemeral=True)
        if cor2 is not None:
            if not re.match(r'^#(?:[a-fA-F0-9]{3}){1,2}$', cor2):
                return await ctx.response.send_message(
                    content='Cor 2 inválida! Você precisa informar uma cor no formato Hex (#000000).',
                    ephemeral=True,
                )
            if not await colorIsAvailable(cor2, ctx.guild):
                return await ctx.response.send_message(
                    content='Cor 2 inválida! você precisa informar uma cor que não seja muito parecida com a cor de algum cargo da staff',
                    ephemeral=True,
                )
        if icone is not None:
            if '<' in icone or '>' in icone or ':' in icone:
                try:
                    emoji_id = int(icone.replace('<','').replace('>','').split(':')[2])
                    emoji = ctx.guild.get_emoji(emoji_id)
                except Exception:
                    emoji = None
                if emoji is None:
                    return await ctx.response.send_message(content='''Ícone inválido! apenas emojis do servidor são permitidos''', ephemeral=True)
                icon_value = await emoji.read()
            elif re.match(r"^https?://", icone):
                return await ctx.response.send_message(
                    content='Ícone inválido! apenas emojis do servidor ou emojis padrão são permitidos',
                    ephemeral=True,
                )
            else:
                icon_value = icone
        await ctx.response.defer()
        async with vipCustomRoleMutation(ctx) as customRole:

            if cor2 is not None and not self._has_secondary_color_feature(ctx.guild):
                return await ctx.followup.send(
                    content='Não foi possível aplicar a **cor 2**: este servidor não possui a vantagem necessária para gradiente de cargos VIP.',
                    ephemeral=True,
                )

            if (icone is not None or raw_icone == "0") and not self._has_role_icon_feature(ctx.guild):
                return await ctx.followup.send(
                    content='Não foi possível alterar o **ícone**: este servidor não possui a vantagem necessária para ícones em cargos VIP.',
                    ephemeral=True,
                )

            colors = [cor, cor2]
            colors = [c for c in colors if c is not None]
            image_file = None
            if colors:
                try:
                    await edit_role_colors(self.bot, customRole, colors)
                    rgb_colors = []

                    def hex_to_rgb(value: str):
                        hex_value = value.lstrip('#')
                        if len(hex_value) == 3:
                            hex_value = ''.join([c * 2 for c in hex_value])
                        return tuple(int(hex_value[i : i + 2], 16) for i in (0, 2, 4))

                    for color in colors:
                        rgb_colors.append(hex_to_rgb(color))

                    img = Image.new("RGB", (80, 10))
                    if len(rgb_colors) == 1:
                        img.paste(rgb_colors[0], [0, 0, *img.size])
                    else:
                        width, height = img.size
                        gradient_pixels = []
                        for y in range(height):
                            for x in range(width):
                                ratio = x / (width - 1) if width > 1 else 0
                                blended = tuple(
                                    int(rgb_colors[0][i] + (rgb_colors[1][i] - rgb_colors[0][i]) * ratio)
                                    for i in range(3)
                                )
                                gradient_pixels.append(blended)
                        img.putdata(gradient_pixels)

                    image_buffer = BytesIO()
                    img.save(image_buffer, format="PNG")
                    image_buffer.seek(0)
                    image_file = discord.File(image_buffer, filename="vip_customizacao.png")
                except Exception as e:
                    if cor2 is not None and self._is_missing_vip_feature_error(e):
                        return await ctx.followup.send(
                            content='Não foi possível aplicar a **cor 2**: este servidor não possui a vantagem/permissão necessária para gradiente.',
                            ephemeral=True,
                        )
                    if cor is not None:
                        return await self._notify_vip_error(
                            ctx,
                            e,
                            use_followup=True,
                            command_params=command_params,
                            user_message=f'Não foi possível aplicar a **cor 1** ({cor}) no momento. A staff foi notificada.',
                            ephemeral=True,
                        )
                    return await self._notify_vip_error(
                        ctx,
                        e,
                        use_followup=True,
                        command_params=command_params,
                        user_message='Algo deu errado ao mudar a cor do cargo VIP, avise o titio sobre!',
                        ephemeral=False,
                    )
            elif raw_cor == "0" or raw_cor2 == "0":
                try:
                    await edit_role_colors(self.bot, customRole, [])
                    await customRole.edit(color=discord.Color.default())
                except Exception as e:
                    return await self._notify_vip_error(
                        ctx,
                        e,
                        use_followup=True,
                        command_params=command_params,
                        user_message='Algo deu errado ao mudar a cor do cargo VIP, avise o titio sobre!',
                        ephemeral=False,
                    )
            if icone is not None:
                try:
                    await customRole.edit(display_icon=icon_value)
                except Exception as e:
                    if self._is_missing_vip_feature_error(e):
                        return await ctx.followup.send(
                            content='Não foi possível aplicar o **ícone**: este servidor não possui a vantagem/permissão necessária para ícones em cargos.',
                            ephemeral=True,
                        )
                    return await self._notify_vip_error(
                        ctx,
                        e,
                        use_followup=True,
                        command_params=command_params,
                        user_message=f'Não foi possível aplicar o **ícone** ({icone}) no momento. A staff foi notificada.',
                        ephemeral=True,
                    )
            elif raw_icone == "0":
                try:
                    await customRole.edit(display_icon=None)
                except Exception as e:
                    if self._is_missing_vip_feature_error(e):
                        return await ctx.followup.send(
                            content='Não foi possível remover o **ícone**: este servidor não possui a vantagem/permissão necessária para ícones em cargos.',
                            ephemeral=True,
                        )
                    return await self._notify_vip_error(
                        ctx,
                        e,
                        use_followup=True,
                        command_params=command_params,
                        user_message='Não foi possível remover o **ícone** no momento. A staff foi notificada.',
                        ephemeral=True,
                    )
            changes = []
            if icone is not None:
                changes.append(f"- Ícone: {icone}")
            elif raw_icone == "0":
                changes.append("- Ícone removido")
            if colors:
                changes.append(f"- Cores: {colors}")
            elif raw_cor == "0" or raw_cor2 == "0":
                changes.append("- Cores removidas")

            followup_kwargs = {
                "content": "Cargo VIP personalizado com sucesso!\n" + "\n".join(changes),
                "ephemeral": False,
            }
            if image_file:
                followup_kwargs["files"] = [image_file]

            await ctx.followup.send(**followup_kwargs)
            primary_color = colors[0] if len(colors) > 0 else None
            secondary_color = colors[1] if len(colors) > 1 else None
            return saveCustomRole(
                ctx.guild_id,
                ctx.user,
                color=primary_color,
                color2=secondary_color,
                iconId=emoji.id if 'emoji' in locals() and emoji is not None else None,
                roleId=customRole.id,
            )



async def setup(bot: commands.Bot):
    await bot.add_cog(VipCog(bot))
