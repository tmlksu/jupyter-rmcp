# 0018 — Answer before the client gives up: soft deadline, detach, never interrupt

**Status:** Accepted (2026-08-08)

## Context

ADR 0017 stopped a retry from double-executing. It did not stop the retry from being
*sent*, because the thing that provokes it was still there: for any execution longer
than the client's patience, this server produced **no reply at all**.

The numbers made that the normal case, not an edge case. `EXEC_TIMEOUT_SEC` defaulted
to 120 s; Claude clients abandon a tool call in well under a minute. So the server's
own timeout handling — interrupt the kernel, collect partial output, return
`status: "timeout"` with a helpful note — ran on the far side of a connection nobody
was listening to any more. The agent saw a failed tool call and did the only thing it
could: resend.

Worse, the 120 s cap did active harm on the way. Its job was to bound a runaway cell,
but what it actually killed was every legitimate long execution — a model download, a
training loop, a big `pip install` — which is precisely the work this server exists to
host on a GPU. "Your job was interrupted at 2 minutes" is not a better outcome than
"your job is still running".

Two separate mistakes were tangled together in one number: *how long the caller
waits* and *how long the work may live*. They have nothing to do with each other.

## Decision

1. **Separate the two clocks.**
   - `SOFT_REPLY_DEADLINE_SEC` (default 45 s) bounds only **the reply**. It must sit
     under the MCP client's tool-call timeout.
   - `EXEC_TIMEOUT_SEC` bounds **the work**, by interrupting the kernel — and now
     defaults to `0`, meaning no cap. A caller-supplied `timeout` still interrupts, so
     "kill this if it exceeds N seconds" remains expressible; it is simply no longer
     the default anyone gets by accident.
2. **On the soft deadline, detach — never interrupt.** The execution keeps running,
   the single-flight claim (ADR 0017) stays held, and the caller gets
   `{status: "still_running", exec_id, elapsed_seconds, partial_output}`. Answering
   late is indistinguishable from not answering, so the server always answers.
3. **`get_execution(kernel_id, exec_id=None, wait_seconds=0)` collects it**, and
   replaces the `get_last_execution` introduced in ADR 0017 rather than sitting beside
   it — one tool for "what happened to my execution", whether it is running, finished,
   or finished while nobody was listening. `wait_seconds` long-polls (clamped below the
   reply deadline) so one call replaces twenty. It reads in-process state only, so it
   answers while the kernel is busy.
4. **Live partial output.** `jupyter-kernel-client` accumulates outputs in a local and
   hands them back only at the end, which makes a long execution look dead exactly when
   the agent most needs evidence that it isn't. The execution now writes each output
   into a per-execution sink as it arrives, via `execute_interactive`'s `output_hook`.
   That seam lives one level below the public `execute()`, so the call is wrapped with
   a fallback to the buffered path — degraded progress reporting is acceptable, a
   broken execution path is not.
5. **The bookkeeping happens inside the task, not in a done-callback.** Release and
   record run within the wrapped coroutine, so any observer that sees `task.done()` can
   trust that the claim is gone and the result is recorded. A callback fires a tick
   later, which a long poll would race.
6. **`still_running` is not `client_abandoned`.** A detached execution answered its
   caller and handed over an exec_id, so it is not eligible for ADR 0017's one-shot
   replay; only a truly abandoned (cancelled) call is. The two are tracked separately.
7. **`get_job` gains the same `wait_seconds`** long poll, on a 5 s interval — each poll
   is a real kernel exec, so a tight loop would both waste the kernel and queue behind
   a foreground execution.

## Consequences

- **Upgrade note: `EXEC_TIMEOUT_SEC=120` in an existing `.env` keeps interrupting.** The
  new default only applies where the value is unset. Deployments that want detach-and-
  keep-running must set `EXEC_TIMEOUT_SEC=0` explicitly. `/health` now reports both
  `soft_reply_deadline_sec` and `exec_hard_timeout_sec` so this is checkable from
  outside, and the smoke test times a slow execution against the *deployed* deadline
  rather than a constant it agrees with itself.
- A runaway cell is no longer bounded by a per-execution default. The remaining brakes
  are `interrupt_kernel`, `restart_kernel`, and the reaper's absolute kernel max-age —
  which is the same bargain Colab makes, and the tool docs point at the first two.
- A detached execution holds its kernel for as long as it runs, so every other call on
  that kernel gets `busy`. That is the intended reading: the kernel really is occupied.
  Work that should not occupy it belongs in `execute_code(background=True)`.
- `partial_output` is local-backend only. colab-cli returns its output in one piece at
  the end, so the sink stays empty there and the note says so rather than implying the
  execution is silent.
- The tool surface stays at 32 — `get_execution` supersedes `get_last_execution` within
  the same unreleased change, so nothing published ever carried both names.
