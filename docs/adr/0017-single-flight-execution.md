# 0017 — Single-flight execution: refuse the retry, keep the result

**Status:** Accepted (2026-08-08)

## Context

MCP clients (the Claude desktop and mobile apps among them) give up on a tool
call that doesn't answer within their own fixed window. The agent reads that as
"the call failed" and does the reasonable thing: it sends the same code again.

A Jupyter kernel queues messages on its shell channel, so the second request does
not fail and does not wait for a human decision — it runs, right after the first
one finishes. The user sees a long computation "run several times and go wrong":
a counter incremented twice, a file appended to twice, a `pip install` racing
itself, a training loop restarted on top of its own checkpoint. Nothing in the
server noticed, because from its side both calls were valid.

The per-kernel `asyncio.Lock` in `local_jupyter.exec_local` (and its colab
equivalent) does not help — it *serializes*, which is precisely the behavior that
turns a retry into a second execution. Correlating output is what that lock is
for; admission control is a different question, and nobody was asking it.

Two distinct races produce the duplicate:

1. The retry arrives **while the first execution is still running**.
2. The retry arrives **after it finished** — the client had already stopped
   listening, so the result went nowhere and the kernel looks idle again.

Both need an answer, and neither is answerable unless the agent can find out what
happened to the call it lost. `colab_log` gave colab kernels a partial view; local
kernels had nothing at all.

## Decision

1. **One foreground execution per kernel, enforced by admission, not queueing.**
   `state.run_single_flight` claims the kernel for the duration of an
   `execute_code`/`execute_cell` call. A second call arriving during that window
   returns `status: "busy"` and executes **nothing** — no queueing, no waiting.
   The refusal carries `elapsed_seconds`, the running call's `code_head` and
   `exec_id`, and a `next_action`, because a refusal an agent can't act on just
   becomes another retry.
2. **Say "this is your retry" out loud.** The claim records a SHA-256 of the
   code; a `busy` whose code hashes equal gets `is_retry: true` and a note that
   names the situation ("the SAME code is already running; it did not fail, your
   client stopped waiting"). Agents recover from a diagnosis, not from an error.
3. **A cancelled caller does not release the kernel.** The work is shielded and
   keeps running, so the claim is held and released from a done-callback when the
   execution actually ends. Releasing on cancellation would hand the retry an
   idle-looking kernel — race 2, manufactured by the fix for race 1.
4. **`get_last_execution(kernel_id)` — a new tool.** In-process bookkeeping only,
   no kernel round-trip, so it answers *while* the kernel is busy, which is the
   one moment it's needed. It reports either the running execution or the last
   completed one (status, `code_head`, output text, `execution_count`, timing),
   flagging `client_abandoned` when the caller had already gone. This is the
   local-kernel equivalent of `colab_log`, and it is what makes `busy`
   actionable.
5. **One-shot replay of an abandoned identical execution.** If the previous
   execution was abandoned by its caller, completed within 5 minutes, and the new
   call sends byte-identical code, the recorded result is returned instead of
   re-running (race 2). Restricted to *abandoned* executions because that is
   positive evidence the caller never saw the result, and consumed on first use
   so a deliberate re-run is never silently swallowed.
6. **Not deduplicated by content in general.** Re-running the same cell is normal
   notebook work. Only an in-flight collision or explicit abandonment suppresses
   execution; everything else runs.

The state lives in two process-local dicts in `state.py` and is deliberately
**not** persisted to `kernels.json`: after an mcp restart nothing of ours is
executing, so an empty map is the correct state, and a persisted claim would be a
kernel nobody can ever use again.

## Consequences

- The tool surface goes 31 → 32 (`get_last_execution`), updated in
  `tests/test_tool_surface.py` and `scripts/smoke_test.py` in the same commit as
  the invariant requires. Deployment mode still changes behavior, never the API.
- `status: "busy"` is a new outcome callers must tolerate. The server
  `instructions` and both tool docstrings state the rule — never resend a call
  that seemed to fail — since the agent's behavior is the actual fix; the guard
  only makes the wrong move harmless.
- Background jobs are unaffected past their launch: `execute_code(background=
  True)` holds the slot for the second or two it takes to spawn the detached
  process and then releases it. Long work belongs there precisely because it
  occupies nothing.
- Polling tools that run code on the kernel (`get_job`, `list_variables`) are
  *not* admission-controlled; they would otherwise be refused exactly when the
  busy note tells the agent to poll. They still queue behind a running execution,
  which is why `get_last_execution` touches the kernel not at all.
- A colab session lost mid-execution keeps its recorded `session_lost` result
  (`_forget_colab` deliberately doesn't clear it) — the last thing that ran
  before a VM vanished is worth reading.
- An `execution_id` parameter for true client-supplied idempotency is *not*
  implemented. The abandonment evidence covers the observed failure without
  changing the tool signature; if a client ever supplies stable call ids, that
  becomes the better key.
