import asyncio
import math
import random
import time
import discord
from discord import app_commands
from discord.ext import commands
from core import database

DISCORD_REACTION_DELAY_MS = 200


class BossSummonModal(discord.ui.Modal, title="Invocar novo boss"):
    boss_name = discord.ui.TextInput(label="Nome do boss", min_length=2, max_length=120, placeholder="Ex.: Fenrir Ancestral")
    hp = discord.ui.TextInput(label="HP do boss", min_length=3, max_length=7, placeholder="Ex.: 5000")

    def __init__(self, cog: "PlayCommandsCog"):
        super().__init__(timeout=120)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("Use este comando em servidor.", ephemeral=True)
        hp_raw = str(self.hp.value).strip()
        if not hp_raw.isdigit() or not (100 <= int(hp_raw) <= 1000000):
            return await interaction.response.send_message("HP inválido. Use um valor entre 100 e 1000000.", ephemeral=True)
        database.create_or_reset_boss_event(interaction.guild.id, int(hp_raw), boss_name=str(self.boss_name.value).strip())
        embed = self.cog._build_boss_embed(interaction.guild.id, last_attack=None)
        await interaction.response.send_message("✅ Novo boss invocado!", embed=embed)


class BossBattleView(discord.ui.View):
    def __init__(self, cog: "PlayCommandsCog", guild_id: int, has_active_boss: bool, can_summon: bool):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        if not has_active_boss:
            self.remove_item(self.attack)
        if can_summon:
            self.add_item(self.SummonBossButton(cog))
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "boss_attack":
                child.disabled = True
                child.label = "Atacar (encerrado)"
        if self.message is not None:
            embed = self.message.embeds[0] if self.message.embeds else self.cog._build_boss_embed(self.guild_id)
            embed.set_footer(text="A janela de ataque encerrou. Execute /play coop boss novamente para atacar.")
            await self.message.edit(embed=embed, view=self)

    class SummonBossButton(discord.ui.Button):
        def __init__(self, cog: "PlayCommandsCog"):
            super().__init__(label="Invocar novo boss", style=discord.ButtonStyle.primary)
            self.cog = cog

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(BossSummonModal(self.cog))

    @discord.ui.button(label="Atacar", style=discord.ButtonStyle.danger, custom_id="boss_attack")
    async def attack(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog._handle_boss_attack(interaction, self)

class RaidLobbyView(discord.ui.View):
    def __init__(self, cog: "PlayCommandsCog", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.message: discord.Message | None = None

    async def _change_membership(self, interaction: discord.Interaction, joining: bool):
        state = self.cog.raid_states.get(self.guild_id)
        if not state or not state.get("active"):
            return await interaction.response.send_message("Não há raid ativa.", ephemeral=True)
        if state.get("status") != "lobby":
            return await interaction.response.send_message("A fase de entrada já terminou.", ephemeral=True)
        if joining:
            state["participants"].add(interaction.user.id)
            msg = "Você entrou na raid! Seu painel efêmero já está disponível e ativa quando a raid começar."
            await interaction.response.send_message(msg, ephemeral=True, view=RaidBattleView(self.cog, self.guild_id))
        else:
            state["participants"].discard(interaction.user.id)
            msg = "Você saiu da raid."
            await interaction.response.send_message(msg, ephemeral=True)
        if self.message is not None:
            await self.message.edit(embed=self.cog._build_raid_embed(self.guild_id), view=self)

    @discord.ui.button(label="Entrar", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._change_membership(interaction, joining=True)

    @discord.ui.button(label="Sair", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._change_membership(interaction, joining=False)


class RaidBattleView(discord.ui.View):
    def __init__(self, cog: "PlayCog", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.message: discord.Message | None = None

    async def _submit_action(self, interaction: discord.Interaction, action: str):
        ok, msg = self.cog._register_raid_action(self.guild_id, interaction.user.id, action)
        if ok:
            await interaction.response.defer(ephemeral=True)
            return
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="Atacar", style=discord.ButtonStyle.danger)
    async def attack(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._submit_action(interaction, "attack")

    @discord.ui.button(label="Defender", style=discord.ButtonStyle.secondary)
    async def defend(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._submit_action(interaction, "defend")

    @discord.ui.button(label="Curar", style=discord.ButtonStyle.success)
    async def heal(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._submit_action(interaction, "heal")


class RitualView(discord.ui.View):
    def __init__(self, cog: "PlayCog", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(label="Canalizar energia", style=discord.ButtonStyle.primary, custom_id="ritual_channel")
    async def channel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog._ritual_action(interaction, action="channel")

    @discord.ui.button(label="Estabilizar", style=discord.ButtonStyle.secondary, custom_id="ritual_stabilize")
    async def stabilize(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog._ritual_action(interaction, action="stabilize")

    @discord.ui.button(label="Recompensas", style=discord.ButtonStyle.success, custom_id="ritual_rewards")
    async def rewards(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "🎁 Concluir o ritual invoca um boss especial com recompensas aumentadas (+40%).",
            ephemeral=True,
        )


class RitualBossActiveConfirmView(discord.ui.View):
    def __init__(self, cog: "PlayCog", guild_id: int, requester_id: int):
        super().__init__(timeout=45)
        self.cog = cog
        self.guild_id = guild_id
        self.requester_id = requester_id

    @discord.ui.button(label="Iniciar assim mesmo", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            return await interaction.response.send_message("Apenas quem iniciou o comando pode confirmar.", ephemeral=True)
        started, message = await self.cog._start_ritual_for_guild(interaction)
        if started:
            self.stop()
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            return await interaction.response.send_message("Apenas quem iniciou o comando pode cancelar.", ephemeral=True)
        await interaction.response.edit_message(content="Ritual cancelado.", embed=None, view=None)
        self.stop()



class DuelInviteView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member | None):
        super().__init__(timeout=30)
        self.challenger = challenger
        self.opponent = opponent
        self.accepted: bool | None = None
        self.accepted_user: discord.Member | None = None
        self.accept_interaction: discord.Interaction | None = None

    async def disable_buttons(self, interaction: discord.Interaction | None = None):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if interaction and interaction.response.is_done() is False:
            await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Aceitar", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.bot:
            return await interaction.response.send_message(
                "Bots não podem aceitar duelos.", ephemeral=True
            )

        if interaction.user.id == self.challenger.id:
            return await interaction.response.send_message(
                "Você não pode aceitar o próprio duelo.", ephemeral=True
            )

        if self.opponent and interaction.user.id != self.opponent.id:
            return await interaction.response.send_message(
                "Apenas o membro convidado pode aceitar este duelo.", ephemeral=True
            )
        self.accepted = True
        self.accepted_user = interaction.user
        self.accept_interaction = interaction
        await interaction.response.send_message("Convite aceito!", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Recusar", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.opponent:
            if interaction.user.id != self.opponent.id:
                return await interaction.response.send_message(
                    "Apenas o membro convidado pode recusar este duelo.", ephemeral=True
                )
        elif interaction.user.id != self.challenger.id:
            return await interaction.response.send_message(
                "Apenas o desafiante pode cancelar este convite.", ephemeral=True
            )
        self.accepted = False
        await interaction.response.send_message("Convite recusado.", ephemeral=True)
        await self.disable_buttons(interaction)
        self.stop()

    async def on_timeout(self) -> None:
        if self.accepted is None:
            self.accepted = False
        self.stop()


class ExpeditionLobbyView(discord.ui.View):
    def __init__(self, cog: "PlayCommandsCog", guild_id: int, owner_id: int, ends_at: int):
        super().__init__(timeout=max(5, ends_at - int(time.time())))
        self.cog = cog
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.ends_at = ends_at
        self.message: discord.Message | None = None

    async def _sync_participants(self, user_id: int, joining: bool) -> tuple[bool, str]:
        result = database.update_expedition_participants_atomic(self.guild_id, user_id, joining)
        if result.get("ok"):
            return True, "Você entrou na expedição!" if joining else "Você saiu da expedição."
        reason = result.get("reason")
        if reason == "closed":
            return False, "O período de entrada já foi encerrado."
        if reason == "missing":
            return False, "Você não está na lista de participantes."
        return False, "Não há expedição ativa."

    @discord.ui.button(label="Entrar", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, _: discord.ui.Button):
        ok, msg = await self._sync_participants(interaction.user.id, joining=True)
        await interaction.response.send_message(msg, ephemeral=True)
        if ok:
            await self.cog._refresh_expedition_lobby(self.guild_id, self.message)

    @discord.ui.button(label="Sair", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button):
        ok, msg = await self._sync_participants(interaction.user.id, joining=False)
        await interaction.response.send_message(msg, ephemeral=True)
        if ok:
            await self.cog._refresh_expedition_lobby(self.guild_id, self.message)

    async def on_timeout(self) -> None:
        await self.cog._finalize_expedition(self.guild_id, message=self.message)


class DuelAttackView(discord.ui.View):
    def __init__(
        self,
        challenger: discord.Member,
        opponent: discord.Member,
        counts: dict[int, int],
        duel_message: discord.Message,
        embed_builder,
    ):
        super().__init__(timeout=None)
        self.challenger = challenger
        self.opponent = opponent
        self.counts = counts
        self.duel_message = duel_message
        self.embed_builder = embed_builder
        self.ephemeral_message: discord.Message | None = None
        self.finished = False

    def set_ephemeral_message(self, message: discord.Message):
        self.ephemeral_message = message

    def mark_finished(self):
        self.finished = True
        self.stop()

    @discord.ui.button(label="Atacar", style=discord.ButtonStyle.danger)
    async def attack(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.finished:
            return await interaction.response.send_message(
                "O duelo já foi encerrado.", ephemeral=True
            )
        if interaction.user.id not in (self.challenger.id, self.opponent.id):
            return await interaction.response.send_message(
                "Você não participa deste duelo.", ephemeral=True
            )
        self.counts[interaction.user.id] += 1
        if self.embed_builder and self.duel_message:
            embed = self.embed_builder(self.counts, ongoing=True)
            await self.duel_message.edit(embed=embed)
        await interaction.response.defer()


class RpsDuelState:
    def __init__(self, challenger: discord.Member, opponent: discord.Member):
        self.challenger = challenger
        self.opponent = opponent
        self.choices: dict[int, str] = {}
        self.result_text: str | None = None
        self.duel_message: discord.Message | None = None
        self.embed_builder = None
        self.done = asyncio.Event()

    def calculate_result(self) -> str:
        challenger_choice = self.choices.get(self.challenger.id)
        opponent_choice = self.choices.get(self.opponent.id)

        if not challenger_choice or not opponent_choice:
            return "Duelo em andamento."

        if challenger_choice == opponent_choice:
            return "O duelo terminou em empate!"

        wins_against = {"pedra": "tesoura", "papel": "pedra", "tesoura": "papel"}
        if wins_against[challenger_choice] == opponent_choice:
            return f"{self.challenger.mention} venceu o duelo!"
        return f"{self.opponent.mention} venceu o duelo!"


class RpsDuelView(discord.ui.View):
    def __init__(self, state: RpsDuelState):
        super().__init__(timeout=None)
        self.state = state
        self.ephemeral_message: discord.Message | None = None
        self.display_choices = {
            "pedra": "Pedra",
            "papel": "Papel",
            "tesoura": "Tesoura",
        }

    def set_ephemeral_message(self, message: discord.Message):
        self.ephemeral_message = message

    async def _handle_choice(self, interaction: discord.Interaction, choice_key: str):
        if interaction.user.id not in (self.state.challenger.id, self.state.opponent.id):
            return await interaction.response.send_message(
                "Você não participa deste duelo.", ephemeral=True
            )

        if interaction.user.id in self.state.choices:
            return await interaction.response.send_message(
                "Você já escolheu sua opção.", ephemeral=True
            )

        self.state.choices[interaction.user.id] = choice_key

        reveal_choices = len(self.state.choices) == 2
        if reveal_choices:
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            self.state.result_text = self.state.calculate_result()
            self.state.done.set()

        if self.state.embed_builder and self.state.duel_message:
            embed = self.state.embed_builder(
                self.state.choices, reveal_choices, self.state.result_text
            )
            await self.state.duel_message.edit(embed=embed)
        await interaction.response.defer()

        if reveal_choices:
            self.stop()

    @discord.ui.button(label="Pedra", style=discord.ButtonStyle.primary)
    async def rock(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._handle_choice(interaction, "pedra")

    @discord.ui.button(label="Papel", style=discord.ButtonStyle.primary)
    async def paper(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._handle_choice(interaction, "papel")

    @discord.ui.button(label="Tesoura", style=discord.ButtonStyle.primary)
    async def scissors(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._handle_choice(interaction, "tesoura")


class BoopView(discord.ui.View):
    def __init__(self, cog: "PlayCog", booper: discord.Member, target: discord.Member):
        super().__init__(timeout=120)
        self.cog = cog
        self.booper = booper
        self.target = target

    @discord.ui.button(label="Retribuir", style=discord.ButtonStyle.primary)
    async def retribuir(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message(
                "Apenas quem recebeu o boop pode retribuir.", ephemeral=True
            )

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(view=self)
        await self.cog.handle_boop(interaction, interaction.user, self.booper)


def _quickdraw_medal(metric_ms: float) -> str:
    if metric_ms < 180:
        return "🥇 A"
    if metric_ms < 240:
        return "🥈 B"
    if metric_ms < 320:
        return "🥉 C"
    return "🎖️ D"


def calculate_quickdraw_result(
    reaction_ms: float | None, false_starts: int
) -> dict[str, float | int | str | None]:
    penalty_points = false_starts * 30
    base_points = (
        max(0, 100 - math.floor(reaction_ms / 15)) if reaction_ms is not None else 0
    )
    metric_ms = (reaction_ms if reaction_ms is not None else 1500) + penalty_points
    final_points = max(0, base_points - penalty_points)
    return {
        "reaction_ms": reaction_ms,
        "false_starts": false_starts,
        "base_points": base_points,
        "penalty_points": penalty_points,
        "final_points": final_points,
        "metric_ms": metric_ms,
        "medal": _quickdraw_medal(metric_ms),
    }


class QuickDrawSoloView(discord.ui.View):
    def __init__(self, player: discord.Member):
        super().__init__(timeout=None)
        self.player = player
        self.false_starts = 0
        self.signal_given = False
        self.signal_time: float | None = None
        self.reaction_ms: float | None = None
        self.finished = False
        self.ephemeral_message: discord.Message | None = None
        self.done = asyncio.Event()
        self.result: dict[str, float | int | str | None] | None = None

    def set_message(self, message: discord.Message):
        self.ephemeral_message = message

    def grant_signal(self):
        self.signal_given = True
        self.signal_time = asyncio.get_running_loop().time()

    def mark_timeout(self):
        if self.finished:
            return
        self.result = calculate_quickdraw_result(None, self.false_starts)
        self.finished = True
        self.done.set()

    def _status_text(self, signal_active: bool | None = None) -> str:
        if signal_active is None:
            signal_active = self.signal_given
        penalty_text = f"Penalidade acumulada: -{self.false_starts * 30} pts"
        if self.finished and self.result:
            reaction_display = (
                f"{self.result['reaction_ms']:.0f} ms"
                if self.result.get("reaction_ms")
                else "Nenhum saque"
            )
            return (
                f"⏱️ Tempo de reação: {reaction_display}\n"
                f"🚫 Falsos starts: {self.false_starts} (-{self.result['penalty_points']} pts)\n"
                f"🏅 Medalha: {self.result['medal']} | Score: {self.result['metric_ms']:.0f} ms\n"
                f"Pontuação final: {self.result['final_points']}"
            )

        wait_text = "🔥 SAQUE!" if signal_active else "⚠️ Aguarde o sinal..."
        return (
            f"{wait_text}\n\n"
            "Clique apenas quando o sinal de fogo aparecer. "
            "Aperte cedo demais e você perderá 30 pontos.\n"
            f"{penalty_text}"
        )

    def _disable_buttons(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def _refresh_message(self):
        if self.ephemeral_message:
            await self.ephemeral_message.edit(content=self._status_text(), view=self)

    @discord.ui.button(label="Sacar", emoji="🔫", style=discord.ButtonStyle.danger)
    async def draw(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message(
                "Este jogo é só para quem iniciou o duelo.", ephemeral=True
            )
        if self.finished:
            return await interaction.response.send_message(
                "O resultado já foi definido.", ephemeral=True
            )

        if not self.signal_given:
            self.false_starts += 1
            self._disable_buttons()
            await interaction.response.edit_message(
                content=(
                    "🚫 Muito cedo! -30 pontos aplicados.\n"
                    f"Penalidade atual: -{self.false_starts * 30} pts."
                ),
                view=self,
            )
            await asyncio.sleep(0.3)
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = False
            return await self._refresh_message()

        if self.reaction_ms is not None and self.result:
            return await interaction.response.send_message(
                "Seu saque já foi registrado!", ephemeral=True
            )

        if not self.signal_time:
            return await interaction.response.send_message(
                "O sinal ainda não foi registrado.", ephemeral=True
            )

        reaction_ms = (asyncio.get_running_loop().time() - self.signal_time) * 1000
        self.reaction_ms = max(reaction_ms - DISCORD_REACTION_DELAY_MS, 0)
        self.result = calculate_quickdraw_result(self.reaction_ms, self.false_starts)
        self.finished = True
        self.done.set()
        self._disable_buttons()
        await interaction.response.edit_message(
            content=self._status_text(signal_active=True), view=self
        )

    async def finalize(self):
        if not self.finished:
            self.mark_timeout()
        self._disable_buttons()
        await self._refresh_message()
        self.stop()


class QuickDrawDuelState:
    def __init__(self, challenger: discord.Member, opponent: discord.Member):
        self.participants = (challenger, opponent)
        self.results: dict[int, dict[str, float | int | str | None]] = {
            challenger.id: {"reaction_ms": None, "false_starts": 0, "final": None},
            opponent.id: {"reaction_ms": None, "false_starts": 0, "final": None},
        }
        self.signal_given = False
        self.signal_time: float | None = None

    def grant_signal(self):
        self.signal_given = True
        self.signal_time = asyncio.get_running_loop().time()

    def register_false_start(self, user_id: int):
        if user_id in self.results:
            self.results[user_id]["false_starts"] += 1

    def register_reaction(self, user_id: int):
        if not self.signal_time or user_id not in self.results:
            return None
        data = self.results[user_id]
        if data["final"]:
            return data["final"]
        reaction_ms = (asyncio.get_running_loop().time() - self.signal_time) * 1000
        adjusted_reaction = max(reaction_ms - DISCORD_REACTION_DELAY_MS, 0)
        data["reaction_ms"] = adjusted_reaction
        data["final"] = calculate_quickdraw_result(
            adjusted_reaction, int(data["false_starts"])
        )
        return data["final"]

    def finalize_pending(self):
        for user_id, data in self.results.items():
            if data.get("final") is None:
                data["final"] = calculate_quickdraw_result(
                    data.get("reaction_ms"), int(data.get("false_starts", 0))
                )

    def get_result(self, user: discord.Member) -> dict[str, float | int | str | None]:
        return self.results.get(user.id, {}).get("final") or {}


class QuickDrawDuelView(discord.ui.View):
    def __init__(self, state: QuickDrawDuelState, player: discord.Member):
        super().__init__(timeout=None)
        self.state = state
        self.player = player
        self.ephemeral_message: discord.Message | None = None
        self.finished = False

    def set_message(self, message: discord.Message):
        self.ephemeral_message = message

    def _status_text(self) -> str:
        data = self.state.results.get(self.player.id, {})
        false_starts = int(data.get("false_starts", 0))
        penalty = false_starts * 30
        final = data.get("final")
        if final:
            reaction_display = (
                f"{final['reaction_ms']:.0f} ms" if final.get("reaction_ms") else "Nenhum saque"
            )
            return (
                f"Resultado do saque rápido:\n"
                f"⏱️ Tempo: {reaction_display}\n"
                f"🚫 Falsos starts: {false_starts} (-{penalty} pts)\n"
                f"Pontuação final: {final['final_points']} | Medalha: {final['medal']}"
            )

        wait_text = "🔥 SAQUE!" if self.state.signal_given else "⚠️ Aguarde o sinal..."
        return (
            f"{wait_text}\n\n"
            "Clique somente após o sinal. Falsos starts aplicam -30 pontos.\n"
            f"Penalidade atual: -{penalty} pts"
        )

    def _disable_buttons(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def _refresh_message(self):
        if self.ephemeral_message:
            await self.ephemeral_message.edit(content=self._status_text(), view=self)

    @discord.ui.button(label="Sacar", emoji="🔫", style=discord.ButtonStyle.primary)
    async def draw(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message(
                "Você não faz parte deste duelo.", ephemeral=True
            )
        if self.finished:
            return await interaction.response.send_message(
                "O duelo já terminou.", ephemeral=True
            )

        if not self.state.signal_given:
            self.state.register_false_start(self.player.id)
            return await interaction.response.edit_message(
                content=(
                    "🚫 Muito cedo! Você perdeu 30 pontos.\n"
                    f"Penalidade: -{self.state.results[self.player.id]['false_starts'] * 30} pts"
                ),
                view=self,
            )

        result = self.state.register_reaction(self.player.id)
        if not result:
            return await interaction.response.send_message(
                "O sinal ainda não foi registrado.", ephemeral=True
            )
        if interaction.response.is_done():
            await interaction.followup.send(
                content=self._status_text(), ephemeral=True, wait=True
            )
        else:
            await interaction.response.edit_message(
                content=self._status_text(), view=self
            )
        self._disable_buttons()
        self.finished = True

    async def finalize(self):
        self._disable_buttons()
        await self._refresh_message()
        self.stop()


class PlayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_minigames: set[tuple[int, int]] = set()
        self.minigame_cooldowns: dict[tuple[int, int], float] = {}
        self.expedition_tasks: dict[int, asyncio.Task] = {}
        self.expedition_finalize_locks: dict[int, asyncio.Lock] = {}
        self.raid_states: dict[int, dict] = {}
        self.raid_tasks: dict[int, asyncio.Task] = {}
        self.raid_locks: dict[int, asyncio.Lock] = {}
        self.ritual_states: dict[int, dict] = {}
        self.ritual_tasks: dict[int, asyncio.Task] = {}
        self.trivia_questions: list[dict[str, str | list[str]]] = [
            {
                "question": "No furry fandom, como é chamado o personagem que representa você?",
                "correct": "Fursona",
                "options": ["Fursona", "Avatar arcano", "OC fixo", "Mascote guild"],
            },
            {
                "question": "A BraFurries é conhecida como o quê em relação ao bot?",
                "correct": "Comunidade mãe",
                "options": ["Comunidade mãe", "Servidor teste", "Canal parceiro", "Guilda temporária"],
            },
            {
                "question": "Qual evento costuma reunir furries presencialmente no Brasil?",
                "correct": "Convenções furmeet/furry",
                "options": ["Convenções furmeet/furry", "Campeonato de e-sports oficial", "Feira agropecuária", "Hackathon acadêmico"],
            },
            {
                "question": "Qual é o foco principal da comunidade BraFurries?",
                "correct": "Integração e convivência da comunidade furry",
                "options": [
                    "Integração e convivência da comunidade furry",
                    "Apenas venda de itens digitais",
                    "Somente competições ranqueadas",
                    "Conteúdo exclusivo de programação",
                ],
            },
        ]
        super().__init__()


    async def cog_load(self):
        await self._restore_expedition_tasks()

    async def cog_unload(self):
        for task in self.expedition_tasks.values():
            task.cancel()
        self.expedition_tasks.clear()
        for task in self.raid_tasks.values():
            task.cancel()
        self.raid_tasks.clear()
        for task in self.ritual_tasks.values():
            task.cancel()
        self.ritual_tasks.clear()

    def _schedule_expedition_finalize(self, guild_id: int, ends_at: int, message: discord.Message | None = None):
        existing = self.expedition_tasks.pop(guild_id, None)
        if existing and not existing.done():
            existing.cancel()

        async def _runner():
            delay = max(0, int(ends_at) - int(time.time()))
            if delay > 0:
                await asyncio.sleep(delay)
            await self._finalize_expedition(guild_id, message=message)

        self.expedition_tasks[guild_id] = asyncio.create_task(_runner())

    async def _restore_expedition_tasks(self):
        now_ts = int(time.time())
        for item in database.get_active_expeditions():
            guild_id = int(item.get("guild_id") or 0)
            ends_at = int(item.get("ends_at") or 0)
            if not guild_id:
                continue
            if ends_at <= now_ts:
                await self._finalize_expedition(guild_id, message=None)
                continue
            self._schedule_expedition_finalize(guild_id, ends_at, message=None)

    def _is_on_cooldown(self, guild_id: int, user_id: int, seconds: int = 20) -> bool:
        now = asyncio.get_running_loop().time()
        key = (guild_id, user_id)
        cooldown_until = self.minigame_cooldowns.get(key, 0)
        return now < cooldown_until

    def _activate_cooldown(self, guild_id: int, user_id: int, seconds: int = 20):
        now = asyncio.get_running_loop().time()
        self.minigame_cooldowns[(guild_id, user_id)] = now + seconds

    async def handle_boop(
        self,
        interaction: discord.Interaction,
        booper: discord.Member,
        target: discord.Member,
    ):
        async def send_ephemeral(message: str):
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

        if booper.bot or target.bot:
            return await send_ephemeral("Bots não participam de boops.")

        if booper.id == target.id:
            return await send_ephemeral("Você não pode dar boop em si mesmo.")

        if not interaction.guild:
            return await send_ephemeral("Este comando só pode ser usado em servidores.")

        success = database.increment_boop_counts(interaction.guild.id, booper, target)
        if not success:
            return await send_ephemeral(
                "Não foi possível registrar o boop. Tente novamente mais tarde."
            )

        content = f"{booper.mention} deu um *boop* em {target.mention}!"
        view = BoopView(self, booper, target)

        if interaction.response.is_done():
            await interaction.followup.send(content=content, view=view)
        else:
            await interaction.response.send_message(content=content, view=view)

    play = app_commands.Group(name="play", description="Comandos de duelo e diversão")
    coop = app_commands.Group(name="coop", description="Comandos cooperativos da comunidade.", parent=play)

    @play.command(name="boop", description="Envie um boop para outro membro.")
    @app_commands.describe(membro="Membro que receberá o boop")
    async def boop(self, ctx: discord.Interaction, membro: discord.Member):
        await self.handle_boop(ctx, ctx.user, membro)

    def _build_boss_embed(self, guild_id: int, last_attack: str | None = None, damage_flash: str | None = None):
        state = database.get_boss_event_state(guild_id)
        if not state or not state.get("active"):
            return discord.Embed(title="🐉 Boss cooperativo", description="Não há nenhum boss ativo no momento.")
        name = state.get("boss_name") or "Boss"
        hp = int(state.get("boss_hp") or 0)
        max_hp = int(state.get("boss_max_hp") or 0)
        desc = f"**{name}**\n❤️ HP: **{hp}/{max_hp}**"
        if last_attack:
            desc += f"\n🗡️ Último ataque: {last_attack}"
        if damage_flash:
            desc += f"\n💥 {damage_flash}"
        return discord.Embed(title="🐉 Boss cooperativo", description=desc)

    async def _handle_boss_attack(self, interaction: discord.Interaction, view: BossBattleView):
        if interaction.guild is None:
            await interaction.response.send_message("Use este comando em servidor.", ephemeral=True)
            return

        user_db_id = await database.async_getUserId(interaction.user.id)
        if user_db_id is None:
            await interaction.response.send_message("Você precisa estar registrado para participar.", ephemeral=True)
            return

        state = database.get_boss_event_state(interaction.guild.id, interaction.user.id)
        if not state or not state.get("active"):
            await interaction.response.send_message("Não há boss ativo agora.", ephemeral=True)
            return

        now_ts = int(time.time())
        cooldown_seconds = int(state.get("attack_cooldown_seconds") or 30)
        cooldown_until = int(state.get("user_cooldown_until") or 0)
        if cooldown_until > now_ts:
            await interaction.response.send_message(f"⏳ Aguarde {cooldown_until - now_ts}s para atacar novamente.", ephemeral=True)
            return

        damage = random.randint(15, 60)
        loot = random.randint(5, 20) if random.random() < 0.35 else 0

        result = database.record_boss_attack(
            guild_id=interaction.guild.id,
            user_id=user_db_id,
            damage=damage,
            now_ts=now_ts,
        )

        result_status = result.get("status")
        if result_status == "inactive":
            await interaction.response.send_message("Não há boss ativo agora.", ephemeral=True)
            return
        if result_status == "cooldown":
            retry_after = max(1, int(result.get("cooldown_until") or now_ts) - now_ts)
            await interaction.response.send_message(f"⏳ Aguarde {retry_after}s para atacar novamente.", ephemeral=True)
            return

        if loot > 0:
            database.adjust_user_economy_balance(interaction.guild.id, interaction.user, loot)

        boss_hp = int(result.get("boss_hp") or 0)
        boss_max_hp = int(result.get("boss_max_hp") or 0)
        defeated = bool(result.get("defeated"))

        if not defeated:
            embed = self._build_boss_embed(interaction.guild.id, last_attack=f"{interaction.user.display_name} - {damage}", damage_flash=f"-{damage} HP")
            await interaction.response.edit_message(embed=embed, view=view)
            await asyncio.sleep(4)
            clean_embed = self._build_boss_embed(interaction.guild.id, last_attack=f"{interaction.user.display_name} - {damage}")
            await interaction.message.edit(embed=clean_embed, view=view)
            return

        contributors = database.get_boss_event_top_contributors(interaction.guild.id, limit=10)
        total_damage = sum(int(item.get("total_damage") or 0) for item in contributors) or 1
        lines = []
        for rank, item in enumerate(contributors, start=1):
            member = interaction.guild.get_member(int(item["discord_user_id"]))
            name = member.mention if member else f"<@{item['discord_user_id']}>"
            contribution = int(item.get("total_damage") or 0)
            reward = max(1, int(200 * (contribution / total_damage)))
            if member:
                database.adjust_user_economy_balance(interaction.guild.id, member, reward)
            lines.append(f"{rank}. {name} — dano: **{contribution}**, recompensa: **+{reward}**")

        database.finish_boss_event(interaction.guild.id)
        embed = discord.Embed(title="🏆 Boss derrotado!", description="\n".join(lines) if lines else "Sem contribuidores.")
        await interaction.response.edit_message(embed=embed, view=None)

    @coop.command(name="boss", description="Mostra o boss ativo e permite atacar.")
    async def boss(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Use este comando em servidor.", ephemeral=True)
            return
        perms = interaction.user.guild_permissions if isinstance(interaction.user, discord.Member) else None
        can_summon = bool(perms and (perms.administrator or perms.manage_guild))
        embed = self._build_boss_embed(interaction.guild.id)
        has_active_boss = "Não há nenhum boss ativo" not in (embed.description or "")
        view = BossBattleView(self, interaction.guild.id, has_active_boss=has_active_boss, can_summon=can_summon and not has_active_boss)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=not has_active_boss)
        view.message = await interaction.original_response()

    def _build_ritual_embed(self, guild_id: int) -> discord.Embed:
        state = self.ritual_states.get(guild_id)
        if not state or not state.get("active"):
            return discord.Embed(title="🕯️ Ritual cooperativo", description="Nenhum ritual ativo no momento.")
        now = int(time.time())
        rem = max(0, int(state["ends_at"]) - now)
        energy = int(state["energy"])
        instability = int(state["instability"])
        slots = 10
        filled = min(slots, math.floor((energy / 100) * slots))
        bar = "█" * filled + "░" * (slots - filled)
        rec = sorted(state["cooldowns"].items(), key=lambda x: x[1])[:5]
        rec_txt = "\n".join([f"<@{uid}>: {max(0, int(until-now))}s" for uid, until in rec]) or "Ninguém em cooldown"
        now_ts = int(time.time())
        state["effects"] = {k:v for k,v in state.get("effects", {}).items() if v > now_ts}
        effects = ", ".join(state.get("effects", {}).keys()) or "Nenhum"
        desc = (
            f"**Energia:** {energy}/100\n"
            f"`{bar}`\n"
            f"**Instabilidade:** {instability}%\n"
            f"**Tempo restante:** {rem}s\n"
            f"**Participantes:** {len(state['participants'])}\n"
            f"**Efeitos ativos:** {effects}\n\n"
            f"**Recargas (top 5)**\n{rec_txt}"
        )
        return discord.Embed(title="🕯️ Ritual cooperativo", description=desc)

    async def _ritual_action(self, interaction: discord.Interaction, action: str):
        if interaction.guild is None:
            return await interaction.response.send_message("Use este comando em servidor.", ephemeral=True)
        state = self.ritual_states.get(interaction.guild.id)
        if not state or not state.get("active"):
            return await interaction.response.send_message("Não há ritual ativo.", ephemeral=True)
        now = int(time.time())
        if now >= int(state.get("ends_at", 0)):
            return await interaction.response.send_message("⏳ Este ritual já expirou.", ephemeral=True)
        key = interaction.user.id
        until = int(state["cooldowns"].get(key, 0))
        cd = 35 if action == "channel" else 45
        if action == "channel" and "Fluxo Sincronizado" in state["effects"]:
            cd = 25
        if until > now:
            return await interaction.response.send_message(f"⏳ Aguarde {until-now}s para usar novamente.", ephemeral=True)
        state["participants"].add(key)
        state["cooldowns"][key] = now + cd
        if action == "channel":
            gain = random.randint(3, 5)
            if "Ressonância Harmônica" in state["effects"]:
                gain += 2
            if "Ruído Arcano" in state["effects"]:
                gain = max(1, gain - 1)
            state["energy"] += gain
            state["instability"] = min(100, state["instability"] + 3)
            txt = f"✅ Canalização concluída (+{gain})."
        else:
            stab = 14 if "Âncora de Luz" in state["effects"] else 10
            state["instability"] = max(0, state["instability"] - stab)
            state["energy"] += 1
            txt = f"🛡️ Estabilização aplicada (-{stab}% instabilidade, +1 energia)."
        await interaction.response.send_message(f"{txt} Próximo uso em {cd}s.", ephemeral=True)
        if state.get("message"):
            await self._safe_update_ritual_message(interaction.guild.id)


    async def _safe_update_ritual_message(self, guild_id: int) -> bool:
        state = self.ritual_states.get(guild_id)
        if not state or not state.get("message"):
            return False
        try:
            await state["message"].edit(embed=self._build_ritual_embed(guild_id), view=RitualView(self, guild_id))
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            state["active"] = False
            return False

    async def _run_ritual(self, guild_id: int):
        while True:
            await asyncio.sleep(20)
            state = self.ritual_states.get(guild_id)
            if not state or not state.get("active"):
                return
            now = int(time.time())
            if now >= state["ends_at"] or state["energy"] >= 100:
                break
            state["instability"] = min(100, state["instability"] + 4)
            now = int(time.time())
            state["effects"] = {k:v for k,v in state.get("effects", {}).items() if v > now}
            neg_chance = max(5, min(45, int(state["instability"] * 0.35)))
            roll = random.randint(1, 100)
            if roll <= neg_chance:
                event = random.choice(["Interferência Sombria", "Ruído Arcano", "Sobrecarga"])
                state["effects"][event] = now + (20 if event != "Ruído Arcano" else 15)
                if event == "Interferência Sombria":
                    state["energy"] = max(0, state["energy"] - 6)
                elif event == "Sobrecarga":
                    for uid in list(state["cooldowns"].keys()):
                        state["cooldowns"][uid] += 10
                state["instability"] = min(100, state["instability"] + 6)
            elif random.randint(1, 100) <= 25:
                event = random.choice(["Ressonância Harmônica", "Âncora de Luz", "Fluxo Sincronizado"])
                state["effects"][event] = now + (20 if event != "Ruído Arcano" else 15)
            if state.get("message"):
                updated = await self._safe_update_ritual_message(guild_id)
                if not updated:
                    return

        state = self.ritual_states.get(guild_id)
        if not state:
            return
        state["active"] = False
        success = state["energy"] >= 100
        msg = state.get("message")
        if success:
            active_boss = database.get_boss_event_state(guild_id)
            if active_boss and int(active_boss.get("active") or 0) == 1:
                result = "✅ Ritual concluído! Já existe um boss ativo, então o Avatar do Ritual não foi invocado agora."
            else:
                database.create_or_reset_boss_event(guild_id, 12000, boss_name="Avatar do Ritual")
                result = "✅ Ritual concluído! Boss especial invocado: **Avatar do Ritual**."
        else:
            result = "❌ O ritual falhou antes de completar a energia."
        if msg:
            await msg.edit(content=result, embed=self._build_ritual_embed(guild_id), view=None)

    async def _start_ritual_for_guild(self, interaction: discord.Interaction) -> tuple[bool, str | None]:
        state = self.ritual_states.get(interaction.guild.id)
        if state and state.get("active"):
            jump = state.get("message")
            if jump:
                return False, f"Já existe um ritual ativo: {jump.jump_url}"
            return False, "Já existe um ritual ativo neste servidor."
        self.ritual_states[interaction.guild.id] = {
            "active": True,
            "energy": 0,
            "instability": 20,
            "participants": set(),
            "cooldowns": {},
            "effects": {},
            "ends_at": int(time.time()) + 600,
            "message": None,
        }
        view = RitualView(self, interaction.guild.id)
        if interaction.response.is_done():
            await interaction.followup.send(embed=self._build_ritual_embed(interaction.guild.id), view=view)
        else:
            await interaction.response.send_message(embed=self._build_ritual_embed(interaction.guild.id), view=view)
        msg = await interaction.original_response()
        self.ritual_states[interaction.guild.id]["message"] = msg
        task = self.ritual_tasks.get(interaction.guild.id)
        if task and not task.done():
            task.cancel()
        self.ritual_tasks[interaction.guild.id] = asyncio.create_task(self._run_ritual(interaction.guild.id))
        return True, None

    @coop.command(name="ritual", description="Inicia um ritual cooperativo com painel interativo.")
    async def ritual(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("Use este comando em servidor.", ephemeral=True)
        active_boss = database.get_boss_event_state(interaction.guild.id)
        if active_boss and int(active_boss.get("active") or 0) == 1:
            view = RitualBossActiveConfirmView(self, interaction.guild.id, interaction.user.id)
            return await interaction.response.send_message(
                "⚠️ Já existe um boss ativo. Se o ritual for concluído com sucesso, **um novo boss não será invocado** para não sobrescrever a luta atual. "
                "Os ganhos de XP do ritual ainda serão aplicados. Deseja iniciar o ritual assim mesmo?",
                view=view,
                ephemeral=True,
            )
        started, message = await self._start_ritual_for_guild(interaction)
        if not started and message:
            return await interaction.response.send_message(message, ephemeral=True)


    def _build_raid_embed(self, guild_id: int, log_lines: list[str] | None = None) -> discord.Embed:
        state = self.raid_states.get(guild_id)
        if not state or not state.get("active"):
            return discord.Embed(title="⚔️ Raid cooperativa", description="Nenhuma raid ativa no momento.")
        phase = int(state.get("phase", 1))
        hp = int(state.get("boss_hp", 0))
        max_hp = int(state.get("boss_max_hp", 1))
        energy = int(state.get("group_energy", 0))
        players = len(state.get("participants", set()))
        status = state.get("status", "battle")
        status_text = "Lobby (aguardando membros)" if status == "lobby" else "Intervalo de cura" if status == "intermission" else "Em combate"
        wait_line = ""
        if status == "lobby" and state.get("lobby_ends_at"):
            rem = max(0, math.ceil(state["lobby_ends_at"] - time.monotonic()))
            wait_line = f"\n**Início em:** {rem}s"
        elif status == "intermission" and state.get("intermission_ends_at"):
            rem = max(0, math.ceil(state["intermission_ends_at"] - time.monotonic()))
            wait_line = f"\n**Novo boss em:** {rem}s"
        desc = (
            f"**Status:** {status_text}\n"
            f"**Fase:** {phase}/3\n"
            f"**HP do Boss:** {hp}/{max_hp}\n"
            f"**Energia do grupo:** {energy}\n"
            f"**Participantes ativos:** {players}"
            f"{wait_line}"
        )
        if log_lines:
            desc += "\n\n**Última rodada**\n" + "\n".join(log_lines)
        return discord.Embed(title="⚔️ Raid cooperativa", description=desc)

    def _register_raid_action(self, guild_id: int, user_id: int, action: str) -> tuple[bool, str]:
        state = self.raid_states.get(guild_id)
        if not state or not state.get("active"):
            return False, "Não há raid ativa."
        status = state.get("status", "battle")
        if status == "lobby":
            return False, "Seu painel está pronto, mas a raid ainda está no lobby de entrada."
        if status == "intermission" and action in {"attack", "defend"}:
            return False, "Durante a preparação só é permitido curar."
        if status not in {"battle", "intermission"}:
            return False, "A raid não está aceitando ações agora."
        participants = state.get("participants", set())
        if user_id not in participants:
            return False, "Você não entrou na raid durante o lobby."
        state["actions"].append((user_id, action))
        return True, f"Ação registrada: **{action}**."

    async def _run_raid_rounds(self, guild_id: int, message: discord.Message, view: RaidBattleView):
        async def _edit_raid_message(embed: discord.Embed, target_view: discord.ui.View | None = None):
            try:
                await message.edit(embed=embed, view=target_view)
                return True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                state = self.raid_states.get(guild_id)
                if state:
                    state["active"] = False
                self.raid_states.pop(guild_id, None)
                task = self.raid_tasks.pop(guild_id, None)
                if task and task is not asyncio.current_task() and not task.done():
                    task.cancel()
                return False

        state = self.raid_states.get(guild_id)
        if not state:
            return
        state["last_logs"] = ["Aguardando primeiras ações..."]
        state["next_boss_attack_at"] = time.monotonic() + random.uniform(3, 7)
        state["pending_defense"] = 0

        while True:
            state = self.raid_states.get(guild_id)
            if not state or not state.get("active"):
                return
            await asyncio.sleep(1)
            lock = self.raid_locks.setdefault(guild_id, asyncio.Lock())
            async with lock:
                state = self.raid_states.get(guild_id)
                if not state or not state.get("active"):
                    return
                logs = []
                actions = list(state.get("actions", []))
                state["actions"].clear()
                attack_n = sum(1 for _, a in actions if a == "attack")
                defend_n = sum(1 for _, a in actions if a == "defend")
                heal_n = sum(1 for _, a in actions if a == "heal")

                damage = sum(random.randint(18, 40) for _ in range(attack_n))
                defense = defend_n * 12
                state["pending_defense"] = int(state.get("pending_defense", 0)) + defense
                heal = heal_n * 10
                if damage:
                    state["boss_hp"] = max(0, int(state["boss_hp"]) - damage)
                if heal:
                    state["group_energy"] = min(200, int(state["group_energy"]) + heal)

                now_mono = time.monotonic()
                boss_hit = 0
                if now_mono >= float(state.get("next_boss_attack_at", now_mono)):
                    defense_now = int(state.get("pending_defense", 0))
                    boss_hit = max(0, random.randint(28, 75) - defense_now)
                    state["group_energy"] = max(0, int(state["group_energy"]) - boss_hit)
                    state["pending_defense"] = 0
                    state["next_boss_attack_at"] = now_mono + random.uniform(2, 6)

                for uid, action in actions:
                    points = 0
                    if action == "attack":
                        points = random.randint(12, 24)
                    elif action == "defend":
                        points = 10
                    elif action == "heal":
                        points = 11
                    state["contribution"][uid] = int(state["contribution"].get(uid, 0)) + points

                if attack_n or defend_n or heal_n or boss_hit:
                    logs = [
                        f"🗡️ Ataques: {attack_n} (dano {damage})",
                        f"🛡️ Defesas: {defend_n} (+{defense} mitigação, total pendente {int(state.get('pending_defense', 0))})",
                        f"💚 Curas: {heal_n} (+{heal} energia)",
                        f"👹 Golpe do boss: -{boss_hit} energia" if boss_hit else "👹 Boss preparando próximo ataque...",
                    ]
                    state["last_logs"] = logs

                if state["group_energy"] <= 0:
                    state["active"] = False
                    for c in view.children:
                        c.disabled = True
                    await _edit_raid_message(
                        embed=discord.Embed(title="☠️ Raid falhou", description="A energia do grupo chegou a zero."),
                        target_view=None,
                    )
                    self.raid_states.pop(guild_id, None)
                    return

                if state["boss_hp"] <= 0:
                    if int(state["phase"]) >= 3:
                        await self._finalize_raid_success(guild_id, message, view)
                        return
                    state["status"] = "intermission"
                    state["phase"] += 1
                    state["boss_max_hp"] = int(state["boss_max_hp"] * 1.35)
                    state["boss_hp"] = state["boss_max_hp"]
                    intermission_logs = list(state.get("last_logs", []))
                    intermission_logs.append(f"✨ Fase avançada para {state['phase']}/3")
                    intermission_logs.append("🕒 Intervalo de 30s para cura. Ataque/defesa bloqueados.")
                    state["last_logs"] = intermission_logs
                    await _edit_raid_message(embed=self._build_raid_embed(guild_id, intermission_logs), target_view=None)
                    state["intermission_ends_at"] = time.monotonic() + 30
                    while True:
                        now_mono = time.monotonic()
                        rem = float(state.get("intermission_ends_at", now_mono)) - now_mono
                        if rem <= 0:
                            break
                        await asyncio.sleep(min(1, rem))
                        state_tick = self.raid_states.get(guild_id)
                        if not state_tick or not state_tick.get("active"):
                            return
                        heal_actions = [(uid, a) for uid, a in state_tick.get("actions", []) if a == "heal"]
                        if heal_actions:
                            healed = len(heal_actions) * 10
                            state_tick["group_energy"] = min(200, int(state_tick["group_energy"]) + healed)
                            state_tick["actions"] = [(uid, a) for uid, a in state_tick.get("actions", []) if a != "heal"]
                            for uid, _ in heal_actions:
                                state_tick["contribution"][uid] = int(state_tick["contribution"].get(uid, 0)) + 11
                            intermission_dynamic_logs = [line for line in list(state_tick.get("last_logs", [])) if not line.startswith("💚 Curas no intervalo:")]
                            intermission_dynamic_logs.append(f"💚 Curas no intervalo: {len(heal_actions)} (+{healed} energia)")
                            state_tick["last_logs"] = intermission_dynamic_logs
                        await _edit_raid_message(embed=self._build_raid_embed(guild_id, state_tick.get("last_logs")), target_view=None)
                    state2 = self.raid_states.get(guild_id)
                    if not state2 or not state2.get("active"):
                        return
                    state2.pop("intermission_ends_at", None)
                    state2["status"] = "battle"
                    state2["next_boss_attack_at"] = time.monotonic() + random.uniform(2, 5)
                    state2["pending_defense"] = 0
                    state2["last_logs"] = ["⚔️ Novo boss engajado!"]

                updated = await _edit_raid_message(
                    embed=self._build_raid_embed(guild_id, state.get("last_logs")),
                    target_view=None,
                )
                if not updated:
                    return

    async def _finalize_raid_success(self, guild_id: int, message: discord.Message, view: RaidBattleView):
        state = self.raid_states.get(guild_id)
        if not state:
            return
        contributions = sorted(state.get("contribution", {}).items(), key=lambda x: x[1], reverse=True)
        total = sum(v for _, v in contributions) or 1
        lines = []
        guild = self.bot.get_guild(guild_id)
        for idx, (uid, points) in enumerate(contributions, start=1):
            reward = max(1, int(250 * (points / total)))
            member = guild.get_member(uid) if guild else None
            if member:
                database.adjust_user_economy_balance(guild_id, member, reward)
            mention = member.mention if member else f"<@{uid}>"
            lines.append(f"{idx}. {mention} — contribuição: **{points}**, recompensa: **+{reward}**")

        for c in view.children:
            c.disabled = True
        await message.edit(embed=discord.Embed(title="🏆 Raid concluída (Fase 3)!", description="\n".join(lines) if lines else "Sem participantes."), view=view)
        self.raid_states.pop(guild_id, None)

    def _build_expedition_embed(self, guild_id: int) -> discord.Embed:
        state = database.get_expedition_state(guild_id)
        if not state or not state.get("active"):
            return discord.Embed(title="🧭 Expedição", description="Nenhuma expedição ativa no momento.")
        participants = state.get("participants", [])
        lines = [f"• <@{member_id}>" for member_id in participants] or ["Nenhum participante ainda."]
        desc = (
            f"Início: <t:{int(state['started_at'])}:f>\n"
            f"Fechamento: <t:{int(state['ends_at'])}:R>\n\n"
            f"**Participantes ({len(participants)}):**\n" + "\n".join(lines)
        )
        return discord.Embed(title="🧭 Expedição em andamento", description=desc)

    async def _refresh_expedition_lobby(self, guild_id: int, message: discord.Message | None):
        if message is None:
            return
        await message.edit(embed=self._build_expedition_embed(guild_id))

    async def _finalize_expedition(self, guild_id: int, message: discord.Message | None = None):
        lock = self.expedition_finalize_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            state = database.get_expedition_state(guild_id)
            if not state or not state.get("active"):
                return
            participants = [int(item) for item in state.get("participants", [])]
            participant_count = len(participants)
            base_chance = 35
            bonus_per_participant = 8
            success_chance = min(95, base_chance + (participant_count * bonus_per_participant))
            success = random.randint(1, 100) <= success_chance
            total_loot = 0
            rarity_count = {"comum": 0, "raro": 0, "épico": 0}
            contribution_lines: list[str] = []
            guild = self.bot.get_guild(guild_id)
            for member_id in participants:
                reward = random.randint(15, 45) if success else random.randint(1, 8)
                roll = random.random()
                rarity = "comum" if roll < 0.70 else "raro" if roll < 0.93 else "épico"
                rarity_count[rarity] += 1
                total_loot += reward
                if guild:
                    member = guild.get_member(member_id)
                    if member is not None:
                        database.adjust_user_economy_balance(guild_id, member, reward)
                contribution_lines.append(f"<@{member_id}>: **+{reward}** ({rarity})")

            database.finalize_expedition(guild_id, int(time.time()))
            result = "✅ Sucesso" if success else "❌ Fracasso parcial"
            summary = (
                f"{result}\n"
                f"Chance: **{success_chance}%** (base {base_chance}% + bônus {bonus_per_participant}% por participante)\n"
                f"Loot total: **{total_loot}**\n"
                f"Raridades: comum **{rarity_count['comum']}**, raro **{rarity_count['raro']}**, épico **{rarity_count['épico']}**\n\n"
                f"**Contribuição dos membros**\n" + ("\n".join(contribution_lines) if contribution_lines else "Sem participantes.")
            )
            if message is not None:
                await message.edit(embed=discord.Embed(title="🏕️ Expedição finalizada", description=summary), view=None)
            elif guild and guild.system_channel:
                await guild.system_channel.send(embed=discord.Embed(title="🏕️ Expedição finalizada", description=summary))


    @coop.command(name="raid", description="Inicia uma raid cooperativa por fases.")
    async def raid(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Use este comando em servidor.", ephemeral=True)
            return
        guild_id = interaction.guild.id
        if guild_id in self.raid_states and self.raid_states[guild_id].get("active"):
            await interaction.response.send_message("Já existe uma raid ativa neste servidor.", ephemeral=True)
            return

        base_hp = 500
        lobby_duration = 60
        self.raid_states[guild_id] = {
            "active": True,
            "phase": 1,
            "boss_hp": base_hp,
            "boss_max_hp": base_hp,
            "group_energy": 120,
            "status": "lobby",
            "cooldowns": {},
            "actions": [],
            "participants": set(),
            "contribution": {},
            "round_window": 20,
            "action_cooldown": 0,
            "lobby_ends_at": time.monotonic() + lobby_duration,
        }
        lobby_view = RaidLobbyView(self, guild_id)
        await interaction.response.send_message("Raid criada! Lobby aberto por 60s para juntar membros.", embed=self._build_raid_embed(guild_id), view=lobby_view)
        msg = await interaction.original_response()
        lobby_view.message = msg

        async def _start_after_lobby():
            while True:
                state = self.raid_states.get(guild_id)
                if not state or not state.get("active"):
                    return
                if state.get("status") != "lobby":
                    return
                now_mono = time.monotonic()
                rem = float(state.get("lobby_ends_at", now_mono)) - now_mono
                if rem <= 0:
                    break
                await asyncio.sleep(min(1, rem))
                try:
                    await msg.edit(embed=self._build_raid_embed(guild_id), view=lobby_view)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    state["active"] = False
                    self.raid_states.pop(guild_id, None)
                    return
            state = self.raid_states.get(guild_id)
            if not state or not state.get("active"):
                return
            state.pop("lobby_ends_at", None)
            state["status"] = "battle"
            battle_view = RaidBattleView(self, guild_id)
            battle_view.message = msg
            try:
                await msg.edit(embed=self._build_raid_embed(guild_id), view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                state["active"] = False
                self.raid_states.pop(guild_id, None)
                task = self.raid_tasks.pop(guild_id, None)
                if task and task is not asyncio.current_task() and not task.done():
                    task.cancel()
                return
            existing = self.raid_tasks.pop(guild_id, None)
            if existing and not existing.done():
                existing.cancel()
            self.raid_tasks[guild_id] = asyncio.create_task(self._run_raid_rounds(guild_id, msg, battle_view))

        existing = self.raid_tasks.pop(guild_id, None)
        if existing and not existing.done():
            existing.cancel()
        self.raid_tasks[guild_id] = asyncio.create_task(_start_after_lobby())

    @coop.command(name="expedition", description="Inicia uma expedição cooperativa com entrada temporária.")
    @app_commands.describe(duracao_segundos="Tempo de entrada na expedição (30-900 segundos)")
    async def expedition(self, interaction: discord.Interaction, duracao_segundos: app_commands.Range[int, 30, 900] = 120):
        if interaction.guild is None:
            await interaction.response.send_message("Use este comando em servidor.", ephemeral=True)
            return
        state = database.get_expedition_state(interaction.guild.id)
        if state and state.get("active"):
            await interaction.response.send_message("Já existe uma expedição ativa neste servidor.", ephemeral=True)
            return
        started_at = int(time.time())
        ends_at = started_at + int(duracao_segundos)
        database.create_expedition(interaction.guild.id, started_at, ends_at)
        self._schedule_expedition_finalize(interaction.guild.id, ends_at)
        database.update_expedition_participants(interaction.guild.id, [interaction.user.id])
        view = ExpeditionLobbyView(self, interaction.guild.id, interaction.user.id, ends_at)
        embed = self._build_expedition_embed(interaction.guild.id)
        await interaction.response.send_message("Expedição iniciada! Entre ou saia até o fechamento.", embed=embed, view=view)
        view.message = await interaction.original_response()

    async def _guard_minigame(self, interaction: discord.Interaction) -> tuple[bool, str | None]:
        if not interaction.guild:
            return False, "Este comando só pode ser usado em servidores."
        key = (interaction.guild.id, interaction.user.id)
        if key in self.active_minigames:
            return False, "Você já possui uma partida em andamento."
        if self._is_on_cooldown(interaction.guild.id, interaction.user.id):
            return False, "Aguarde alguns segundos antes de iniciar outro minigame."
        self.active_minigames.add(key)
        return True, None

    def _finish_minigame(self, guild_id: int, user_id: int):
        key = (guild_id, user_id)
        self.active_minigames.discard(key)
        self._activate_cooldown(guild_id, user_id)

    async def _run_forca_minigame(self, interaction: discord.Interaction):
        ok, reason = await self._guard_minigame(interaction)
        if not ok:
            return await interaction.response.send_message(reason, ephemeral=True)
        try:
            words = [
                "fursona",
                "fursuit",
                "anthro",
                "canino",
                "felino",
                "raposa",
                "lobo",
                "draconico",
                "escamas",
                "pelagem",
                "comissao",
                "artistico",
                "roleplay",
                "convencao",
                "furmeet",
                "adocao",
                "digital",
                "criatividade",
                "comunidade",
                "amizade",
                "respeito",
                "musica",
                "cinema",
                "tecnologia",
                "aventura",
            ]
            target = random.choice(words)
            visible = ["_" for _ in target]
            wrong_letters: set[str] = set()
            max_errors = 6

            def build_gallows(errors: int) -> str:
                stages = [
                    "```\n +---+\n |   |\n     |\n     |\n     |\n     |\n=========\n```",
                    "```\n +---+\n |   |\n O   |\n     |\n     |\n     |\n=========\n```",
                    "```\n +---+\n |   |\n O   |\n |   |\n     |\n     |\n=========\n```",
                    "```\n +---+\n |   |\n O   |\n/|   |\n     |\n     |\n=========\n```",
                    "```\n +---+\n |   |\n O   |\n/|\\  |\n     |\n     |\n=========\n```",
                    "```\n +---+\n |   |\n O   |\n/|\\  |\n/    |\n     |\n=========\n```",
                    "```\n +---+\n |   |\n O   |\n/|\\  |\n/ \\  |\n     |\n=========\n```",
                ]
                return stages[max(0, min(errors, max_errors))]

            class ForcaGuessModal(discord.ui.Modal, title="Forca"):
                guess = discord.ui.TextInput(
                    label="Digite uma letra",
                    placeholder="Ex.: a",
                    min_length=1,
                    max_length=1,
                )

                def __init__(self):
                    super().__init__(timeout=30)
                    self.answer: str | None = None

                async def on_submit(self, i: discord.Interaction):
                    self.answer = str(self.guess.value).strip().lower()
                    await i.response.send_message("Palpite recebido!", ephemeral=True)

            class ForcaGuessView(discord.ui.View):
                def __init__(self, player_id: int):
                    super().__init__(timeout=30)
                    self.player_id = player_id
                    self.guess: str | None = None

                async def interaction_check(self, i: discord.Interaction) -> bool:
                    if i.user.id != self.player_id:
                        await i.response.send_message("Somente quem iniciou pode jogar.", ephemeral=True)
                        return False
                    return True

                @discord.ui.button(label="Enviar palpite", style=discord.ButtonStyle.primary)
                async def submit_guess(self, i: discord.Interaction, _: discord.ui.Button):
                    modal = ForcaGuessModal()
                    await i.response.send_modal(modal)
                    await modal.wait()
                    self.guess = modal.answer
                    self.stop()

            errors = 0
            message = None
            won = False
            while errors < max_errors:
                remaining = " ".join(visible)
                attempts_left = max_errors - errors
                used_letters = ", ".join(sorted(wrong_letters)) if wrong_letters else "nenhuma"
                content = (
                    f"**Jogo da Forca**\n{build_gallows(errors)}\n"
                    f"Palavra: `{remaining}`\n"
                    f"Erros: **{errors}/{max_errors}** (chances restantes: **{attempts_left}**)\n"
                    f"Letras erradas: **{used_letters}**\n"
                    "Clique no botão para enviar **uma letra**."
                )
                view = ForcaGuessView(interaction.user.id)
                if message is None:
                    await interaction.response.send_message(content, view=view, ephemeral=True)
                    message = await interaction.original_response()
                else:
                    await message.edit(content=content, view=view)

                await view.wait()
                guess = view.guess
                if not guess:
                    break

                if guess in visible or guess in wrong_letters:
                    continue

                if guess in target:
                    for idx, letter in enumerate(target):
                        if letter == guess:
                            visible[idx] = guess
                    if "_" not in visible:
                        won = True
                        break
                else:
                    wrong_letters.add(guess)
                    errors += 1

            final_word = "".join(visible)
            final_text = (
                f"✅ Você venceu! A palavra era **{target}**."
                if won
                else f"❌ Você perdeu! A palavra era **{target}**."
            )
            if message:
                await message.edit(
                    content=(
                        f"**Jogo da Forca**\n{build_gallows(errors)}\n"
                        f"Palavra final: `{ ' '.join(final_word) }`\n{final_text}"
                    ),
                    view=None,
                )

            points = len(target) * 10 if won else 0
            database.record_minigame_match(
                interaction.guild.id, interaction.user, "forca", points, won
            )
        finally:
            self._finish_minigame(interaction.guild.id, interaction.user.id)

    async def _run_trivia_minigame(self, interaction: discord.Interaction):
        ok, reason = await self._guard_minigame(interaction)
        if not ok:
            return await interaction.response.send_message(reason, ephemeral=True)
        try:
            selected = random.choice(self.trivia_questions)
            question = str(selected["question"])
            correct = str(selected["correct"])
            options = [str(option) for option in selected["options"]]
            random.shuffle(options)

            class TriviaOptionButton(discord.ui.Button):
                def __init__(self, label: str):
                    super().__init__(label=label, style=discord.ButtonStyle.secondary)
                    self.answer_value = label

                async def callback(self, i: discord.Interaction):
                    view: TriviaView = self.view
                    view.correct = self.answer_value == view.correct_answer
                    view.stop()
                    message = "✅ Resposta correta!" if view.correct else f"❌ Resposta incorreta. Correta: **{view.correct_answer}**"
                    await i.response.edit_message(content=message, view=None)

            class TriviaView(discord.ui.View):
                def __init__(self, player_id: int, answer: str, answer_options: list[str]):
                    super().__init__(timeout=20)
                    self.player_id = player_id
                    self.correct_answer = answer
                    self.correct = False
                    for option in answer_options:
                        self.add_item(TriviaOptionButton(option))

                async def interaction_check(self, i: discord.Interaction) -> bool:
                    if i.user.id != self.player_id:
                        await i.response.send_message("Somente quem iniciou pode responder.", ephemeral=True)
                        return False
                    return True

            view = TriviaView(interaction.user.id, correct, options)
            await interaction.response.send_message(f"**Trivia**\n{question}", view=view, ephemeral=True)
            await view.wait()
            points = 20 if view.correct else 0
            database.record_minigame_match(
                interaction.guild.id, interaction.user, "trivia", points, view.correct
            )
        finally:
            self._finish_minigame(interaction.guild.id, interaction.user.id)

    async def _run_memoria_minigame(self, interaction: discord.Interaction):
        ok, reason = await self._guard_minigame(interaction)
        if not ok:
            return await interaction.response.send_message(reason, ephemeral=True)
        try:
            target = random.choice(["🦊", "🐺", "🐱", "🐶", "🐯", "🐼", "🦁", "🐮", "🐸", "🐨"])
            options = ["🦊", "🐺", "🐱", "🐶", "🐯", "🐼", "🦁", "🐮", "🐸", "🐨"]
            random.shuffle(options)
            class MemoryView(discord.ui.View):
                def __init__(self, player_id: int, target_emoji: str, buttons: list[str]):
                    super().__init__(timeout=20)
                    self.player_id = player_id
                    self.target_emoji = target_emoji
                    self.won = False
                    for emoji in buttons:
                        self.add_item(MemoryButton(emoji))
                async def interaction_check(self, i: discord.Interaction) -> bool:
                    if i.user.id != self.player_id:
                        await i.response.send_message("Você não participa desta partida.", ephemeral=True)
                        return False
                    return True
            class MemoryButton(discord.ui.Button):
                def __init__(self, emoji: str):
                    super().__init__(label=emoji, style=discord.ButtonStyle.primary)
                    self.emoji_value = emoji
                async def callback(self, i: discord.Interaction):
                    view: MemoryView = self.view
                    view.won = self.emoji_value == view.target_emoji
                    view.stop()
                    msg = "✅ Acertou!" if view.won else f"❌ Errou! O correto era {view.target_emoji}"
                    await i.response.edit_message(content=msg, view=None)
            view = MemoryView(interaction.user.id, target, options)
            countdown_seconds = 5
            await interaction.response.send_message(
                f"Memorize: **{target}**\nEscolha liberada em **{countdown_seconds}**...",
                ephemeral=True,
            )
            message = await interaction.original_response()
            for second in range(countdown_seconds - 1, 0, -1):
                await asyncio.sleep(1)
                await message.edit(
                    content=f"Memorize: **{target}**\nEscolha liberada em **{second}**..."
                )
            await asyncio.sleep(1)
            await message.edit(
                content="Agora escolha o emoji correto:",
                view=view,
            )
            await view.wait()
            points = 15 if view.won else 0
            database.record_minigame_match(
                interaction.guild.id, interaction.user, "memoria", points, view.won
            )
        finally:
            self._finish_minigame(interaction.guild.id, interaction.user.id)

    async def _show_minigame_ranking(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use em servidor.", ephemeral=True)
        ranking = database.get_weekly_minigame_ranking(interaction.guild.id, limit=10)
        if not ranking:
            return await interaction.response.send_message("Sem partidas registradas nesta semana.")
        lines = []
        for idx, item in enumerate(ranking, start=1):
            mention = f"<@{item['discord_user_id']}>"
            lines.append(f"**{idx}.** {mention} — {item['points']} pts ({item['wins']} vitórias)")
        embed = discord.Embed(title="🏆 Ranking semanal de minigames", description="\n".join(lines), color=discord.Color.gold())
        await interaction.response.send_message(embed=embed)

    @app_commands.choices(
        jogo=[
            app_commands.Choice(name="Forca", value="forca"),
            app_commands.Choice(name="Trivia", value="trivia"),
            app_commands.Choice(name="Memória", value="memoria"),
            app_commands.Choice(name="Ranking semanal", value="ranking"),
        ]
    )
    @play.command(name="minigames", description="Jogue minigames rápidos ou veja o ranking.")
    async def minigames(self, interaction: discord.Interaction, jogo: str):
        if jogo == "forca":
            return await self._run_forca_minigame(interaction)
        if jogo == "trivia":
            return await self._run_trivia_minigame(interaction)
        if jogo == "memoria":
            return await self._run_memoria_minigame(interaction)
        return await self._show_minigame_ranking(interaction)

    @app_commands.choices(
        jogo=[
            app_commands.Choice(name="Cliques", value="cliques"),
            app_commands.Choice(name="Pedra, Papel e Tesoura", value="pedra_papel_tesoura"),
            app_commands.Choice(name="Saque Rápido", value="saque_rapido"),
        ]
    )
    @play.command(name="duelo", description="Convide um membro para um duelo de cliques ou PPT.")
    async def duel(
        self,
        ctx: discord.Interaction,
        membro: discord.Member | None = None,
        jogo: str = "cliques",
    ):
        if jogo == "saque_rapido" and membro is None:
            rules_text = (
                "- Espere pelo aviso: 🤠 Sinal a qualquer momento…\n"
                "- Após o 🔥 SAQUE! você tem 1.5s para clicar.\n"
                "- Clique cedo: -30 pontos e bloqueio de 300ms.\n"
                "Score final = tempo médio + penalidade por falsos starts."
            )

            embed = discord.Embed(
                title="Saque Rápido (Solo)",
                description=(
                    "Teste seus reflexos!\n"
                    "Prepare-se para sacar..."
                ),
                color=discord.Color.orange(),
            )
            embed.add_field(name="Regras", value=rules_text, inline=False)

            await ctx.response.send_message(embed=embed)
            duel_message = await ctx.original_response()
            for counter in range(10, 0, -1):
                embed.description = (
                    "Teste seus reflexos!\n"
                    "Prepare-se para sacar...\n\n"
                    f"Começando em {counter}s"
                )
                await duel_message.edit(embed=embed)
                await asyncio.sleep(1)

            embed.description = "🤠 Sinal a qualquer momento… não saque cedo!"
            await duel_message.edit(embed=embed)

            solo_view = QuickDrawSoloView(ctx.user)
            solo_message = await ctx.followup.send(
                content=solo_view._status_text(signal_active=False),
                view=solo_view,
                ephemeral=True,
            )
            solo_view.set_message(solo_message)

            await asyncio.sleep(random.uniform(2, 5))
            solo_view.grant_signal()
            embed.description = "🔥 SAQUE!"
            await duel_message.edit(embed=embed)
            await solo_view._refresh_message()

            try:
                await asyncio.wait_for(solo_view.done.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                solo_view.mark_timeout()

            await solo_view.finalize()
            result = solo_view.result or calculate_quickdraw_result(None, 0)
            reaction_display = (
                f"{result['reaction_ms']:.0f} ms" if result.get("reaction_ms") else "Nenhum"
            )
            final_embed = discord.Embed(
                title="Resultado - Saque Rápido (Solo)",
                description=(
                    f"⏱️ Tempo de reação: **{reaction_display}**\n"
                    f"🚫 Falsos starts: **{result['false_starts']}** (-{result['penalty_points']} pts)\n"
                    f"Pontuação base: **{result['base_points']}**\n"
                    f"Pontuação final: **{result['final_points']}**\n"
                    f"Score (ms + penalidade): **{result['metric_ms']:.0f} ms**\n"
                    f"Medalha: **{result['medal']}**"
                ),
                color=discord.Color.green(),
            )
            return await duel_message.edit(embed=final_embed, view=None)

        if membro:
            if membro.bot:
                return await ctx.response.send_message(
                    "Você não pode duelar com bots.", ephemeral=True
                )
            if membro.id == ctx.user.id:
                return await ctx.response.send_message(
                    "Você não pode duelar consigo mesmo.", ephemeral=True
                )

        duel_description = (
            f"{ctx.user.mention} desafiou {membro.mention} para um duelo!"
            if membro
            else f"{ctx.user.mention} desafiou qualquer um para um duelo! Clique em Aceitar para participar."
        )
        invite_embed = discord.Embed(
            title="Convite para duelo",
            description=duel_description,
            color=discord.Color.blurple(),
        )
        invite_embed.set_footer(text="O convite expira em 30 segundos.")
        view = DuelInviteView(ctx.user, membro)
        message_content = membro.mention if membro else None
        await ctx.response.send_message(
            content=message_content,
            embed=invite_embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        invite_message = await ctx.original_response()

        await view.wait()
        opponent = view.accepted_user or membro

        if not view.accepted or opponent is None:
            not_accepted = discord.Embed(
                title="Convite para duelo",
                description="O convite não foi aceito a tempo ou foi recusado.",
                color=discord.Color.red(),
            )
            return await invite_message.edit(embed=not_accepted, view=None)

        opponent_followup = view.accept_interaction.followup

        await invite_message.delete()

        duel_embed = discord.Embed(
            title="Duelo",
            description="Preparem-se!",
            color=discord.Color.orange(),
        )
        duel_embed.add_field(name="Desafiante", value=ctx.user.mention, inline=True)
        duel_embed.add_field(name="Desafiado", value=opponent.mention, inline=True)
        duel_message = await ctx.followup.send(embed=duel_embed)

        quickdraw_embed_builder = None
        if jogo == "saque_rapido":

            def build_quickdraw_embed(
                status: str, color: discord.Color
            ) -> discord.Embed:
                embed = discord.Embed(
                    title="Duelo - Saque Rápido",
                    description=status,
                    color=color,
                )
                embed.add_field(name="Desafiante", value=ctx.user.mention, inline=True)
                embed.add_field(name="Desafiado", value=opponent.mention, inline=True)
                embed.add_field(
                    name="Regras",
                    value=(
                        "Aguarde o sinal de fogo.\n"
                        "Falsos starts: -30 pontos.\n"
                        "Janela de 1.5s após o 🔥 SAQUE!."
                    ),
                    inline=False,
                )
                return embed

            quickdraw_embed_builder = build_quickdraw_embed

        for counter in [5, 4, 3, 2, 1]:
            if jogo == "saque_rapido" and quickdraw_embed_builder:
                description = (
                    f"O duelo contra **{opponent.display_name}** começa em **{counter}...**\n\n"
                    "⚡ Reaja assim que o 🔥 aparecer!"
                )
                countdown_embed = quickdraw_embed_builder(
                    description, discord.Color.orange()
                )
            else:
                countdown_embed = discord.Embed(
                    title="Duelo",
                    description=(
                        f"O duelo contra **{opponent.display_name}** começa em **{counter}...**"
                    ),
                    color=discord.Color.orange(),
                )
                countdown_embed.add_field(
                    name="Desafiante", value=ctx.user.mention, inline=True
                )
                countdown_embed.add_field(
                    name="Desafiado", value=opponent.mention, inline=True
                )
            await duel_message.edit(embed=countdown_embed)
            await asyncio.sleep(1)

        if jogo == "saque_rapido":
            quickdraw_state = QuickDrawDuelState(ctx.user, opponent)
            build_quickdraw_embed = quickdraw_embed_builder or (
                lambda status, color: discord.Embed(
                    title="Duelo - Saque Rápido",
                    description=status,
                    color=color,
                )
            )

            await duel_message.edit(
                embed=build_quickdraw_embed(
                    "Prepare-se para o saque rápido!", discord.Color.orange()
                ),
                view=None,
            )

            quickdraw_views = []
            for participant in (ctx.user, opponent):
                view_instance = QuickDrawDuelView(quickdraw_state, participant)
                followup = (
                    ctx.followup if participant.id == ctx.user.id else opponent_followup
                )
                message = await followup.send(
                    content=view_instance._status_text(),
                    view=view_instance,
                    ephemeral=True,
                )
                view_instance.set_message(message)
                quickdraw_views.append(view_instance)

            embed_description = "🤠 Sinal a qualquer momento… não saque cedo!"
            await duel_message.edit(
                embed=build_quickdraw_embed(embed_description, discord.Color.orange())
            )

            await asyncio.sleep(random.uniform(2, 5))
            quickdraw_state.grant_signal()
            await duel_message.edit(
                embed=build_quickdraw_embed("🔥 SAQUE!", discord.Color.red())
            )
            for view in quickdraw_views:
                await view._refresh_message()

            await asyncio.sleep(1.5)
            quickdraw_state.finalize_pending()
            for view in quickdraw_views:
                await view.finalize()

            challenger_result = quickdraw_state.get_result(ctx.user)
            opponent_result = quickdraw_state.get_result(opponent)

            def format_result(result: dict[str, float | int | str | None]):
                reaction_display = (
                    f"{result['reaction_ms']:.0f} ms" if result.get("reaction_ms") else "Nenhum"
                )
                return (
                    f"⏱️ Tempo: **{reaction_display}**\n"
                    f"🚫 Falsos starts: **{result['false_starts']}** (-{result['penalty_points']} pts)\n"
                    f"Pontuação final: **{result['final_points']}**\n"
                    f"Score (ms + penalidade): **{result['metric_ms']:.0f} ms**\n"
                    f"Medalha: **{result['medal']}**"
                )

            challenger_points = challenger_result.get("final_points", 0)
            opponent_points = opponent_result.get("final_points", 0)

            if challenger_points > opponent_points:
                winner_text = f"{ctx.user.mention} venceu o duelo de saque rápido!"
            elif opponent_points > challenger_points:
                winner_text = f"{opponent.mention} venceu o duelo de saque rápido!"
            else:
                winner_text = "Empate no saque rápido!"

            final_embed = build_quickdraw_embed(winner_text, discord.Color.green())
            final_embed.clear_fields()
            final_embed.add_field(
                name=f"{ctx.user.display_name}",
                value=format_result(challenger_result),
                inline=False,
            )
            final_embed.add_field(
                name=f"{opponent.display_name}",
                value=format_result(opponent_result),
                inline=False,
            )
            return await duel_message.edit(embed=final_embed, view=None)

        if jogo == "pedra_papel_tesoura":
            display_choices = {"pedra": "Pedra", "papel": "Papel", "tesoura": "Tesoura"}

            def build_rps_embed(
                choices: dict[int, str], reveal: bool, result_text: str | None
            ) -> discord.Embed:
                def status_for(member: discord.Member) -> str:
                    choice_key = choices.get(member.id)
                    if reveal and choice_key:
                        return display_choices.get(choice_key, choice_key)
                    if choice_key:
                        return "Escolheu"
                    return "Ainda não escolheu"

                description = (
                    "Escolham Pedra, Papel ou Tesoura clicando nos botões enviados a vocês!"
                )
                if reveal and result_text:
                    description = result_text

                embed = discord.Embed(
                    title="Duelo - Pedra, Papel e Tesoura",
                    description=description,
                    color=discord.Color.green() if reveal else discord.Color.orange(),
                )
                embed.add_field(
                    name=f"Desafiante - {ctx.user.display_name}",
                    value=status_for(ctx.user),
                    inline=True,
                )
                embed.add_field(
                    name=f"Desafiado - {opponent.display_name}",
                    value=status_for(opponent),
                    inline=True,
                )
                return embed

            state = RpsDuelState(ctx.user, opponent)
            state.duel_message = duel_message
            state.embed_builder = build_rps_embed

            await duel_message.edit(
                embed=build_rps_embed(state.choices, reveal=False, result_text=None),
                view=None,
            )

            rps_views = []
            for participant in (ctx.user, opponent):
                view_instance = RpsDuelView(state)
                followup = ctx.followup if participant.id == ctx.user.id else opponent_followup
                message = await followup.send(
                    content=(
                        "Faça sua escolha! Clique em um dos botões abaixo para definir sua jogada."
                    ),
                    view=view_instance,
                    ephemeral=True,
                )
                view_instance.set_ephemeral_message(message)
                rps_views.append(view_instance)

            await state.done.wait()

            final_embed = build_rps_embed(
                state.choices, reveal=True, result_text=state.result_text
            )
            await duel_message.edit(embed=final_embed, view=None)

            for view in rps_views:
                if view.ephemeral_message:
                    await view.ephemeral_message.edit(
                        content=state.result_text,
                        view=None,
                    )
        else:
            def build_duel_embed(counts: dict[int, int], ongoing: bool) -> discord.Embed:
                challenger_hits = counts.get(ctx.user.id, 0)
                opponent_hits = counts.get(opponent.id, 0)
                status = (
                    "Toquem em **Atacar** o mais rápido possível por 10 segundos!"
                    if ongoing
                    else "Duelo encerrado!"
                )
                embed = discord.Embed(
                    title="Duelo",
                    description=status,
                    color=discord.Color.orange() if ongoing else discord.Color.green(),
                )
                embed.add_field(name="Desafiante", value=ctx.user.mention, inline=True)
                embed.add_field(name="Desafiado", value=opponent.mention, inline=True)
                if ongoing:
                    embed.add_field(
                        name="Ataques",
                        value=(
                            f"{ctx.user.display_name}: **{challenger_hits}**\n"
                            f"{opponent.display_name}: **{opponent_hits}**"
                        ),
                        inline=False,
                    )
                return embed

            counts = {ctx.user.id: 0, opponent.id: 0}
            await duel_message.edit(
                embed=build_duel_embed(counts, ongoing=True), view=None
            )

            attack_views = []
            for participant in (ctx.user, opponent):
                view_instance = DuelAttackView(
                    ctx.user, opponent, counts, duel_message, build_duel_embed
                )
                followup = ctx.followup if participant.id == ctx.user.id else opponent_followup
                message = await followup.send(
                    content=(
                        "Pressione **Atacar** o mais rápido possível por 10 segundos!"
                    ),
                    view=view_instance,
                    ephemeral=True,
                )
                view_instance.set_ephemeral_message(message)
                attack_views.append(view_instance)

            await asyncio.sleep(10)

            for view in attack_views:
                view.mark_finished()

            challenger_hits = counts.get(ctx.user.id, 0)
            opponent_hits = counts.get(opponent.id, 0)
            if challenger_hits > opponent_hits:
                winner_text = f"{ctx.user.mention} venceu o duelo!"
            elif opponent_hits > challenger_hits:
                winner_text = f"{opponent.mention} venceu o duelo!"
            else:
                winner_text = "O duelo terminou em empate!"

            final_embed = build_duel_embed(counts, ongoing=False)
            final_embed.description = (
                f"{winner_text}\n\n"
                f"Total de ataques:\n"
                f"{ctx.user.display_name}: **{challenger_hits}**\n"
                f"{opponent.display_name}: **{opponent_hits}**"
            )

            await duel_message.edit(embed=final_embed, view=None)

            for view in attack_views:
                if view.ephemeral_message:
                    await view.ephemeral_message.edit(
                        content=winner_text,
                        view=None,
                    )


async def setup(bot: commands.Bot):
    await bot.add_cog(PlayCog(bot))
