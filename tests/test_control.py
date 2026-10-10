from __future__ import annotations

import asyncio

from control import WorkerControl


def test_pause_and_resume_do_not_clear_caller_state():
    control = WorkerControl()
    state = {"job": "downloading album", "page": "4", "checked": False}
    control.start()
    control.pause()

    async def scenario():
        async def resume_later():
            await asyncio.sleep(0.15)
            assert state == {"job": "downloading album", "page": "4", "checked": False}
            control.resume()

        task = asyncio.create_task(resume_later())
        result = await control.gate("album:4", sleep=0.05)
        await task
        return result

    assert asyncio.run(scenario()) == "ok"
    assert control.is_running()
    assert state["job"] == "downloading album"


def test_pause_extends_deadline_and_active_wait_does_not():
    control = WorkerControl()
    control.start()

    async def paused():
        control.pause()

        async def resume_later():
            await asyncio.sleep(0.2)
            control.resume()

        task = asyncio.create_task(resume_later())
        deadline = [1000.0]
        result = await control.gate("album:1", sleep=0.05, deadline=deadline)
        await task
        return result, deadline[0]

    result, deadline = asyncio.run(paused())
    assert result == "ok"
    assert deadline > 1000.05

    async def active():
        deadline = [1000.0]
        result = await control.gate("album:1", sleep=0.05, deadline=deadline)
        return result, deadline[0]

    result, deadline = asyncio.run(active())
    assert result == "ok"
    assert deadline == 1000.0


def test_skip_hits_only_the_current_item():
    control = WorkerControl()
    control.start()
    control.arm_skip("album:2")

    async def scenario():
        other = await control.gate("album:1")
        current = await control.gate("album:2")
        again = await control.gate("album:2")
        return other, current, again

    assert asyncio.run(scenario()) == ("ok", "skip", "ok")


def test_skip_while_paused_moves_on_without_dropping_pause():
    control = WorkerControl()
    control.start()
    control.pause()
    control.arm_skip("album:8")

    async def scenario():
        skipped = await control.gate("album:8", sleep=5)
        assert control.is_paused()
        control.resume()
        continued = await control.gate("album:9", sleep=0)
        return skipped, continued

    assert asyncio.run(scenario()) == ("skip", "ok")


def test_stop_unblocks_a_pause():
    control = WorkerControl()
    control.start()
    control.pause()

    async def scenario():
        async def stop_later():
            await asyncio.sleep(0.1)
            control.stop()

        task = asyncio.create_task(stop_later())
        result = await control.gate("album:1", sleep=30)
        await task
        return result

    assert asyncio.run(scenario()) == "stop"
    assert control.is_stopped()


def test_skip_is_ignored_when_stopped():
    control = WorkerControl()
    control.arm_skip("album:1")
    assert control.is_stopped()

    async def scenario():
        return await control.gate("album:1")

    assert asyncio.run(scenario()) == "stop"
