"""Canonical log configuration vocabulary shared by Discord interfaces."""
LOG_TYPE_LABELS = {
    "ban": "Banimentos", "call": "Logs de call", "message_delete": "Mensagens apagadas",
    "message_edit": "Mensagens editadas", "mute": "Castigos / mutes", "profile": "Alterações de perfil", "warn": "Warns",
}
DEFAULT_LOG_TYPES = tuple(LOG_TYPE_LABELS)
LEGACY_CALL_LOG_TYPES = ("voice", "voice_call")

def effective_call_config(get_config, guild_id: int):
    """The canonical row wins even when disabled; legacy rows are fallbacks only."""
    primary = get_config(guild_id, "call")
    if primary is not None:
        return primary
    for log_type in LEGACY_CALL_LOG_TYPES:
        config = get_config(guild_id, log_type)
        if config is not None:
            return config
    return None
