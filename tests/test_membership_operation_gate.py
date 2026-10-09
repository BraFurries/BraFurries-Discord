import asyncio

from core.membership_operation_gate import (
    membership_event_guard,
    membership_reconciliation_guard,
)


def test_same_member_events_are_serialized():
    async def scenario():
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        order = []

        async def first():
            async with membership_event_guard(1001, 5001):
                order.append("first-enter")
                first_entered.set()
                await release_first.wait()
                order.append("first-exit")

        async def second():
            await first_entered.wait()
            async with membership_event_guard(1001, 5001):
                order.append("second-enter")

        first_task = asyncio.create_task(first())
        second_task = asyncio.create_task(second())
        await first_entered.wait()
        await asyncio.sleep(0)
        assert order == ["first-enter"]

        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert order == ["first-enter", "first-exit", "second-enter"]

    asyncio.run(scenario())


def test_different_member_events_can_run_concurrently():
    async def scenario():
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release = asyncio.Event()

        async def event(member_id, entered):
            async with membership_event_guard(1002, member_id):
                entered.set()
                await release.wait()

        first = asyncio.create_task(event(5001, first_entered))
        second = asyncio.create_task(event(5002, second_entered))
        await asyncio.wait_for(first_entered.wait(), timeout=1)
        await asyncio.wait_for(second_entered.wait(), timeout=1)
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())


def test_reconciliation_waits_for_inflight_event_and_blocks_new_events():
    async def scenario():
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        reconciliation_entered = asyncio.Event()
        release_reconciliation = asyncio.Event()
        second_entered = asyncio.Event()

        async def first_event():
            async with membership_event_guard(1003, 5001):
                first_entered.set()
                await release_first.wait()

        async def reconcile():
            await first_entered.wait()
            async with membership_reconciliation_guard(1003):
                reconciliation_entered.set()
                await release_reconciliation.wait()

        async def second_event():
            await first_entered.wait()
            await asyncio.sleep(0)
            async with membership_event_guard(1003, 5002):
                second_entered.set()

        first = asyncio.create_task(first_event())
        await first_entered.wait()
        reconciliation = asyncio.create_task(reconcile())
        await asyncio.sleep(0)
        second = asyncio.create_task(second_event())
        await asyncio.sleep(0)

        release_first.set()
        await asyncio.wait_for(reconciliation_entered.wait(), timeout=1)
        assert not second_entered.is_set()

        release_reconciliation.set()
        await asyncio.wait_for(second_entered.wait(), timeout=1)
        await asyncio.gather(first, reconciliation, second)

    asyncio.run(scenario())
