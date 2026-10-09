# Configurações de ambiente
import os

def _legacy_discord_id(name: str) -> int:
    """Fail closed for optional, legacy Discord settings."""
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.isascii() and raw.isdecimal() and len(raw) <= 20 and int(raw) > 0 else 0


def _legacy_discord_ids(name: str) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    parts = [part.strip() for part in raw.split(",")]
    if not all(part.isascii() and part.isdecimal() and len(part) <= 20 and int(part) > 0 for part in parts):
        return []
    return [int(part) for part in parts]


DEBUG = True
ENVIRONMENT = 'development'

# Configurações do bot
BOT_NAME = 'Coddy'
BOT_LANG = 'pt-br'
CHATBOT = True
SOCIAL_MEDIAS=['Discord','Telegram','Instagram']


# Configurações Discord
DISCORD_INTENTS = ['guilds', 'members', 'messages', 'reactions', 'typing', 'presences', 'message_content']
DISCORD_GUILD_ID = _legacy_discord_id("DISCORD_GUILD_ID")
DISCORD_ADMINS = _legacy_discord_ids("DISCORD_ADMINS")
DISCORD_VIP_ROLES_ID = [[role_id] for role_id in _legacy_discord_ids("DISCORD_VIP_ROLES_ID")]
DISCORD_HAS_VIP_CUSTOM_ROLES = True
DISCORD_VIP_CUSTOM_ROLE_PREFIX = 'FurVip ~'
DISCORD_HAS_ROLE_DIVISION = True
DISCORD_VIP_ROLE_DIVISION_START_ID = _legacy_discord_id("DISCORD_VIP_ROLE_DIVISION_START_ID")
DISCORD_VIP_ROLE_DIVISION_END_ID = _legacy_discord_id("DISCORD_VIP_ROLE_DIVISION_END_ID")
DISCORD_MEMBER_NOT_VERIFIED_ROLE = _legacy_discord_id("DISCORD_MEMBER_NOT_VERIFIED_ROLE")
DISCORD_NSFW_ROLE = _legacy_discord_id("DISCORD_NSFW_ROLE")
DISCORD_TEST_CHANNEL = _legacy_discord_id("DISCORD_TEST_CHANNEL")
DISCORD_IS_TESTING = False
DISCORD_BUMP_WARN = True
DISCORD_BUMP_WARNING_TIME = [9,23]
DISCORD_HAS_BUMP_REWARD = True
DISCORD_BOT_PREFIX = '>'
DISCORD_STAFF_COLORS = ['#fff000','#ac75ff','#6bfa60']

# Configurações Instagram
INSTAGRAM_TOKEN = os.getenv("INSTAGRAM_TOKEN", "")

# Configurações do telegram
TELEGRAM_BOT_USERNAME = '@Coddy_The_PetBot'
TELEGRAM_ADMIN = os.getenv("TELEGRAM_ADMIN", "").strip()




