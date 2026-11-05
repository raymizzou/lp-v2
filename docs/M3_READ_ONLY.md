# M3 read-only observer

`lp-v2 run --capital-usdc 100` starts the independent loopback observer and
same-origin dashboard at `http://127.0.0.1:8765/dashboard`. The browser polls
`/summary` every five seconds. Add `?demo=1` for the accepted four-state mock;
that mode makes no requests. Live mode never substitutes demo data.

The immutable startup cap changes only by stopping and re-running the command
(or, after separately approved service setup, editing the nonsecret service
startup cap and restarting this service). There is no config reload, trading
mode, mutation route, or service-changing restart CLI.

The reader scans at most five SDK catalog pages of twenty political markets.
The official `polymarket-client==0.2.0` PublicClient returns typed Gamma markets,
keyset paginators and typed CLOB order books. Book prices are Decimals, and both
condition and token identities, constraints and exchange timestamps must match.
Books older than sixty seconds are excluded. Every discovered excluded market
retains a row/reason; a failed catalog read clears proposals and retains old
last-success timestamps. The bounded scan is not a complete exchange catalog.

The typed Gamma model omits `in_play`. For a successful HTTP read of a JSON
object from the fixed public CLOB market endpoint, the adapter accepts either
`in_play: false` or an absent `in_play` key for this check. Explicit `true`, `null`,
numbers and strings remain excluded. Failed reads, invalid JSON and non-object
responses remain unknown/excluded; they cannot establish an absent key. Internal
unknown market state still prevents proposals, and all other eligibility checks
remain independent. No live endpoint validation is claimed in this batch.
Fee-enabled markets are excluded until their executable maker fee inputs/model
are verified; no zero fee is inferred from absent fee data. Missing region
proof also prevents proposals. These conservative exclusions may yield no
proposals on live data, and are visible rather than hidden.

Decisions run on ten-second boundaries, score snapshots/ranking on ten-minute
boundaries, and wallet plus legacy-order reads on thirty-second boundaries. Safe existing simulated
quote pairs retain their prices for at least sixty seconds before repricing.
Every round rechecks identity, quantity, post-only ask, reward band, fees,
safety cushion and aggregate capital/risk limits; unsafe pairs are withdrawn
immediately instead of being kept alive by that timer. This is only proposal
state, with no actual submissions or occupation. Known scores and precise
source exclusion reasons remain visible for excluded markets and follow the
same ten-minute score cadence. Missing score inputs remain UNKNOWN. A new simulation
ledger receives every Engine.decide round; all returned intents remain local
proposals and are never replayed/submitted. Real lp-v2 occupation is known zero
because this read-only process has no submission capability. Old wallet orders
are observational only and never enter its ledger, capital, or proposals.
`wallet_orders` reports wallet-wide `legacy_order_count`, current successful
`as_of`, and `last_success_at`. All three start UNKNOWN. Only a complete read
(including a complete empty result, count zero) publishes a count and current
time. Failure, malformed data, cyclic pagination or the five-page limit yields
UNKNOWN count/current time while retaining the last successful time. No raw
order or account details are published.

`transport_safety` reports `get_attempts`, `rejected_non_get_attempts`, `status`
(`ok`, `violation`, or UNKNOWN), and `monitor_started_at`. The instance-local
audit wraps final httpx default and mounted transports for direct account,
region and metadata reads and all four PublicClient transports (Gamma, Data,
RFQ and CLOB). GET attempts count dispatches, including failed attempts; every
non-GET is rejected before outbound dispatch. The violation persists even when
a read catches the rejection. This also rejects PublicClient's POST-based
batch-book read; the adapter uses individual GET book reads. Existing `safety`
retains exactly `orders_sent`, `cancels_sent`, and `since`; counts derive from
audited dispatches, with rejected requests excluded. Incompatible or missing
instrumentation disables adapter reads and reports UNKNOWN counts/status rather
than measured zero. Pinned SDK/httpx private wiring is isolated in the audit
module because PublicClient has no transport constructor argument.
`monitor_started_at` is the actual adapter/audit lifetime start; the caller's
`safety.since` is separate run metadata, never evidence of earlier monitoring
or a completed 24-hour observation.

`/summary` reads the latest locked memory snapshot
without contacting the exchange. Observed run/region/wallet identity is published
before waiting for the market catalog, so health checks remain immediate while
initial decisions/scores still show UNKNOWN. Region identity is not proof of
completed market computation. Freshness fields record successful data reads;
unknown and failed reads never manufacture success times or zero balances.

All money is serialized as strings. Missing data is `UNKNOWN`. The dashboard
independently marks snapshot age over 15 seconds, decisions over 60 seconds,
and scores over 1500 seconds, retaining old values. Times use Asia/Shanghai;
“today” starts at Beijing 08:00, corresponding to UTC midnight. M4 income and
inventory columns remain UNKNOWN. Live polling is single-flight with a
ten-second fetch/JSON timeout; late responses cannot overwrite a recovered
snapshot. A separate one-second wall-clock repaint updates freshness while
requests are pending. The HTTP CSP allows only same-origin scripts and summary
connections, inline styling, and no objects, base changes or form actions;
the obsolete offline Mock meta policy is removed.

## Existing L2 credential configuration (future approved setup only)

No keys are provisioned by this change. Reuse the existing exchange API key
later, without deriving or creating credentials. No private key is accepted.
The operator must explicitly pass `--credentials-file /etc/lp-v2/credentials.json`.
There is no discovery of `.env`, inherited API key variables, or other wallets.
The file must be a nonsymlink regular file owned by the process UID, mode 0600;
the directory must be owned by the service user and mode 0700. Its exact fields
are `key`, `secret` (URL-safe base64), `passphrase`, `signer_address` (public),
`wallet_address` (public funding wallet), and integer `signature_type` (0/1/2).
Do not put real values in examples, Git, shell arguments, logs or browser data.
Signer address and funding wallet are deliberately separate; the signer is the
L2 `POLY_ADDRESS`, while only the funding wallet last four characters are shown.

The account transport permits only fixed GET `/balance-allowance` and
`/data/orders`. It never constructs SecureClient, signs orders, derives keys,
or permits the mutation-like GET `/balance-allowance/update`. HMAC uses the
base64-decoded secret and `timestamp + GET + path` (excluding query string),
URL-safe base64 encoded with padding. The installed SDK's BalanceAllowance
model defines balance as integer base units; Polygon USDC has six decimals,
so `182400000` is `182.4` USDC. Malformed/negative balances remain UNKNOWN.

References checked for this implementation: installed SDK
`clients/public.py`, `models/gamma/market.py`, `models/clob/order_book.py`,
`models/clob/account.py`, `clients/secure.py::_make_l2_header_resolver_sync`,
`_internal/hmac.py`, and [official REST authentication documentation](https://docs.polymarket.com/getting-started/api).
No live exchange or credential access was performed by the worker.

## Deployment draft and operator checklist

`deploy/deploy-lp-v2 <40-character-SHA>` and `deploy/lp-v2.service` are drafts.
They have only been exercised against offline fake command boundaries. They
are not authorization to SSH, enable a unit, deploy, run use, or trade.

Before any future approved deployment:

- Provision a dedicated `lp-v2` service user, `/opt/lp-v2/repo` with the exact
  `raymizzou/lp-v2` origin, `/opt/lp-v2/releases`, and independent runtime/deploy
  locks. Confirm loopback port 8765 is free; resolve conflicts in an approved
  plan. Never alter another service or repository.
- Provision `/etc/lp-v2` mode 0700 and credentials.json mode 0600, both owned by
  the service user. Verify ownership without printing contents. Put only the
  approved capital in `/etc/lp-v2/run.env` as `LP_V2_CAPITAL_USDC=100`.
- Review resolved dependencies and the installed SDK version, installation
  output, source SHA, credential boundaries, and host readiness. Existing
  dependency pins/CI trigger policy are unchanged by this draft.
- Export the nonsecret expected capital and wallet tail to the deployment
  shell as `LP_V2_CAPITAL_USDC` and `LP_V2_WALLET_TAIL`. Verify they match the
  approved target. The script requires successful latest trusted push CI on
  main for the exact SHA and fixed CI workflow before any install/switch.
- The script fetches only its dedicated repo, verifies main ancestry, archives
  the exact commit into a new release, installs a venv, records completion,
  atomically switches `current`, restarts only `lp-v2.service`, and checks exact
  SHA/capital/wallet tail/region. Health failure returns nonzero. A partial
  installation is retained for diagnosis and cannot be used as a release.
- CI success and `/healthz` identity do not prove 24-hour acceptance or grant
  deployment authority. Capture startup-to-health elapsed time; the five-minute
  operational target has not been demonstrated on a real host.

Rollback after a failed switch is an explicit operator action within its
separately approved rollback plan: read `/opt/lp-v2/previous-release`, verify
that release's trusted CI, COMPLETE marker, source and runtime, stop only
`lp-v2.service`, restore the previous absolute release symlink atomically with
`os.replace`, then restart only that service. Verify previous SHA, original
capital, wallet tail and region using `/healthz`. Record failure even if rollback
succeeds. Do not blindly redeploy a failed SHA or claim success from restart.

No merge, host deployment, key installation, 24-hour live trial or issue closure
is included in the M3 implementation batch.
