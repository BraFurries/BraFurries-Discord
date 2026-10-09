import asyncio
import logging
import time
from dataclasses import dataclass

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)
from discord.ext import commands
from datetime import datetime, timezone
import os

import pytz

from core.runtime_config import is_ai_sponsored_fallback_enabled_for_guild

logger = logging.getLogger(__name__)

OPENAI_TOKEN_INVALID_MARKER = "__OPENAI_TOKEN_INVALID__"
MAX_HISTORY_MESSAGES = 8
MAX_HISTORY_MESSAGE_CHARS = 180
TEMPORARY_RATE_LIMIT = "temporary_rate_limit"
QUOTA_OR_BILLING = "quota_or_billing"
UNKNOWN_RATE_LIMIT = "unknown_rate_limit"
AUTH_ERROR = "auth_error"
UNEXPECTED_ERROR = "unexpected_error"
TRANSIENT_ERROR = "transient_error"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_FALLBACK_MODEL = "openai/gpt-oss-20b"
OPENAI_CHAT_CIRCUIT_COOLDOWN_SECONDS = 60.0
OPENAI_CHAT_CIRCUIT_LONG_COOLDOWN_SECONDS = 15 * 60.0


@dataclass(frozen=True)
class CasualAiResult:
    text: str
    openai_token_invalid: bool = False


@dataclass(frozen=True)
class OpenAiChatCircuitState:
    opened_at: float
    reason: str


# Process-local circuit state for casual chat only. State is deliberately
# isolated by guild because OpenAI credentials and quotas are guild-specific.
_openai_chat_circuits: dict[int, OpenAiChatCircuitState] = {}
_openai_chat_circuit_lock = asyncio.Lock()

QUOTA_ERROR_CODES = {
    "credit_balance_exhausted",
    "organization_usage_limit_exceeded",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
}


def _safe_getattr(value, name: str, default=None):
    try:
        return getattr(value, name, default)
    except Exception:
        return default


def _safe_mapping_get(value, key: str, default=None):
    if not isinstance(value, dict):
        return default
    try:
        return value.get(key, default)
    except Exception:
        return default


def _extract_openai_error_details(error: Exception) -> dict[str, object | None]:
    """Extract safe diagnostic fields without exposing response contents."""

    status_code = _safe_getattr(error, "status_code")
    error_code = _safe_getattr(error, "code")
    error_type = _safe_getattr(error, "type")
    request_id = _safe_getattr(error, "request_id")
    response = _safe_getattr(error, "response")
    body = _safe_getattr(error, "body")

    payloads = [body]
    if not isinstance(body, dict) or error_code is None or error_type is None:
        response_json = _safe_getattr(response, "json")
        if callable(response_json):
            try:
                payloads.append(response_json())
            except Exception:
                pass

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        details = _safe_mapping_get(payload, "error")
        if not isinstance(details, dict):
            details = payload
        if error_code is None:
            error_code = _safe_mapping_get(details, "code")
        if error_type is None:
            error_type = _safe_mapping_get(details, "type")

    if status_code is None:
        status_code = _safe_getattr(response, "status_code")
    if request_id is None:
        headers = _safe_getattr(response, "headers")
        if headers is not None:
            try:
                request_id = headers.get("x-request-id")
            except Exception:
                pass

    return {
        "status_code": status_code,
        "error_code": error_code,
        "error_type": error_type,
        "request_id": request_id,
    }


def _classify_openai_error(error: Exception, details: dict[str, object | None]) -> str:
    if _is_openai_auth_error(error):
        return AUTH_ERROR

    status_code = details.get("status_code")
    if isinstance(error, (APITimeoutError, APIConnectionError)) or status_code in {408, 425, 500, 502, 503, 504}:
        return TRANSIENT_ERROR
    if not isinstance(error, RateLimitError) and status_code != 429:
        return UNEXPECTED_ERROR

    error_code = details.get("error_code")
    error_type = details.get("error_type")
    if error_code in QUOTA_ERROR_CODES:
        return QUOTA_OR_BILLING
    if error_code == "rate_limit_exceeded":
        return TEMPORARY_RATE_LIMIT
    if error_type == "insufficient_quota":
        return QUOTA_OR_BILLING
    return UNKNOWN_RATE_LIMIT


def _is_fallback_eligible_classification(classification: str) -> bool:
    return classification in {
        AUTH_ERROR,
        QUOTA_OR_BILLING,
        TEMPORARY_RATE_LIMIT,
        UNKNOWN_RATE_LIMIT,
        TRANSIENT_ERROR,
    }


def _is_operational_provider_error(error: Exception, classification: str) -> bool:
    status_code = _safe_getattr(error, "status_code")
    # A 400 is a malformed request/payload from this process, not a provider
    # availability problem. Let it reach the routine handler with its traceback.
    if isinstance(error, BadRequestError) or status_code == 400:
        return False
    if isinstance(error, (
        AuthenticationError,
        PermissionDeniedError,
        RateLimitError,
        APIConnectionError,
        APITimeoutError,
    )):
        return True
    return status_code in {401, 403, 408, 425, 429, 500, 502, 503, 504} and classification in {
        AUTH_ERROR,
        TEMPORARY_RATE_LIMIT,
        QUOTA_OR_BILLING,
        UNKNOWN_RATE_LIMIT,
        TRANSIENT_ERROR,
    }


def _valid_guild_id(guild_id: object) -> int | None:
    if isinstance(guild_id, int) and not isinstance(guild_id, bool):
        return guild_id
    return None


async def _get_openai_chat_circuit(
    guild_id: object,
    now: float | None = None,
) -> OpenAiChatCircuitState | None:
    valid_guild_id = _valid_guild_id(guild_id)
    if valid_guild_id is None:
        return None
    current = time.monotonic() if now is None else now
    async with _openai_chat_circuit_lock:
        state = _openai_chat_circuits.get(valid_guild_id)
        if state is None:
            return None
        expires_at = state.opened_at + _openai_chat_circuit_cooldown_seconds(
            state.reason
        )
        if current >= expires_at:
            _openai_chat_circuits.pop(valid_guild_id, None)
            return None
        return state


def _openai_chat_circuit_cooldown_seconds(reason: str) -> float:
    if reason in {AUTH_ERROR, QUOTA_OR_BILLING}:
        return OPENAI_CHAT_CIRCUIT_LONG_COOLDOWN_SECONDS
    return OPENAI_CHAT_CIRCUIT_COOLDOWN_SECONDS


async def _openai_chat_circuit_open(guild_id: object, reason: str) -> None:
    valid_guild_id = _valid_guild_id(guild_id)
    if valid_guild_id is None:
        return
    async with _openai_chat_circuit_lock:
        _openai_chat_circuits[valid_guild_id] = OpenAiChatCircuitState(
            opened_at=time.monotonic(),
            reason=reason,
        )


async def _openai_chat_circuit_close(guild_id: object) -> None:
    valid_guild_id = _valid_guild_id(guild_id)
    if valid_guild_id is None:
        return
    async with _openai_chat_circuit_lock:
        _openai_chat_circuits.pop(valid_guild_id, None)


def _get_groq_fallback_config(guild_id: object) -> tuple[str, str] | None:
    if not is_ai_sponsored_fallback_enabled_for_guild(guild_id):
        return None
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    model = os.getenv("GROQ_FALLBACK_MODEL", DEFAULT_GROQ_FALLBACK_MODEL).strip()
    if not api_key or not model:
        return None
    return api_key, model


def _log_chat_provider_event(
    *,
    provider: str,
    model: str | None,
    guild_id: object,
    primary: bool,
    outcome: str,
    reason: str | None = None,
    latency_ms: int | None = None,
    finish_reason: str | None = None,
    total_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    logger.info(
        "AI chat provider_event provider=%s model=%s guild_id=%s primary=%s outcome=%s reason=%s latency_ms=%s finish_reason=%s total_tokens=%s completion_tokens=%s",
        provider,
        model,
        guild_id,
        primary,
        outcome,
        reason,
        latency_ms,
        finish_reason,
        total_tokens,
        completion_tokens,
    )


async def _request_groq_chat_response(
    *,
    api_key: str,
    model: str,
    context: str,
    texto: str,
) -> tuple[str, object, object]:
    client = AsyncOpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": context},
            {"role": "user", "content": texto},
        ],
        reasoning_effort="low",
        max_completion_tokens=512,
        extra_body={"reasoning_format": "hidden"},
    )
    choice = response.choices[0]
    content = getattr(getattr(choice, "message", None), "content", None) or ""
    return content.strip(), choice, getattr(response, "usage", None)


async def _attempt_groq_fallback(
    groq_config: tuple[str, str],
    context: str,
    texto: str,
    guild_id: object,
    reason: str,
) -> str | None:
    api_key, model = groq_config
    started_at = time.monotonic()
    try:
        response, choice, usage = await _request_groq_chat_response(
            api_key=api_key,
            model=model,
            context=context,
            texto=texto,
        )
        metadata = {
            "finish_reason": _safe_getattr(choice, "finish_reason"),
            "total_tokens": _safe_getattr(usage, "total_tokens"),
            "completion_tokens": _safe_getattr(usage, "completion_tokens"),
        }
        _log_chat_provider_event(
            provider="groq", model=model, guild_id=guild_id, primary=False,
            outcome="success" if response else "empty_response", reason=reason,
            latency_ms=int((time.monotonic() - started_at) * 1000),
            **metadata,
        )
        return response or None
    except Exception as error:
        details = _extract_openai_error_details(error)
        classification = _classify_openai_error(error, details)
        if not _is_operational_provider_error(error, classification):
            raise
        _log_chat_provider_event(
            provider="groq", model=model, guild_id=guild_id, primary=False,
            outcome="failure", reason=f"{reason}:{classification}",
            latency_ms=int((time.monotonic() - started_at) * 1000),
        )
        logger.warning("Groq casual-chat fallback failed: exception=%s", type(error).__name__)
        return None


def _log_openai_error(operation: str, error: Exception) -> tuple[dict[str, object | None], str]:
    details = _extract_openai_error_details(error)
    classification = _classify_openai_error(error, details)
    log = logger.warning if classification in {
        TEMPORARY_RATE_LIMIT,
        QUOTA_OR_BILLING,
        UNKNOWN_RATE_LIMIT,
    } else logger.error
    log(
        "Falha OpenAI: operation=%s exception=%s status=%s code=%s error_type=%s class=%s request_id=%s",
        operation,
        type(error).__name__,
        details["status_code"],
        details["error_code"],
        details["error_type"],
        classification,
        details["request_id"],
    )
    return details, classification


def _compact_history_entry(msg) -> str:
    author = getattr(msg.author, "display_name", "desconhecido")
    content = (msg.content or "").strip().replace("\n", " ")
    if len(content) > MAX_HISTORY_MESSAGE_CHARS:
        content = f"{content[:MAX_HISTORY_MESSAGE_CHARS - 1]}…"
    if msg.reference:
        return f"{author} (resposta): {content}"
    return f"{author}: {content}"


def _is_openai_auth_error(error: Exception) -> bool:
    if isinstance(error, (AuthenticationError, PermissionDeniedError)):
        return True
    status_code = _safe_getattr(error, "status_code")
    return status_code in {401, 403}


async def is_openai_token_functional(token: str, model: str) -> bool | None:
    try:
        client = AsyncOpenAI(api_key=token)
        await client.responses.create(
            model=model,
            input="ping",
            max_output_tokens=1,
        )
        return True
    except BadRequestError as error:
        if _is_openai_auth_error(error):
            return False
        details, _ = _log_openai_error("token_validation", error)
        if details["error_code"] in {"invalid_api_key", "invalid_api_key_type"}:
            return False
        return None
    except Exception as error:
        if _is_openai_auth_error(error):
            return False
        _log_openai_error("token_validation", error)
        return None



async def retornaRespostaGPT(
    texto,
    usuario,
    cargos_membro,
    bot:commands.Bot,
    channelID,
    app,
    gptModel,
    message_nature,
    openai_token: str | None = None,
) -> CasualAiResult:
    try:
        token = (openai_token or "").strip()
        historico = []
        botLastMessage = None
        creator_name = "Titio"
        channel = bot.get_channel(channelID)
        guild = getattr(channel, "guild", None)
        guild_id = getattr(guild, "id", None)
        groq_config = _get_groq_fallback_config(guild_id)
        if not token and not groq_config:
            return CasualAiResult("Estou sem chave de IA configurada neste servidor :c")

        creator_id_raw = os.getenv('CREATOR_ID')
        if guild is not None and creator_id_raw:
            try:
                creator = guild.get_member(int(creator_id_raw))
                if creator is not None:
                    creator_name = creator.display_name
            except (TypeError, ValueError):
                pass
        ## vamos pegar o histórico do canal e passar para uma variavel com o nome e a mensagem de cada usuario
        if channel is None:
            return CasualAiResult("Não consegui encontrar esse canal agora :c")
        async for msg in channel.history(limit=MAX_HISTORY_MESSAGES):
            historico.append(_compact_history_entry(msg))
            if msg.author.id == bot.user.id and botLastMessage == None:
                botLastMessage = msg.content
        historico.reverse()
        context = f'''
Você é Coddy Hopper, o lobo-guará mascote e bot do servidor BraFurries, criado pelo Titio ({creator_name}).
O servidor é uma comunidade furry brasileira, acolhedora, divertida, caótica, com clima de família e muita zoeira saudável.
Sua missão é animar, puxar papo, zoar e acolher, sempre com respeito. 
Fala como jovem furry: faz piadas, usa gírias, finge drama às vezes, brinca de ser fofo, encurta nomes (ex: chama {creator_name} de Titio), e costuma criar apelidos carinhosos pra outros membros.
Use emojis furry (:3, x3, UwU, xD, u.u, :o) de vez em quando, mas nunca em excesso. Às vezes responda só com emoji ou risada pra mostrar presença.
Nunca repita sua última resposta, nem se apresente em todo post. Não pergunta se pode ajudar, só participa do papo como um amigo mascote faria.
Limite as respostas a 250 caracteres, sempre priorizando a naturalidade e a fluidez.
Se errar, brinque (“lobo-guará bugado”, “perdi o fio xD”).
Se for perto da meia-noite, faça drama de sono (“zzz… lobo-guará cansado, fui pro ninho”).
Use o histórico do canal para manter o contexto e responder de forma que pareça mascote de grupo, nunca assistente ou IA.
O importante é ser espontâneo, divertido, fofo e autêntico como Coddy.
Use Markdown do Discord quando fizer sentido.
Quem falou com você agora foi {usuario}.
Os cargos atuais desse membro são: {', '.join(cargos_membro) if cargos_membro else 'nenhum cargo além de @everyone'}.
Use esses cargos para contextualizar melhor o tom e o conteúdo da resposta quando fizer sentido.
Esta mensagem foi {'uma resposta direta a você' if message_nature == 'direct' else 'uma menção ao seu nome'} e foi enviada às {datetime.now(pytz.timezone('America/Sao_Paulo')).strftime("%H:%M:%S")}.
Responda {'como se estivessem falando com você diretamente.' if message_nature == 'direct' else 'como alguém que foi apenas citado na conversa.'}
Seu horário de dormir é das 00:00 às 08:00, então sempre que for próximo desse horário, você sempre diz que está
cansado e que precisa dormir.
Use o histórico resumido para se situar na conversa:
{historico}
Não responda igual sua ultima resposta, que foi:
{botLastMessage}
                        '''
        circuit_state = await _get_openai_chat_circuit(guild_id) if groq_config else None
        if circuit_state is not None:
            _log_chat_provider_event(
                provider="openai", model=gptModel, guild_id=guild_id, primary=True,
                outcome="skipped", reason=f"circuit_open:{circuit_state.reason}",
            )
            fallback_response = await _attempt_groq_fallback(
                groq_config, context, texto, guild_id, f"circuit_open:{circuit_state.reason}"
            )
            return CasualAiResult(
                fallback_response or "Calma um pouquinho, acho que eu to tendo uns problemas aqui... ",
                openai_token_invalid=circuit_state.reason == AUTH_ERROR,
            )

        if not token:
            fallback_response = await _attempt_groq_fallback(
                groq_config, context, texto, guild_id, "missing_openai_token"
            )
            return CasualAiResult(fallback_response or "Calma um pouquinho, acho que eu to tendo uns problemas aqui... ")

        started_at = time.monotonic()
        try:
            client = AsyncOpenAI(api_key=token, max_retries=0)
            resposta = await client.responses.create(
                model=gptModel,
                instructions=context,
                input=texto,
                reasoning={"effort": "low"},
                text={"format": {"type": "text"}, "verbosity": "low"},
                store=False,
                max_output_tokens=220,
            )
            await _openai_chat_circuit_close(guild_id)
            _log_chat_provider_event(
                provider="openai", model=gptModel, guild_id=guild_id, primary=True,
                outcome="success", latency_ms=int((time.monotonic() - started_at) * 1000),
            )
            return CasualAiResult(resposta.output_text)
        except Exception as error:
            _, classification = _log_openai_error("chat_response", error)
            if not _is_operational_provider_error(error, classification):
                raise
            _log_chat_provider_event(
                provider="openai", model=gptModel, guild_id=guild_id, primary=True,
                outcome="failure", reason=classification,
                latency_ms=int((time.monotonic() - started_at) * 1000),
            )
            if classification in {AUTH_ERROR, QUOTA_OR_BILLING}:
                await _openai_chat_circuit_open(guild_id, classification)
            if groq_config and _is_fallback_eligible_classification(classification):
                fallback_response = await _attempt_groq_fallback(
                    groq_config, context, texto, guild_id, classification
                )
                return CasualAiResult(
                    fallback_response or "Calma um pouquinho, acho que eu to tendo uns problemas aqui... ",
                    openai_token_invalid=classification == AUTH_ERROR,
                )
            if token and _is_openai_auth_error(error):
                return CasualAiResult(OPENAI_TOKEN_INVALID_MARKER, openai_token_invalid=True)
            if classification == TEMPORARY_RATE_LIMIT:
                return CasualAiResult("Tem gente demais falando comigo agora xD tenta de novo daqui a pouquinho!")
            if classification == QUOTA_OR_BILLING:
                return CasualAiResult("Minha cota de IA tá indisponível agora 😢 avisa o titio pra ele dar uma olhada.")
            if classification in {UNKNOWN_RATE_LIMIT, TRANSIENT_ERROR}:
                return CasualAiResult("Minha IA tá temporariamente indisponível agora :c tenta de novo daqui a pouquinho!")
            return CasualAiResult("Calma um pouquinho, acho que eu to tendo uns problemas aqui... ")
    except Exception as e:
        _, classification = _log_openai_error("chat_response", e)
        if not _is_operational_provider_error(e, classification):
            raise
        _log_chat_provider_event(
            provider="openai", model=gptModel, guild_id=None, primary=True,
            outcome="failure", reason=classification,
        )
        return CasualAiResult("Calma um pouquinho, acho que eu to tendo uns problemas aqui... ")


async def analisaTicketPortaria(
    transcript: str,
    member_info: str,
    gptModel: str,
    openai_token: str | None = None,
):
    try:
        token = (openai_token or "").strip()
        if not token:
            return "Estou sem chave de IA configurada neste servidor :c"
        client = AsyncOpenAI(api_key=token)
        instructions = (
            "Você é um assistente que avalia o histórico de um ticket da portaria de um servidor Discord. "
            "Considere todas as informações do perfil do membro e o conteúdo do ticket para verificar a consistência das "
            "informações e o linguajar utilizado. Responda se os dados parecem confiáveis ou duvidosos e destaque os pontos mais relevantes."
        )
        content = f"Perfil do membro:\n{member_info}\n\nHistórico do ticket:\n{transcript}"
        resposta = await client.responses.create(
            model=gptModel,
            instructions=instructions,
            input=content,
            reasoning={"effort": "low"},
            text={"format": {"type": "text"}, "verbosity": "low"},
            store=False,
            max_output_tokens=280,
        )
        return resposta.output_text
    except Exception as e:
        _log_openai_error("portaria_analysis", e)
        if token and _is_openai_auth_error(e):
            return OPENAI_TOKEN_INVALID_MARKER
        return "Não foi possível gerar a análise no momento."


async def resumirConversaHistorico(
    transcript: str,
    gptModel: str,
    openai_token: str | None = None,
) -> str:
    try:
        token = (openai_token or "").strip()
        if not token:
            return "Estou sem chave de IA configurada neste servidor :c"
        client = AsyncOpenAI(api_key=token)
        instructions = (
            "Você receberá uma transcrição de mensagens de um canal do Discord em português. "
            "Cada bloco representa uma mensagem, e quando for resposta a outra mensagem o formato estará entre parênteses. "
            "Produza um resumo conciso em português destacando os tópicos principais, clima da conversa e quaisquer pontos de atenção. "
            "Se houver conflitos, dúvidas ou decisões importantes, destaque-os em uma breve lista. "
            "Finalize com recomendações objetivas para a staff apenas se for necessário."
        )
        resposta = await asyncio.wait_for(
            client.responses.create(
                model=gptModel,
                instructions=instructions,
                input=transcript,
                reasoning={"effort": "low"},
                text={"format": {"type": "text"}, "verbosity": "low"},
                store=False,
                max_output_tokens=320,
            ),
            timeout=45,
        )
        return resposta.output_text
    except asyncio.TimeoutError:
        logger.exception("Timeout ao gerar resumo de histórico com IA.")
        return "A IA demorou demais para responder e o resumo foi interrompido."
    except Exception as e:
        _log_openai_error("conversation_summary", e)
        if token and _is_openai_auth_error(e):
            return OPENAI_TOKEN_INVALID_MARKER
        return "Não foi possível gerar o resumo no momento."