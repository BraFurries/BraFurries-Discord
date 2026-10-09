from __future__ import annotations

import discord

from core.database import list_form_published_messages


FORM_FLOW_CUSTOM_ID_PREFIX = "form_flow:"


class FormFlowButtonView(discord.ui.View):
    def __init__(self, flow_id: int, label: str = "Abrir formulário") -> None:
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"{FORM_FLOW_CUSTOM_ID_PREFIX}{flow_id}",
        )
        self.add_item(button)


def register_published_form_views(bot: discord.Client) -> None:
    for published in list_form_published_messages():
        flow_id = published.get("flow_id")
        message_id = published.get("message_id")
        if not flow_id or not message_id:
            continue
        try:
            bot.add_view(FormFlowButtonView(int(flow_id)), message_id=int(message_id))
        except Exception:
            continue
