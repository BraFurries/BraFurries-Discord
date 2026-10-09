import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.community_lifecycle_api import (
    ClaimedSyncRun,
    CommunityLifecycleApiClient,
    CommunityLifecycleApiError,
    IncompleteGuildSnapshot,
    fetch_complete_members,
    observe_network_with_retry,
)
from core.community_reconciliation import (
    MEMBER_BATCH_SIZE,
    reconcile_claimed_run,
    reconcile_guild,
    reconciliation_trigger_for_observation,
)


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.payload

    async def text(self):
        return str(self.payload)


class FakeSession:
    def __init__(self, responses, request_log):
        self.responses = responses
        self.request_log = request_log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def request(self, method, url, **kwargs):
        self.request_log.append((method, url, kwargs))
        return self.responses.pop(0)


def session_factory(responses, request_log):
    return lambda **_kwargs: FakeSession(responses, request_log)


class FakeGuild:
    def __init__(self, members, *, member_count=None):
        self.id = 100
        self.name = "Guild"
        self.owner_id = 1
        self.owner = None
        self.member_count = len(members) if member_count is None else member_count
        self._members = list(members)

    async def fetch_members(self, limit=None):
        assert limit is None
        for member in self._members:
            yield member


def fake_member(user_id=1):
    guild = SimpleNamespace(id=100)
    return SimpleNamespace(
        id=user_id,
        name=f"user{user_id}",
        global_name=f"Global {user_id}",
        display_name=f"Guild {user_id}",
        joined_at=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
        guild=guild,
        roles=[],
    )


def test_observe_network_uses_shared_internal_contract():
    requests = []
    responses = [FakeResponse({"communityId": 5})]
    client = CommunityLifecycleApiClient(
        base_url="http://api.internal/",
        token="shared-token",
        session_factory=session_factory(responses, requests),
    )
    guild = FakeGuild([fake_member(1)])

    result = asyncio.run(client.observe_network(guild, active=True))

    assert result["communityId"] == 5
    method, url, kwargs = requests[0]
    assert method == "PUT"
    assert url == "http://api.internal/internal/community-networks/discord/100"
    assert kwargs["headers"] == {"Authorization": "Bearer shared-token"}
    assert kwargs["json"] == {
        "name": "Guild",
        "ownerDiscordUserId": "1",
        "active": True,
        "observedMembers": 1,
    }


def test_observe_network_retry_recovers_transient_bootstrap_failure():
    class FlakyClient:
        def __init__(self):
            self.calls = 0

        async def observe_network(self, guild, *, active):
            self.calls += 1
            if self.calls < 3:
                raise CommunityLifecycleApiError(
                    "temporary",
                    status=503,
                    retryable=True,
                )
            return {"communityId": 77, "guildId": str(guild.id)}

    client = FlakyClient()
    guild = FakeGuild([fake_member(1)])

    result = asyncio.run(
        observe_network_with_retry(
            client,
            guild,
            active=True,
            attempts=3,
            base_delay_seconds=0,
        )
    )

    assert result["communityId"] == 77
    assert client.calls == 3


def test_http_503_is_classified_as_retryable():
    requests = []
    responses = [FakeResponse({"error": "temporarily unavailable"}, status=503)]
    client = CommunityLifecycleApiClient(
        base_url="http://api.internal",
        token="shared-token",
        session_factory=session_factory(responses, requests),
    )
    guild = FakeGuild([fake_member(1)])

    with pytest.raises(CommunityLifecycleApiError) as captured:
        asyncio.run(client.observe_network(guild, active=True))

    assert captured.value.status == 503
    assert captured.value.retryable is True


def test_malformed_success_payload_is_not_retryable():
    class MalformedResponse(FakeResponse):
        async def json(self):
            raise ValueError("invalid json")

    requests = []
    responses = [MalformedResponse(status=200)]
    client = CommunityLifecycleApiClient(
        base_url="http://api.internal",
        token="shared-token",
        session_factory=session_factory(responses, requests),
    )
    guild = FakeGuild([fake_member(1)])

    with pytest.raises(CommunityLifecycleApiError) as captured:
        asyncio.run(client.observe_network(guild, active=True))

    assert captured.value.status is None
    assert captured.value.retryable is False


def test_http_401_is_not_retryable():
    requests = []
    responses = [FakeResponse({"error": "unauthorized"}, status=401)]
    client = CommunityLifecycleApiClient(
        base_url="http://api.internal",
        token="shared-token",
        session_factory=session_factory(responses, requests),
    )
    guild = FakeGuild([fake_member(1)])

    with pytest.raises(CommunityLifecycleApiError) as captured:
        asyncio.run(client.observe_network(guild, active=True))

    assert captured.value.status == 401
    assert captured.value.retryable is False


def test_observe_network_retry_fails_fast_on_permanent_client_error():
    class FailingClient:
        def __init__(self):
            self.calls = 0

        async def observe_network(self, guild, *, active):
            self.calls += 1
            raise CommunityLifecycleApiError("unauthorized", status=401)

    client = FailingClient()
    guild = FakeGuild([fake_member(1)])

    with pytest.raises(CommunityLifecycleApiError) as captured:
        asyncio.run(
            observe_network_with_retry(
                client,
                guild,
                active=True,
                attempts=3,
                base_delay_seconds=0,
            )
        )

    assert captured.value.status == 401
    assert client.calls == 1


def test_claim_queue_204_means_no_work():
    requests = []
    responses = [FakeResponse(None, status=204)]
    client = CommunityLifecycleApiClient(
        base_url="http://api.internal",
        token="shared-token",
        session_factory=session_factory(responses, requests),
    )

    assert asyncio.run(client.claim_next_sync()) is None


def test_missing_internal_token_fails_closed():
    client = CommunityLifecycleApiClient(
        base_url="http://api.internal",
        token="",
    )

    with pytest.raises(CommunityLifecycleApiError, match="não configurada"):
        asyncio.run(client.claim_next_sync())


def test_complete_member_fetch_fails_closed_on_count_mismatch():
    guild = FakeGuild([fake_member(1)], member_count=2)

    with pytest.raises(IncompleteGuildSnapshot, match="incompleto"):
        asyncio.run(fetch_complete_members(guild, attempts=1))


def test_complete_member_fetch_accepts_count_churn_during_exhaustive_listing():
    class ChurningGuild(FakeGuild):
        async def fetch_members(self, limit=None):
            assert limit is None
            for index, member in enumerate(self._members):
                yield member
                if index == 0:
                    self.member_count += 1

    guild = ChurningGuild([fake_member(1), fake_member(2)], member_count=2)

    members = asyncio.run(fetch_complete_members(guild, attempts=1))

    assert [member.id for member in members] == [1, 2]
    assert guild.member_count == 3


class RecordingLifecycleClient:
    def __init__(self):
        self.calls = []

    async def observe_network(self, guild, *, active):
        self.calls.append(("observe", guild.id, active))
        return {}

    async def create_sync_run(self, guild_id, trigger):
        self.calls.append(("create", guild_id, trigger))
        return {"runId": 77}

    async def apply_member_batch(self, guild, *, run_id, members, approved_resolver):
        self.calls.append(
            ("batch", guild.id, run_id, [member.id for member in members], [
                approved_resolver(member) for member in members
            ])
        )
        return {"observedMembers": len(members), "updatedMembers": len(members)}

    async def finalize_member_snapshot(self, guild_id, *, run_id, observed_members):
        self.calls.append(("finalize", guild_id, run_id, observed_members))
        return {
            "communityId": 10,
            "guildId": str(guild_id),
            "observedMembers": observed_members,
            "updatedMembers": 0,
        }

    async def finish_sync_run(
        self,
        guild_id,
        run_id,
        *,
        success,
        observed_members=None,
        updated_members=None,
        error_code=None,
    ):
        self.calls.append(
            (
                "finish",
                guild_id,
                run_id,
                success,
                observed_members,
                updated_members,
                error_code,
            )
        )
        return {}


def test_healthy_matching_network_stays_event_driven():
    assert reconciliation_trigger_for_observation(
        sync_state="HEALTHY",
        tracked_present_members=5500,
        provider_member_count=5500,
    ) is None


def test_required_baseline_uses_startup_trigger_only_during_bootstrap():
    assert reconciliation_trigger_for_observation(
        sync_state="RECONCILIATION_REQUIRED",
        tracked_present_members=0,
        provider_member_count=5500,
        required_state_trigger="STARTUP",
    ) == "STARTUP"

    assert reconciliation_trigger_for_observation(
        sync_state="RECONCILIATION_REQUIRED",
        tracked_present_members=0,
        provider_member_count=5500,
    ) == "DRIFT"


def test_count_drift_requests_full_reconciliation():
    assert reconciliation_trigger_for_observation(
        sync_state="HEALTHY",
        tracked_present_members=5498,
        provider_member_count=5500,
    ) == "DRIFT"


def test_delivery_failure_requires_recovery_even_when_count_matches():
    assert reconciliation_trigger_for_observation(
        sync_state="HEALTHY",
        tracked_present_members=5500,
        provider_member_count=5500,
        local_recovery_required=True,
    ) == "RECOVERY"


def test_reconciling_network_does_not_start_parallel_full_scan():
    assert reconciliation_trigger_for_observation(
        sync_state="RECONCILING",
        tracked_present_members=5400,
        provider_member_count=5500,
        local_recovery_required=True,
    ) is None


def test_fresh_startup_recovers_reconciliation_left_running_by_previous_process():
    assert reconciliation_trigger_for_observation(
        sync_state="RECONCILING",
        tracked_present_members=5400,
        provider_member_count=5500,
        recover_stale_reconciling=True,
    ) == "STARTUP"


def test_fresh_startup_recovers_orphaned_run_even_after_network_became_healthy():
    assert reconciliation_trigger_for_observation(
        sync_state="HEALTHY",
        tracked_present_members=5500,
        provider_member_count=5500,
        recover_stale_reconciling=True,
        membership_reconciliation_running=True,
    ) == "STARTUP"


def test_nonstartup_health_check_does_not_supersede_running_reconciliation():
    assert reconciliation_trigger_for_observation(
        sync_state="HEALTHY",
        tracked_present_members=5500,
        provider_member_count=5500,
        membership_reconciliation_running=True,
    ) is None


def test_claimed_run_preflight_failure_marks_run_failed():
    guild = FakeGuild([fake_member(1)])
    client = RecordingLifecycleClient()
    bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 100 else None)

    def failing_resolver_factory(_guild):
        raise RuntimeError("portaria unavailable")

    with pytest.raises(RuntimeError, match="portaria unavailable"):
        asyncio.run(
            reconcile_claimed_run(
                bot,
                client,
                ClaimedSyncRun(run_id=91, guild_id=100, trigger="MANUAL"),
                approved_resolver_factory=failing_resolver_factory,
            )
        )

    assert ("finish", 100, 91, False, None, None, "RUNTIME_ERROR") in client.calls


def test_reconcile_guild_batches_members_then_finalizes_complete_snapshot():
    members = [fake_member(user_id) for user_id in range(1, MEMBER_BATCH_SIZE + 2)]
    guild = FakeGuild(members)
    client = RecordingLifecycleClient()

    result = asyncio.run(
        reconcile_guild(
            guild,
            client,
            trigger="STARTUP",
            approved_resolver=lambda _member: True,
        )
    )

    assert result == {
        "communityId": 10,
        "guildId": "100",
        "observedMembers": MEMBER_BATCH_SIZE + 1,
        "updatedMembers": MEMBER_BATCH_SIZE + 1,
    }
    assert client.calls[0:2] == [
        ("observe", 100, True),
        ("create", 100, "STARTUP"),
    ]
    first_batch = client.calls[2]
    second_batch = client.calls[3]
    assert first_batch[0:3] == ("batch", 100, 77)
    assert len(first_batch[3]) == MEMBER_BATCH_SIZE
    assert second_batch[0:3] == ("batch", 100, 77)
    assert len(second_batch[3]) == 1
    assert client.calls[4] == (
        "finalize",
        100,
        77,
        MEMBER_BATCH_SIZE + 1,
    )
    assert client.calls[5] == (
        "finish",
        100,
        77,
        True,
        MEMBER_BATCH_SIZE + 1,
        MEMBER_BATCH_SIZE + 1,
        None,
    )


def test_reconcile_records_failing_batch_offset_in_error_code():
    class FailingSecondBatchClient(RecordingLifecycleClient):
        def __init__(self):
            super().__init__()
            self.batch_calls = 0

        async def apply_member_batch(
            self,
            guild,
            *,
            run_id,
            members,
            approved_resolver,
        ):
            self.batch_calls += 1
            if self.batch_calls == 2:
                raise CommunityLifecycleApiError(
                    "API lifecycle respondeu 400: invalid member",
                    status=400,
                )
            return await super().apply_member_batch(
                guild,
                run_id=run_id,
                members=members,
                approved_resolver=approved_resolver,
            )

    members = [fake_member(user_id) for user_id in range(1, MEMBER_BATCH_SIZE + 2)]
    guild = FakeGuild(members)
    client = FailingSecondBatchClient()

    with pytest.raises(CommunityLifecycleApiError, match="batch iniciado em 100"):
        asyncio.run(
            reconcile_guild(
                guild,
                client,
                trigger="RECOVERY",
                approved_resolver=lambda _member: True,
            )
        )

    assert (
        "finish",
        100,
        77,
        False,
        None,
        None,
        "API_400_BATCH_100",
    ) in client.calls


def test_finalize_snapshot_sends_only_count_not_member_id_list():
    requests = []
    responses = [FakeResponse({
        "communityId": 10,
        "guildId": "100",
        "observedMembers": 5500,
        "updatedMembers": 0,
    })]
    client = CommunityLifecycleApiClient(
        base_url="http://api.internal",
        token="shared-token",
        session_factory=session_factory(responses, requests),
    )

    asyncio.run(
        client.finalize_member_snapshot(
            100,
            run_id=77,
            observed_members=5500,
        )
    )

    _, _, kwargs = requests[0]
    assert kwargs["json"] == {
        "runId": 77,
        "complete": True,
        "observedMembers": 5500,
    }
    assert "observedDiscordUserIds" not in kwargs["json"]


def test_api_http_status_is_preserved_for_safe_sync_error_code():
    requests = []
    responses = [FakeResponse({"message": "too large"}, status=413)]
    client = CommunityLifecycleApiClient(
        base_url="http://api.internal",
        token="shared-token",
        session_factory=session_factory(responses, requests),
    )
    guild = FakeGuild([fake_member(1)])

    with pytest.raises(CommunityLifecycleApiError) as captured:
        asyncio.run(
            client.apply_member_batch(
                guild,
                run_id=77,
                members=[fake_member(1)],
                approved_resolver=lambda _member: True,
            )
        )

    assert captured.value.status == 413
