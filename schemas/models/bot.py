from discord.ext import commands
from collections.abc import Awaitable, Callable
import logging


logger = logging.getLogger(__name__)

class Config:
    def __init__(self, config_dict):
        for key, value in config_dict.items():
            if key == 'guildId':
                self.guildId = value
            else:
                setattr(self, key, value)
        self.levels = {}
        self.economy = {}
        self.gptModel = {}

    def __str__(self) -> str:
        return str(self.__dict__)


class MyBot(commands.Bot):
    def __init__(self, config:Config, *args, **kwargs):
        # Prevent discord.py from sleeping indefinitely on a per-route cooldown.
        # The Discord-imposed retry_after is respected; this only bounds local waiting.
        kwargs.setdefault("max_ratelimit_timeout", 120.0)
        super(MyBot, self).__init__(*args, **kwargs)
        self.config = config
        self._startup_callbacks: list[Callable[[], Awaitable[None]]] = []
        self._shutdown_callbacks: list[Callable[[], Awaitable[None]]] = []

    def get_guild_config(self, guild_id: int) -> Config | None:
        if isinstance(self.config, list):
            for guild_config in self.config:
                if getattr(guild_config, "guildId", None) == guild_id:
                    return guild_config
            return None

        if isinstance(self.config, Config) and getattr(self.config, "guildId", None) == guild_id:
            return self.config

        return None

    def add_shutdown_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._shutdown_callbacks.append(callback)

    def add_startup_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._startup_callbacks.append(callback)

    async def setup_hook(self) -> None:
        for callback in list(self._startup_callbacks):
            await callback()

    async def close(self) -> None:
        shutdown_errors: list[Exception] = []

        for callback in list(self._shutdown_callbacks):
            try:
                await callback()
            except Exception as exc:
                logger.exception("Shutdown callback failed during bot close", exc_info=exc)
                shutdown_errors.append(exc)

        await super().close()

        if shutdown_errors:
            raise RuntimeError(
                f"{len(shutdown_errors)} shutdown callback(s) failed while closing the bot"
            ) from shutdown_errors[0]
