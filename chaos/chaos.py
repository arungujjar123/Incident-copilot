"""
CHAOS TOOL: breaks the shop on purpose and writes down the TRUE root cause.

Every run appends one line to chaos/ground_truth/incidents.jsonl.
That file is the ANSWER KEY for the evaluation (Step 9): we will compare
the agent's diagnosis with it. The agent must never be allowed to read it.

Usage (run from the project root, with the stack running):
    python chaos/chaos.py list
    python chaos/chaos.py run postgres_down                # break for 90 s, then heal
    python chaos/chaos.py run db_table_lock --duration 120
    python chaos/chaos.py run slow_bank_api --no-recover   # leave it broken
    python chaos/chaos.py recover                          # heal everything
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent          # project root (where docker-compose.yml is)
GROUND_TRUTH = ROOT / "chaos" / "ground_truth" / "incidents.jsonl"
PROMETHEUS = os.getenv("PROMETHEUS_URL", "http://localhost:9090")


# ---------------------------------------------------------------- helpers
def compose(*args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run `docker compose <args>` from the project root."""
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, env=env,
                          check=check, capture_output=True, text=True)


def redeploy(service: str, **chaos_env: str) -> None:
    """Recreate one service with (bad) config, like a real deployment would.
    With no chaos_env, it redeploys the normal config. `--no-deps` = don't touch other services.
    If nothing changed, docker compose leaves the container alone."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CHAOS_")}
    env.update(chaos_env)
    compose("up", "-d", "--no-deps", service, env=env)


def set_bank_latency(seconds: float) -> None:
    """Call payments' chaos hook from INSIDE its container (its port is not public)."""
    code = ("import json, urllib.request as u; u.urlopen(u.Request('http://localhost:8002/admin/chaos', "
            f"data=json.dumps(dict(extra_latency_s={seconds})).encode(), "
            "headers={'Content-Type': 'application/json'}))")
    compose("exec", "-T", "payments", "python", "-c", code)


_lock_process: subprocess.Popen | None = None


def start_table_lock() -> None:
    """Open a transaction that locks the orders table and then sleeps, holding the lock.
    We name it 'nightly_report_job' so it looks like a real batch job in pg_stat_activity."""
    global _lock_process
    _lock_process = subprocess.Popen(
        ["docker", "compose", "exec", "-T", "-e", "PGAPPNAME=nightly_report_job", "postgres",
         "psql", "-U", "shop", "-d", "shop", "-c",
         "BEGIN; LOCK TABLE orders IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(3600);"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def stop_table_lock() -> None:
    compose("exec", "-T", "postgres", "psql", "-U", "shop", "-d", "shop", "-c",
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = 'nightly_report_job';", check=False)
    if _lock_process is not None:
        try:
            _lock_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _lock_process.kill()


def prom_value(query: str) -> float:
    url = f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=5) as resp:
        result = json.load(resp)["data"]["result"]
    return float(result[0]["value"][1]) if result else 0.0


def shop_health() -> tuple[bool, float, float]:
    """Healthy = traffic is flowing and almost no 5xx errors at the gateway (last 30 s)."""
    rps = prom_value('sum(rate(http_requests_total{job="gateway"}[30s]))')
    errors = prom_value('sum(rate(http_requests_total{job="gateway", status=~"5.."}[30s]))')
    return rps > 1 and errors < 0.1, rps, errors


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- the faults
@dataclass
class Fault:
    name: str
    root_cause_service: str      # which component is really broken
    root_cause_category: str     # what kind of problem it is
    description: str             # the true story, written like a postmortem summary
    inject: Callable[[], None]
    recover: Callable[[], None]


FAULTS = {f.name: f for f in [
    Fault("postgres_down", "postgres", "dependency_down",
          "The Postgres database container stopped. orders cannot save orders.",
          inject=lambda: compose("stop", "postgres"),
          recover=lambda: compose("start", "postgres")),

    Fault("redis_down", "redis", "dependency_down",
          "The Redis container stopped. payments cannot check or store payment records.",
          inject=lambda: compose("stop", "redis"),
          recover=lambda: compose("start", "redis")),

    Fault("redis_hang", "redis", "dependency_unresponsive",
          "The Redis process froze (it is still 'running' but answers nothing). "
          "payments' Redis calls hang until they time out.",
          inject=lambda: compose("pause", "redis"),
          recover=lambda: compose("unpause", "redis")),

    Fault("payments_down", "payments", "service_down",
          "The payments service stopped. orders cannot reach it.",
          inject=lambda: compose("stop", "payments"),
          recover=lambda: compose("start", "payments")),

    Fault("db_table_lock", "postgres", "lock_contention",
          "A long-running batch job (application_name=nightly_report_job) holds an exclusive lock "
          "on the orders table. INSERTs wait forever, all pooled connections get stuck, "
          "and new requests fail with PoolTimeout.",
          inject=start_table_lock,
          recover=stop_table_lock),

    Fault("bad_config_deploy", "orders", "bad_config",
          "orders was redeployed with a typo in PAYMENTS_URL (http://payment:8002, missing 's'). "
          "orders cannot resolve the payments host.",
          inject=lambda: redeploy("orders", CHAOS_ORDERS_PAYMENTS_URL="http://payment:8002"),
          recover=lambda: redeploy("orders")),

    Fault("gateway_timeout_misconfig", "gateway", "bad_config",
          "gateway was redeployed with DOWNSTREAM_TIMEOUT_S=0.02 (20 ms instead of 3 s). "
          "It gives up on orders before orders can answer; orders itself is healthy.",
          inject=lambda: redeploy("gateway", CHAOS_GATEWAY_TIMEOUT_S="0.02"),
          recover=lambda: redeploy("gateway")),

    Fault("slow_bank_api", "payments", "external_dependency_latency",
          "The external bank API used by payments became slow (+2.5 s per call). "
          "orders gives up on payments after 2 s. Redis and Postgres are fine.",
          inject=lambda: set_bank_latency(2.5),
          recover=lambda: set_bank_latency(0)),
]}


def recover_all() -> None:
    """Undo every fault. Safe to run any time, even if nothing is broken."""
    compose("unpause", "redis", check=False)
    compose("start", "postgres", "redis", "payments", check=False)
    stop_table_lock()
    redeploy("orders")
    redeploy("gateway")
    set_bank_latency(0)


def wait_until_healthy(timeout_s: int = 180) -> float | None:
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            healthy, rps, errors = shop_health()
            print(f"  waiting for recovery... requests/s={rps:.1f} errors/s={errors:.2f}")
            if healthy:
                return round(time.time() - start, 1)
        except Exception as e:  # Prometheus can be briefly unreachable
            print(f"  (prometheus not reachable: {e})")
        time.sleep(5)
    return None


# ---------------------------------------------------------------- commands
def cmd_list(_args) -> None:
    for f in FAULTS.values():
        print(f"{f.name:<27} root cause: {f.root_cause_service:<9} ({f.root_cause_category})")


def cmd_recover(_args) -> None:
    print("Recovering everything...")
    recover_all()
    took = wait_until_healthy()
    print("Healthy again." if took is not None else "WARNING: still unhealthy, check Grafana.")


def cmd_run(args) -> None:
    fault = FAULTS.get(args.fault)
    if fault is None:
        sys.exit(f"Unknown fault '{args.fault}'. Run: python chaos/chaos.py list")

    # 1) Only break a HEALTHY system, otherwise the answer key would be wrong.
    healthy, rps, errors = shop_health()
    if not healthy and not args.force:
        sys.exit(f"Shop is not healthy (requests/s={rps:.1f}, errors/s={errors:.2f}). "
                 "Run 'python chaos/chaos.py recover' first, or use --force.")

    # 2) Break it.
    print(f"Injecting '{fault.name}': {fault.description}")
    injected_at = now_utc()
    fault.inject()
    print(f"Injected at {iso(injected_at)} (UTC). Watch Grafana: http://localhost:3000")

    recovered_at, recovery_s = None, None
    try:
        if args.no_recover:
            print("--no-recover: leaving it broken. Heal later with: python chaos/chaos.py recover")
        else:
            # 3) Keep it broken for a while, so it shows up in metrics, logs and traces.
            for left in range(args.duration, 0, -10):
                print(f"  broken... {left}s left")
                time.sleep(min(10, left))
    except KeyboardInterrupt:
        print("\nCtrl+C: recovering now.")
    finally:
        # 4) Heal it (also runs on Ctrl+C, so we never leave the shop broken by accident).
        if not args.no_recover:
            fault.recover()
            recovered_at = now_utc()
            print(f"Recovered at {iso(recovered_at)} (UTC).")
            recovery_s = wait_until_healthy()

    # 5) Write the answer key.
    record = {
        "incident_id": f"inc-{injected_at:%Y%m%d-%H%M%S}-{fault.name}",
        "fault": fault.name,
        "root_cause_service": fault.root_cause_service,
        "root_cause_category": fault.root_cause_category,
        "root_cause_description": fault.description,
        "injected_at": iso(injected_at),
        "recovered_at": iso(recovered_at) if recovered_at else None,
        "seconds_to_healthy_after_recovery": recovery_s,
    }
    GROUND_TRUTH.parent.mkdir(parents=True, exist_ok=True)
    with GROUND_TRUTH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    print(f"Ground truth saved: {record['incident_id']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Break the shop on purpose and record the true root cause.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show all faults").set_defaults(func=cmd_list)
    sub.add_parser("recover", help="heal everything").set_defaults(func=cmd_recover)
    run = sub.add_parser("run", help="inject one fault")
    run.add_argument("fault")
    run.add_argument("--duration", type=int, default=90, help="seconds to keep it broken (default 90)")
    run.add_argument("--no-recover", action="store_true", help="leave it broken")
    run.add_argument("--force", action="store_true", help="inject even if the shop is unhealthy")
    run.set_defaults(func=cmd_run)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()