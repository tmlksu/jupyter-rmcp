"""Phase-3 live-colab gate (REQUIRED, incl. a destructive out-of-band kill).

Mirrors PHASE-3 acceptance criterion 4. CPU sessions (no GPU units). Each of the
three loss paths (execute_code / get_job / restart_kernel) is exercised on its
OWN freshly-killed session, because the FIRST loss detection forgets the session
(the state machine's single _mark_lost path) — so a chained second call would hit
the unknown-id route instead of the tracked-dead route we want to prove.

Out-of-band kill: `docker exec jrmcp-mcp colab --auth adc stop -s <name>`.
Exits 0 with `LIVE GATE OK` or 1 on the first failing check.
"""
import asyncio
import subprocess
import sys
import time

from fastmcp import Client

URL = "http://127.0.0.1:7130/mcp"
_checks = 0


def check(name, cond, detail=""):
    global _checks
    if not cond:
        print(f"LIVE GATE FAIL: {name}: {detail}")
        raise SystemExit(1)
    _checks += 1
    print(f"OK  {name}")


def kill_oob(name):
    r = subprocess.run(["docker", "exec", "jrmcp-mcp", "colab", "--auth", "adc",
                        "stop", "-s", name], capture_output=True, text=True, timeout=120)
    print(f"    [out-of-band kill {name}] rc={r.returncode} {r.stdout.strip()[:120]}")


async def new_colab(client, tag):
    r = await client.call_tool("start_kernel", {"backend": "colab"}, timeout=420)
    kid = r.data.get("kernel_id")
    check(f"{tag}: start_kernel colab -> rmcp id", bool(kid) and kid.startswith("rmcp-"), str(r.data))
    return kid


async def main():
    client = Client(URL)
    async with client:
        # ---- session A: standard gate, then kill -> execute_code reports session_lost ----
        a = await new_colab(client, "A")
        r = await client.call_tool("execute_code",
                                   {"kernel_id": a, "code": "print('hello', 6*7)"}, timeout=180)
        check("A: foreground exec ok", r.data.get("status") == "ok", str(r.data)[:200])
        check("A: foreground stdout", "hello 42" in r.data.get("text", ""), str(r.data)[:200])

        r = await client.call_tool("execute_code",
                                   {"kernel_id": a, "code": "import time\nfor i in range(3):\n    print('tick', i, flush=True)\n    time.sleep(1)\n",
                                    "background": True}, timeout=120)
        job = r.data.get("job_id")
        check("A: background job launched", r.data.get("status") == "running_in_background" and bool(job), str(r.data)[:200])
        status, out = None, ""
        for _ in range(30):
            r = await client.call_tool("get_job", {"kernel_id": a, "job_id": job}, timeout=90)
            status = r.data.get("status")
            out = r.data.get("output", "")
            if status != "running_in_background":
                break
            time.sleep(2)
        check("A: background job reached done", status == "done", f"status={status} out={out[:120]}")
        check("A: background job output visible", "tick 2" in out, out[:200])

        kill_oob(a)
        r = await client.call_tool("execute_code",
                                   {"kernel_id": a, "code": "print('should not run')"}, timeout=180)
        check("A: execute_code after kill -> session_lost",
              r.data.get("status") == "session_lost", str(r.data)[:200])

        # ---- session B: kill -> restart_kernel reports session_lost ----
        b = await new_colab(client, "B")
        kill_oob(b)
        r = await client.call_tool("restart_kernel", {"kernel_id": b}, timeout=180)
        check("B: restart_kernel after kill -> session_lost",
              r.data.get("status") == "session_lost", str(r.data)[:200])

        # ---- session C: kill -> get_job reports session_lost ----
        c = await new_colab(client, "C")
        kill_oob(c)
        r = await client.call_tool("get_job", {"kernel_id": c, "job_id": "job-deadbeef"}, timeout=180)
        check("C: get_job after kill -> session_lost",
              r.data.get("status") == "session_lost", str(r.data)[:200])

        # ---- list_kernels drops all three killed sessions (reconcile on demand) ----
        r = await client.call_tool("list_kernels", {}, timeout=90)
        ids = {k.get("kernel_id") for k in r.data}
        check("list_kernels dropped killed sessions",
              not ({a, b, c} & ids), f"still present: {sorted({a, b, c} & ids)}")

    print(f"\nLIVE GATE OK ({_checks} checks)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"LIVE GATE FAIL (exception): {e}")
        sys.exit(1)
