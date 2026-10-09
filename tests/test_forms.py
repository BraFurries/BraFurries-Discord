import asyncio

import discord

from cogs.forms import (
    EMBED_FIELD_NAME_LIMIT,
    EMBED_FIELD_VALUE_LIMIT,
    EMBED_TOTAL_LIMIT,
    FormFlowModal,
    _embed_character_count,
    _limited_field_name,
    _response_length_limits,
)


def _embed() -> discord.Embed:
    embed = discord.Embed(title="Ficha")
    embed.set_footer(text="ID: 123")
    return embed


async def _build_form_flow_modal(flow_data, questions):
    return FormFlowModal(flow_data, questions, object())


def test_modal_with_one_question_allows_full_embed_field_length():
    modal = asyncio.run(
        _build_form_flow_modal(
            {"name": "Teste", "type": "normal"},
            [{"id": 1, "question_text": "Conte sobre você", "required": True}],
        )
    )

    assert modal.inputs[1].max_length == EMBED_FIELD_VALUE_LIMIT


def test_response_with_exactly_1024_characters_uses_one_field():
    embed = _embed()
    response = "a" * 1024
    embed.add_field(name="Resposta", value=response)

    assert len(embed.fields) == 1
    assert embed.fields[0].value == response


def test_long_question_is_limited_to_discord_field_name_size():
    field_name = _limited_field_name("pergunta " * 100)

    assert len(field_name) <= EMBED_FIELD_NAME_LIMIT
    assert field_name.endswith("...")


def test_five_fields_are_limited_to_fit_in_single_embed():
    questions = [
        {"id": index, "question_text": f"Pergunta {index}" + "?" * 245}
        for index in range(1, 6)
    ]
    flow_data = {"name": "Ficha grande", "type": "portaria"}
    limits = _response_length_limits(flow_data, questions)
    modal = asyncio.run(_build_form_flow_modal(flow_data, questions))
    embed = discord.Embed(title="Registro da portaria")
    embed.set_footer(text="ID: 12345678901234567890")
    embed.add_field(name="Respondente", value="x" * 128, inline=False)

    for question in questions:
        embed.add_field(
            name=_limited_field_name(question["question_text"]),
            value="x" * limits[question["id"]],
            inline=False,
        )

    assert all(
        modal.inputs[question["id"]].max_length == limits[question["id"]]
        for question in questions
    )
    assert all(limit <= EMBED_FIELD_VALUE_LIMIT for limit in limits.values())
    assert _embed_character_count(embed) <= EMBED_TOTAL_LIMIT
