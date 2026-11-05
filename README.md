# lp-v2

Independent Polymarket liquidity-provider service (MVP).

Tracking parent: [open_trader#503](https://github.com/raymizzou/open_trader/issues/503).
M2: [lp-v2#3](https://github.com/raymizzou/lp-v2/issues/3), [PR #9](https://github.com/raymizzou/lp-v2/pull/9), and the [verification report](docs/M2_RESULT.md).
Original cloud-local roadmap reference: `~/ray-docs/任务流/PM-LP-V2-ROADMAP-t713u/PLAN_S10_LP_V2_MVP_t713u.md` (not bundled in this repository).

## What M2 implements

Four pure computations handle [scoring](src/lp_v2/scoring.py), [dual BUY quotes](src/lp_v2/quoting.py), [cost-based hedges](src/lp_v2/hedge.py) and [capital accounting](src/lp_v2/capital.py). They consume explicit data rather than contacting an exchange. The [ledger](src/lp_v2/ledger.py) tracks owned facts, uncertainty and recovery provenance. One [engine](src/lp_v2/engine.py) round returns immutable place/cancel/merge intents; it does not execute them.

[SDKPort](src/lp_v2/sdk_port.py) preserves the M2 execution contract for fakes. M3 adds a separate [read-only adapter](src/lp_v2/readonly_sdk.py) and [runner](src/lp_v2/readonly_runtime.py). The runner uses Engine.decide only for simulation and never submits its intents.

## Install and test

Use Python **3.12+**. From the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

Tests prohibit socket networking, including loopback. HTTP tests exercise the actual parser/handler through memory streams and server boundary fakes. The [report](docs/M2_RESULT.md) records the 730-case suite, financial invariants, independent scoring oracle, five observed CI timings and remaining limits.

## Run the read-only status service

```sh
.venv/bin/lp-v2 run --capital-usdc 100
```

`--capital-usdc` is required, finite and positive. Startup freezes the cap and
UTC timestamp and serves **127.0.0.1:8765**. Stop with Ctrl-C. The independent
read-only runtime reads market data and proposes pairs every ten seconds;
an independent public scanner refreshes ranking after each completed politics scan,
and wallet reads refresh every thirty seconds.
Live execution is a separate explicit opt-in; capital and account identity remain frozen during each run.

- GET `/dashboard` (or `/`): standalone page, polling `/summary` every five seconds.
- GET `/dashboard?demo=1`: explicit offline four-state demo.
- GET `/summary`: immediate schema 1 in-memory snapshot, exact money strings,
  real occupation separated from simulated proposal reserves, UNKNOWN for missing facts.
- GET `/healthz`: code SHA, frozen capital, wallet tail and region identity.
- Non-GET returns 405; unknown paths return 404. No arbitrary file serving.

Account configuration is explicit and optional; missing credentials leave wallet
UNKNOWN. The default read-only path does not load a private key or sign orders. No credential discovery is supported.
Read the [M3 runtime and deployment draft guide](docs/M3_READ_ONLY.md) for
conservative market exclusions, authentication, freshness and future operator
steps. Deployment artifacts are drafts; no live run or 24-hour acceptance is
claimed. Browser visual QA remains separate from offline behavior verification.

## M4 explicit live entry (draft)

Keep the read-only service running while checking a candidate market:

```sh
.venv/bin/lp-v2 preflight --market <condition_id> --capital-usdc 100 --code-sha <40-character-SHA>
```

This public GET-only command prints structured checks and exits 0 only for public PASS
(2 for BLOCKED/UNKNOWN). Private identity, relayer and allowance remain NOT_CHECKED.
PASS is not live authorization or private readiness. Switching and rollback remain
explicit operator actions; see the [M4 guide](docs/M4_LIVE.md). Verified taker-only
fee schedules preserve `feesEnabled=true` while maker plans carry explicit zero costs.
Actual nonzero maker fill fees remain unsupported.

The separate `run --live --market <condition_id>` path accepts one to three approved markets.
A single market is the startup seed for serial rotation; repeating `--market` for two or three
markets selects the frozen M5 multi-market runner. Existing M5 journal histories stay on
the frozen runner even when reduced to one market. Existing rotation state cannot be
opened by the frozen multi-market path. Both paths connect the existing engine
and ledger to the pinned SDK through an append-only SQLite journal. It includes
owned-order drain/stop, restart reconciliation, receipt-gated Proxy merge, and
actual UTC income in the existing dashboard. Read the [M4 operator guide](docs/M4_LIVE.md)
for the explicit signing/L2/relayer file boundaries, Linux process requirements,
accounting definitions, UNKNOWN behavior and remaining operational verification.
The [live unit](deploy/lp-v2-live.service) and [launcher](deploy/run-live) are drafts;
they provision nothing and do not replace the read-only deployment path.
The M5 extension freezes one to three markets in one executor, preserves M4 state and global limits,
and records per-market occupation and income. The read-only `lp-v2 report --state-dir … --day YYYY-MM-DD --out …`
command writes daily CSV/JSON with explicit UNKNOWN reasons and safety evidence; see the [M5 report guide](docs/M5_REPORT.md).
No deployment, live operation, key provisioning or field acceptance is claimed.

## Live-control wrapper

The new `lp-v2 live profile show tokyo` and `lp-v2 live check` consume an existing
v1 default profile. Setup and edits save local expectations; on/off add explicit
confirmation, same-run readback and owned drain around the existing executor.
See the [live-control operator guide](docs/operations/live-control.md) for commands,
installation prerequisites, recovery and concrete required startup proofs.
Actual submitted/confirmed merge remains separately NOT_RUN runtime acceptance.
These offline capabilities do not claim deployment or target readiness.

Do not place trades or deploy until Ray explicitly authorizes each milestone.

【LLM：Codex@Air｜模型=gpt-6-astra｜effort=xhigh｜fast=否】

The live controller binds the installed launcher/unit and actual process arguments
to the checked private-file references and full derived account identity. Its
pre-switch handoff requires an inactive/disabled known legacy service, no alternate
local live executor, zero open orders and exact selected-token inventory proof
(including explicit two-token zero balances for new state). Incomplete evidence
blocks startup; this covers one host and selected market, not distributed wallet
exclusivity. See the [operator guide](docs/operations/live-control.md).

## Scored single-market rotation

Both modes scan politics (tag 2) to the end of the keyset catalog, with 100 rows
per page and sequential public GET requests. Scans run separately from the
10-second decision loop. `/summary.rotation` and the dashboard report coverage,
scan timing, candidate exclusions, current market, waiting reasons and counters.
An incomplete scan is labelled **已扫范围内第一** and cannot initiate rotation.

A complete scan with a strictly higher executable score starts exit-only recovery;
a tie holds. There is no debounce. Recovery cancels owned opening orders promptly,
continues cost-bounded hedges and receipt-gated merges, and requires independent
zero-risk proof before another market can enter. If the current market becomes
ineligible, recovery starts without waiting for a complete scan.

Automatic entry requires the existing confirmed `live on` action and its persisted
window. After expiry, a valid current market continues; a new market cannot sign.
A direct `run --live` without a bound on action grants no automatic-entry window.
See the [rotation operator notes](docs/operations/live-control.md#single-market-rotation)
for retained journals, restart and off behavior. This implementation is offline
verified; deployment, live operation and field acceptance require separate authorization.
