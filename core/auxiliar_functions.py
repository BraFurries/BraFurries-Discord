import aiohttp
from schemas.models.bot import *
import discord
import re

async def edit_role_colors(bot: MyBot, role: discord.Role, colors: list[str]) -> discord.Role:
    """
    PATCH /guilds/{guild_id}/roles/{role_id} with up to three colors.

    Parameters
    ----------
    bot_token : str
        Your bot’s token (the same one you pass to bot.run()).
    guild_id : int
        ID of the guild (server).
    role_id : int
        ID of the role you want to edit.
    hex_colors : list[str]
        A list of 1–3 strings like ["#ff0000", "#00ff00", "#0000ff"].

    Returns
    -------
    dict
        The updated role object returned by Discord.
    """
    url = f"https://discord.com/api/v10/guilds/{role.guild.id}/roles/{role.id}"
    headers = {
        "Authorization": f"Bot {bot.http.token}",
        "Content-Type": "application/json"
    }

    # Map first 3 colors to the fields Discord would use if it supported them.
    # (Currently the public docs only list a single "color" field :contentReference[oaicite:0]{index=0},
    # but Discord’s client UI lets you choose 2-color gradients or even 3-step holographic).
    payload: dict[str, dict[str, int]] = {'colors': {}}
    keys = ["primary_color", "secondary_color", "tertiary_color"]
    for key, color in zip(keys, colors):
        if not re.match(r'^#(?:[a-fA-F0-9]{3}){1,2}$', color):
            raise ValueError("Invalid color format. Use hex format like '#000000'.")
        payload['colors'][key] = int(color.replace('#', '0x'), 16)

    async with aiohttp.ClientSession() as session:
        async with session.patch(url, headers=headers, json=payload) as resp:
            resp.raise_for_status()
            return await resp.json()
    