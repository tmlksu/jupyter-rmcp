"""Single-flight execution guard (ADR 0017): one foreground exec per kernel.

The bug being pinned: a Jupyter kernel QUEUES shell messages, so a client that
gives up on a slow call and resends the same code gets it executed twice. These
tests assert the second call runs NOTHING, that the claim is released down every
exit path (ok / error / timeout / session_lost / caller cancelled), and that the
abandoned call's result stays recoverable.

`backends._run_on_kernel` is the single execution choke point, so stubbing it is
enough to drive both execute_code and execute_cell without a kernel.
"""
import asyncio

import pytest

import config
import jobs
import notebook
import state


@pytest.fixture(autouse=True)
def clean_guard(monkeypatch):
    """Empty in-flight/last-exec maps per test, and never spawn the reaper."""
    import reaper
    monkeypatch.setattr(reaper, "_reaper_started", True)
    monkeypatch.setattr(state, "_inflight", {})
    monkeypatch.setattr(state, "_last_exec", {})
    yield


@pytest.fixture()
def no_notebook(monkeypatch):
    """Kernels are unbound, so nothing tries to write a notebook back."""
    async def _none(_kernel_id):
        return None
    monkeypatch.setattr(jobs.local_jupyter, "_notebook_for_kernel", _none)


class _Kernel:
    """A stand-in kernel whose exec blocks until the test releases it."""

    def __init__(self, reply=None):
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.calls: list[str] = []
        self.reply = reply or {"status": "ok", "execution_count": 1, "outputs": [], "timed_out": False}

    async def run(self, _kernel_id, code, _timeout=None, sink=None):
        self.calls.append(code)
        if sink is not None:
            sink.append({"output_type": "stream", "name": "stdout", "text": "progress...\n"})
        self.started.set()
        await self.gate.wait()
        return self.reply


def _install(monkeypatch, kernel):
    monkeypatch.setattr(jobs.backends, "_run_on_kernel", kernel.run)
    return kernel


CODE = "x = 1\nprint(x)\n"


class TestBusyRefusal:
    async def test_second_call_executes_nothing(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        first = asyncio.ensure_future(jobs.execute_code("kid-1", CODE))
        await k.started.wait()  # the first call now owns the kernel

        busy = await jobs.execute_code("kid-1", "y = 2\n")

        assert busy["status"] == "busy"
        assert k.calls == [CODE]        # the second code never reached the kernel
        assert busy["is_retry"] is False
        k.gate.set()
        assert (await first)["status"] == "ok"

    async def test_busy_reports_elapsed_and_code_head(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        first = asyncio.ensure_future(jobs.execute_code("kid-2", CODE))
        await k.started.wait()

        busy = await jobs.execute_code("kid-2", "other()\n")

        assert busy["running"]["code_head"] == CODE
        assert busy["running"]["elapsed_seconds"] >= 0
        assert busy["running"]["exec_id"].startswith("exec-")
        assert "get_execution" in busy["next_action"]
        assert "interrupt_kernel" in busy["next_action"]
        k.gate.set()
        await first

    async def test_identical_code_is_flagged_as_a_retry(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        first = asyncio.ensure_future(jobs.execute_code("kid-3", CODE))
        await k.started.wait()

        busy = await jobs.execute_code("kid-3", CODE)

        assert busy["is_retry"] is True
        assert "SAME code" in busy["note"]
        assert k.calls == [CODE]
        k.gate.set()
        await first

    async def test_other_kernels_are_unaffected(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        first = asyncio.ensure_future(jobs.execute_code("kid-a", CODE))
        await k.started.wait()
        k.gate.set()

        second = await jobs.execute_code("kid-b", CODE)

        assert second["status"] == "ok"
        await first

    async def test_execute_cell_shares_the_slot(self, monkeypatch, no_notebook, nb_stub):
        k = _install(monkeypatch, _Kernel())
        first = asyncio.ensure_future(jobs.execute_code("kid-4", CODE))
        await k.started.wait()

        busy = await notebook.execute_cell("kid-4", cell_id="c1")

        assert busy["status"] == "busy"
        assert k.calls == [CODE]
        k.gate.set()
        await first

    async def test_execute_code_is_refused_while_a_cell_runs(self, monkeypatch, no_notebook, nb_stub):
        k = _install(monkeypatch, _Kernel())
        monkeypatch.setattr(notebook.backends, "_run_on_kernel", k.run)
        first = asyncio.ensure_future(notebook.execute_cell("kid-5", cell_id="c1"))
        await k.started.wait()

        busy = await jobs.execute_code("kid-5", CODE)

        assert busy["status"] == "busy"
        assert busy["running"]["tool"] == "execute_cell"
        k.gate.set()
        await first


class TestRelease:
    """Every exit path must free the kernel — a stuck claim is worse than the bug."""

    async def test_released_after_success(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        k.gate.set()
        await jobs.execute_code("kid-ok", CODE)
        assert state.running_exec("kid-ok") is None

    @pytest.mark.parametrize("reply", [
        {"status": "timeout", "outputs": [], "timed_out": True},
        {"status": "session_lost", "outputs": [], "timed_out": False, "note": "gone"},
    ])
    async def test_released_after_timeout_and_session_loss(self, monkeypatch, no_notebook, reply):
        k = _install(monkeypatch, _Kernel(reply=reply))
        k.gate.set()
        await jobs.execute_code("kid-x", CODE)
        assert state.running_exec("kid-x") is None

    async def test_released_after_an_exception(self, monkeypatch, no_notebook):
        async def _boom(*_a, **_kw):
            raise RuntimeError("kernel 'kid-e' not found on any backend")
        monkeypatch.setattr(jobs.backends, "_run_on_kernel", _boom)

        with pytest.raises(RuntimeError, match="not found"):
            await jobs.execute_code("kid-e", CODE)

        assert state.running_exec("kid-e") is None
        # and the failure is still readable rather than silently dropped
        assert state._last_exec["kid-e"]["status"] == "error"

    async def test_a_second_call_runs_once_the_first_finishes(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        first = asyncio.ensure_future(jobs.execute_code("kid-seq", CODE))
        await k.started.wait()
        k.gate.set()
        await first

        second = await jobs.execute_code("kid-seq", "z = 3\n")

        assert second["status"] == "ok"
        assert k.calls == [CODE, "z = 3\n"]


class TestAbandonedCaller:
    """A client that gives up mid-call must not hand its retry an idle-looking kernel."""

    async def test_claim_is_held_until_the_work_really_ends(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        first = asyncio.ensure_future(jobs.execute_code("kid-c", CODE))
        await k.started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        busy = await jobs.execute_code("kid-c", CODE)   # the retry arrives

        assert busy["status"] == "busy"
        assert busy["is_retry"] is True
        assert k.calls == [CODE]
        k.gate.set()
        await asyncio.sleep(0.01)   # let the abandoned work unwind before the test ends
        assert state.running_exec("kid-c") is None

    async def test_result_is_recorded_when_the_work_finishes(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        first = asyncio.ensure_future(jobs.execute_code("kid-d", CODE))
        await k.started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        k.gate.set()
        await asyncio.sleep(0.01)

        assert state.running_exec("kid-d") is None
        rec = await jobs.get_execution("kid-d")
        assert rec["status"] == "ok"
        assert rec["client_abandoned"] is True

    async def test_identical_retry_replays_instead_of_re_running(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        first = asyncio.ensure_future(jobs.execute_code("kid-r", CODE))
        await k.started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        k.gate.set()
        await asyncio.sleep(0.01)

        replayed = await jobs.execute_code("kid-r", CODE)

        assert replayed["replayed"] is True
        assert replayed["status"] == "ok"
        assert k.calls == [CODE]            # not run a second time

    async def test_replay_is_one_shot(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        first = asyncio.ensure_future(jobs.execute_code("kid-r2", CODE))
        await k.started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        k.gate.set()
        await asyncio.sleep(0.01)
        await jobs.execute_code("kid-r2", CODE)          # consumes the replay

        again = await jobs.execute_code("kid-r2", CODE)  # a deliberate re-run really runs

        assert again.get("replayed") is None
        assert k.calls == [CODE, CODE]

    async def test_different_code_is_never_replayed(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        first = asyncio.ensure_future(jobs.execute_code("kid-r3", CODE))
        await k.started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        k.gate.set()
        await asyncio.sleep(0.01)

        other = await jobs.execute_code("kid-r3", "print('different')\n")

        assert other.get("replayed") is None
        assert k.calls == [CODE, "print('different')\n"]

    async def test_a_completed_call_is_not_replayable(self, monkeypatch, no_notebook):
        """Only ABANDONED results replay: a normal re-run must always re-run."""
        k = _install(monkeypatch, _Kernel())
        k.gate.set()
        await jobs.execute_code("kid-n", CODE)

        again = await jobs.execute_code("kid-n", CODE)

        assert again.get("replayed") is None
        assert k.calls == [CODE, CODE]


class TestSoftDeadline:
    """The server must answer before the client stops listening — always (ADR 0018)."""

    @pytest.fixture(autouse=True)
    def tiny_deadline(self, monkeypatch):
        monkeypatch.setattr(config, "SOFT_REPLY_DEADLINE_SEC", 0.05)

    async def test_slow_execution_answers_with_still_running(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())

        r = await jobs.execute_code("kid-sd", CODE)

        assert r["status"] == "still_running"
        assert r["exec_id"].startswith("exec-")
        assert "not a failure" in r["note"].lower()
        assert k.calls == [CODE]      # started, and never interrupted
        k.gate.set()
        await asyncio.sleep(0.01)

    async def test_still_running_carries_live_partial_output(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())

        r = await jobs.execute_code("kid-sd2", CODE)

        assert "progress..." in r["partial_output"]
        k.gate.set()
        await asyncio.sleep(0.01)

    async def test_detached_execution_still_holds_the_kernel(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        await jobs.execute_code("kid-sd3", CODE)

        second = await jobs.execute_code("kid-sd3", CODE)

        assert second["status"] == "busy"
        assert second["running"]["detached"] is True
        assert k.calls == [CODE]
        k.gate.set()
        await asyncio.sleep(0.01)

    async def test_result_is_collectable_once_it_finishes(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel(reply={
            "status": "ok", "execution_count": 3, "timed_out": False,
            "outputs": [{"output_type": "stream", "name": "stdout", "text": "done late\n"}]}))
        started = await jobs.execute_code("kid-sd4", CODE)
        k.gate.set()

        r = await jobs.get_execution("kid-sd4", exec_id=started["exec_id"], wait_seconds=5)

        assert r["status"] == "ok"
        assert "done late" in r["text"]
        assert r["detached"] is True
        assert r["client_abandoned"] is False   # we DID answer, so no blind-retry replay

    async def test_long_poll_returns_as_soon_as_it_finishes(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        await jobs.execute_code("kid-sd5", CODE)

        async def _release_soon():
            await asyncio.sleep(0.05)
            k.gate.set()
        asyncio.ensure_future(_release_soon())

        r = await jobs.get_execution("kid-sd5", wait_seconds=5)

        assert r["status"] == "ok"

    async def test_long_poll_gives_up_without_killing_the_work(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        await jobs.execute_code("kid-sd6", CODE)

        r = await jobs.get_execution("kid-sd6", wait_seconds=0.05)

        assert r["status"] == "running"
        assert "progress..." in r["running"]["partial_output"]
        assert state.running_exec("kid-sd6") is not None   # the poller did not cancel it
        k.gate.set()
        await asyncio.sleep(0.01)

    async def test_unknown_exec_id_is_not_confused_with_the_latest(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        k.gate.set()
        await jobs.execute_code("kid-sd7", CODE)

        r = await jobs.get_execution("kid-sd7", exec_id="exec-deadbeef")

        assert r["status"] == "unknown"


class TestGetJobLongPoll:
    async def test_waits_for_the_job_to_leave_the_running_state(self, monkeypatch):
        monkeypatch.setattr(jobs, "_JOB_POLL_INTERVAL_SEC", 0.01)
        states = ["running_in_background", "running_in_background", "done"]

        async def _poll(_kid, job_id, _offset, _max_chars):
            return {"kernel_id": "k", "job_id": job_id, "status": states.pop(0), "output": ""}
        monkeypatch.setattr(jobs, "_poll_job", _poll)

        r = await jobs.get_job("k", "job-1", wait_seconds=5)

        assert r["status"] == "done"
        assert states == []

    async def test_returns_immediately_without_wait_seconds(self, monkeypatch):
        polls = []

        async def _poll(_kid, job_id, _offset, _max_chars):
            polls.append(job_id)
            return {"kernel_id": "k", "job_id": job_id, "status": "running_in_background"}
        monkeypatch.setattr(jobs, "_poll_job", _poll)

        r = await jobs.get_job("k", "job-2")

        assert r["status"] == "running_in_background"
        assert len(polls) == 1


class TestGetLastExecution:
    async def test_reports_nothing_for_an_unknown_kernel(self):
        r = await jobs.get_execution("kid-unknown")
        assert r["status"] == "none"

    async def test_reports_a_running_execution_without_touching_the_kernel(
            self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel())
        first = asyncio.ensure_future(jobs.execute_code("kid-run", CODE))
        await k.started.wait()

        r = await jobs.get_execution("kid-run")

        assert r["status"] == "running"
        assert r["running"]["code_head"] == CODE
        assert k.calls == [CODE]     # the poll itself ran nothing
        k.gate.set()
        await first

    async def test_returns_the_last_completed_result(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel(reply={
            "status": "ok", "execution_count": 7, "timed_out": False,
            "outputs": [{"output_type": "stream", "name": "stdout", "text": "hello\n"}]}))
        k.gate.set()
        await jobs.execute_code("kid-last", CODE)

        r = await jobs.get_execution("kid-last")

        assert r["status"] == "ok"
        assert r["execution_count"] == 7
        assert "hello" in r["text"]
        assert r["code_head"] == CODE
        assert r["client_abandoned"] is False
        assert "code_sha" not in r      # internal bookkeeping stays internal

    async def test_output_is_windowed_to_max_chars(self, monkeypatch, no_notebook):
        k = _install(monkeypatch, _Kernel(reply={
            "status": "ok", "execution_count": 1, "timed_out": False,
            "outputs": [{"output_type": "stream", "name": "stdout", "text": "z" * 5000}]}))
        k.gate.set()
        await jobs.execute_code("kid-big", CODE)

        r = await jobs.get_execution("kid-big", max_chars=500)

        assert len(r["text"]) == 500
        assert r["text_truncated"] is True


@pytest.fixture()
def nb_stub(monkeypatch):
    """A one-code-cell notebook bound to every kernel, served from memory."""
    nb = {"cells": [{"cell_type": "code", "id": "c1", "source": "print('cell')\n",
                     "outputs": [], "execution_count": None}],
          "metadata": {}, "nbformat": 4, "nbformat_minor": 5}

    async def _nb_for(_kernel_id):
        return "work.ipynb"

    async def _read(_path):
        return nb

    async def _write(_path, _nb):
        return None

    monkeypatch.setattr(notebook.local_jupyter, "_notebook_for_kernel", _nb_for)
    monkeypatch.setattr(notebook.local_jupyter, "_read_nb", _read)
    monkeypatch.setattr(notebook.local_jupyter, "_write_nb", _write)
    return nb
