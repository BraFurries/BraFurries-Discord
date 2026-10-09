from commands.default_commands import calcular_idade
from datetime import datetime
import asyncio
import re

from core.runtime_config import get_optional_discord_snowflake

LEGACY_PORTARIA_CHANNEL_ID = get_optional_discord_snowflake("CODDY_LEGACY_PORTARIA_CHANNEL_ID")


async def moderate(bot, message):
    if await portaria(bot, message): return True
    return False


async def portaria(bot, message):
    need_correction = False
    if LEGACY_PORTARIA_CHANNEL_ID is not None and message.channel.id == LEGACY_PORTARIA_CHANNEL_ID:  # canal legado
        need_correction = await check_age(bot, message)
        
        return need_correction
    return False
async def check_age(bot, message):
    pattern1 = r'\b(\d{2}/\d{2}/\d{4}|\d{2}\s+de\s+(?:\w*)+\s+de\s+\d{4})\b.*?(?:tenho)?\s?(\d{1,2})\s?(?:anos)\b'  # Regex para encontrar a data seguida de "00 anos"
    pattern1 = re.compile(pattern1, re.IGNORECASE)
    pattern2 = r'\b(?:tenho)?\s?(\d{1,2})\s?(?:anos)\b.*?\b(\d{2}/\d{2}/\d{4}|\d{2}\s+de\s+(?:\w*)+\s+de\s+\d{4})\b'
    pattern2 = re.compile(pattern2, re.IGNORECASE)  # Regex para encontrar "00 anos" seguida da data
    match1 = re.search(pattern1, message.content)
    match2 = re.search(pattern2, message.content)
    if match1:
        data_nascimento = match1.group(1)
        idade = match1.group(2)
    elif match2:
        idade = match2.group(1)
        data_nascimento = match2.group(2)
    if match1 or match2:
        if calcular_idade(data_nascimento) != int(idade) and int(idade) > 0:
            await asyncio.sleep(3)
            await message.channel.send('Sua idade não bate com a data de nascimento que você informou meu querido, corrige isso u.u', reference=message)
            return True
        if idade == 0:
            await asyncio.sleep(3)
            await message.channel.send('Você não pode ter 0 anos, né?', reference=message)
            return True
    if bool(re.search(r'\b(tenho)?\s?(\d{1,2})\s?(anos)\b',message.content,re.IGNORECASE)) != bool(re.search(r'\b(\d{2}/\d{2}/\d{4}|\d{2}\\\d{2}\\\d{4}|\d{2}\s+de\s+(\w*)+\s+de\s+\d{4})\b',message.content,re.IGNORECASE)):
        #testar qual ta dando erro
        await asyncio.sleep(3)
        await message.channel.send('Você precisa informar a sua data de nascimento junto com a sua idade: "X anos", você pode corrigir isso pra gente, por favor?', reference=message)
        await asyncio.sleep(1)
        await message.channel.send('segue esse exemplo: 20 anos, nascido em 01/01/2000')
        return True
    return False