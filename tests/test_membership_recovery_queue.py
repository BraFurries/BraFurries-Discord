from core.membership_recovery_queue import MembershipRecoveryQueue


def test_failed_guild_requeues_at_tail_instead_of_starving_later_guilds():
    queue = MembershipRecoveryQueue()
    queue.mark_required(10)
    queue.mark_required(20)
    queue.mark_required(30)

    assert queue.pop_next() == 10
    queue.mark_required(10)

    assert queue.pop_next() == 20
    assert queue.pop_next() == 30
    assert queue.pop_next() == 10


def test_new_recovery_generation_is_not_cleared_by_older_reconciliation():
    queue = MembershipRecoveryQueue()
    first_generation = queue.mark_required(10)

    queue.mark_required(10)

    assert queue.clear_if_unchanged(10, first_generation) is False
    assert queue.is_required(10) is True


def test_success_clears_only_the_generation_it_started_with():
    queue = MembershipRecoveryQueue()
    generation = queue.mark_required(10)

    assert queue.clear_if_unchanged(10, generation) is True
    assert queue.is_required(10) is False
    assert queue.pop_next() is None


def test_failed_recovery_waits_until_cooldown_expires():
    current = [100.0]
    queue = MembershipRecoveryQueue(clock=lambda: current[0])

    queue.mark_required(10)
    assert queue.pop_next() == 10

    queue.mark_required(10, delay_seconds=900)

    assert queue.pop_next() is None
    current[0] += 899
    assert queue.pop_next() is None
    current[0] += 1
    assert queue.pop_next() == 10


def test_deferred_guild_does_not_block_ready_guilds():
    current = [100.0]
    queue = MembershipRecoveryQueue(clock=lambda: current[0])

    queue.mark_required(10, delay_seconds=900)
    queue.mark_required(20)

    assert queue.pop_next() == 20
    assert queue.pop_next() is None


def test_new_recovery_request_does_not_shorten_existing_cooldown():
    current = [100.0]
    queue = MembershipRecoveryQueue(clock=lambda: current[0])

    queue.mark_required(10, delay_seconds=900)
    current[0] += 60
    queue.mark_required(10)

    assert queue.pop_next() is None
    current[0] += 839
    assert queue.pop_next() is None
    current[0] += 1
    assert queue.pop_next() == 10


def test_deferred_state_is_visible_until_deadline():
    current = [100.0]
    queue = MembershipRecoveryQueue(clock=lambda: current[0])

    queue.mark_required(10, delay_seconds=900)

    assert queue.is_required(10) is True
    assert queue.is_deferred(10) is True

    current[0] += 900

    assert queue.is_deferred(10) is False
    assert queue.pop_next() == 10
