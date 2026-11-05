# One-command live control

`lp-v2 live` wraps the single-market executor and its serial rotation controller. Code and offline tests do
not authorize deployment, enabling live mode, trading, or a new run window.
Installation, effective service permissions and field acceptance remain separate.

## Existing default profile

An existing v1 `tokyo` profile and default index load directly:

```sh
lp-v2 live profile show tokyo
lp-v2 live check
```

The local lookup is `$XDG_CONFIG_HOME/lp-v2` or `~/.config/lp-v2`. The index is
`config.json` with `schema_version: 1` and `default_profile: "tokyo"`; profiles are
`profiles/<name>.json`. Directories must be owned 0700 and files owned regular
0600, without symlinks. No setup or JSON editing is needed for an already-prepared
complete v1 profile. `show` is local; `check` reads the selected target anew.
Import timestamps and prepared-profile provenance descriptors are preserved as
data, never instructions or current readiness evidence. The existing trusted-SSH,
release/hash, L2 public-identity, file/service and status-server source labels are
accepted without rewriting the user's profile.

The delivery owner preserves configuration compatibility and backups. After a
separately approved deployment, that owner verifies the exact deployed SHA and
artifacts and updates its pin and provenance. A profile change never deploys code.

## New target or deliberate settings change

For a new target, use an existing trusted SSH alias:

```sh
lp-v2 live setup --profile tokyo --ssh-host EXISTING_ALIAS --set-default
lp-v2 live profile show tokyo
lp-v2 live profile set tokyo --capital-usdc 80
lp-v2 live profile set tokyo --market 0xCONDITION_ID
lp-v2 live profile set tokyo --code-sha EXACT_DEPLOYED_SHA
lp-v2 live profile set-default tokyo
lp-v2 live check --profile tokyo
```

Placeholders in this guide are explanatory and cannot enable a run. Setup can
prompt for the existing alias, capital and market. It imports only explicit
standard deployment locations, not arbitrary credential discovery. An alternate
release root can be supplied as `--release-root`. Unknown or conflicting sources
fail safely. The displayed settings and sources require `save` before local
persistence; this is not authorization to enable live mode. Missing prerequisites
are reported rather than guessed. Drafts cannot become a usable default or start.
A successfully saved incomplete draft reports `status=SAVED_DRAFT`,
`local_saved=true`, name/revision, exact `missing` fields and `can_enable=false`,
with exit2. It does not run a complete target check or save the requested default
selection. Re-enter setup to complete the import; a full save reports `SAVED`
with a separate check result. A saved draft is distinct from a failed save.

The v1 fields are: schema/name/revision; target SSH alias and trusted SHA256 host
key pin; release root and exact SHA; market and decimal capital; full signer,
Proxy wallet and signature type 1; explicit signing/L2/relayer/run.env/state paths;
fixed service user `lp-v2`, units `lp-v2.service` and `lp-v2-live.service`, port
8765; timezone-aware update time and per-field sources. Credentials remain files
on the target. The profile stores references and public identity only.

`profile set` accepts capital, market, code SHA, SSH host, release root, signing
file, L2 file, relayer file and run.env flags. It exposes no arbitrary JSON path,
shell assignment, wallet type, state relocation or strategy settings. Saves show
old/new values, require `save`, keep a protected `.bak`, compare the old revision
under a local lock and atomically replace the profile. Revision conflicts require
re-reading; cancellation/write failures preserve the existing profile and index.
Saving local capital 80 does not change a target still configured for 100. Every
new on rechecks the new revision, identity, occupancy and remote configuration.

## Check, enable and stop

`check` returns JSON with independent checks. Exit 0 means all required facts
passed; exit 2 means failure, unknown evidence, busy, cancellation or an invalid
request. Read the check map, not only a top-level status. A failed check does not
stop the read-only service. Missing/old/malformed public preflight is UNKNOWN.
Each private/config check retains a recognized safe `reason`; public market
subchecks appear under `checks.public_market.checks` with their fixed
`reason_code` (for example fees or book freshness). Unknown external detail is
`CHECK_DETAIL_UNAVAILABLE`; stale/protocol/identity evidence has an explicit safe
reason. Raw error text, arbitrary subcheck keys and private paths are not reports.
Opening eligibility also reports safe `details`: proven `liquidity_num`, the
`minimum_liquidity` of `1000`, executable-book `midpoint` when verified, the
`minimum_midpoint`/`maximum_midpoint` bounds of `0.10`/`0.90`, observation `as_of`
and authored `LIQUIDITY_BELOW_MIN`/`MIDPOINT_OUTSIDE_RANGE` blockers. Unknown
numbers are omitted. Details retain the report's code/market/capital/time binding;
they do not replace independent fee, in-play or account checks.
Book freshness uses the independent local UTC clock at each receipt and again at
joint completion: original book timestamps must be 0–60 seconds old at both
boundaries, including exactly 60 seconds. Books created during the read can pass;
future-at-receipt books, slow-read expiry, missing/invalid times and backward
clocks cannot. The adapter preserves exchange timestamps and adds no tolerance
or automatic retry. Preflight and opening diagnostics report completion time;
decisions and valuations use time sampled after market reads. Decision time must
not precede validated joint completion for the current read; missing or invalid
completion evidence also refuses. Account facts,
fills, receipts, frozen startup identity, 10-second decisions, 30-second wallet reads and completed scanner rounds retain
their own time meaning.
On confirmation, only the validated nested opening diagnostic `as_of` is excluded
from comparing the two fresh reports. Displayed reports retain that timestamp.
Liquidity, midpoint, thresholds, blockers, statuses, reasons, profile/window and
identity changes still refuse; stale/future/missing authoritative observation
times remain invalid. Offline verification does not establish current target or
market readiness.
A successful public CLOB market read must return a JSON object. For the in-play
check, an absent `in_play` key or literal boolean `false` passes; explicit `true`,
`null`, numbers or strings refuse. Failed reads, invalid JSON and non-object
responses remain unknown/refused. Preflight and the SDK use this same rule; other
market and private checks remain independent.
No force/bypass flag exists.

After separate authorization specifying target, wallet, exact code, market,
capital and window, an operator can request:

```sh
lp-v2 live on --window-end 2099-01-01T00:00:00+00:00
# Review the concrete target, revision, preparation and rollback, then type on.
lp-v2 live off
```

The displayed date is a syntax example, not an approved window. `on` requires a
future timezone-aware window end, checks before confirmation, rechecks after
confirmation and rechecks on the target under its mutation lock. It starts once.
The window is an authorization bound checked at execution, not a scheduler.
Use `off` to stop; this command does not install a future automatic shutdown job.

If an authorized preparation explicitly includes changing non-secret run.env
values, use `on --prepare-run-env --window-end ...`. Only MARKET, CAPITAL and SHA
can be changed, exactly to the displayed profile values, with backup and readback.
All other gates must already pass. Other run.env lines, credential files, journal,
restart policy and boot enablement are preserved. Unit installation, missing
secrets or a new release require their own authorized preparation/deployment.

Two fresh snapshots must independently prove the same run, full validated account,
code, capital, unique startup seed market, live process, stopped read-only process and
SUCCESS accounting. Accounting, decision and snapshot times must advance. A
systemctl 0, HTTP 200, mode=live alone or protective pause is insufficient proof
of successful quoting. Both accepted rounds require `eligible=true` and
`skip_reason=null` for the single matching market. A pause, a missing eligibility
field or a skip reason triggers bounded owned rollback and remains nonzero;
a running process does not prove orders were placed or scored.

`off` uses the frozen run record even after market/capital edits. It does not need
opening-market checks. A changed target/root/state is ambiguous and is refused;
do not select a different executor by editing the profile. The original process
record, matching stop-result run ID and a new successful journal drain after the
frozen run boundary must agree. Inventory is retained and reported. Process stop,
drain, port release and read-only restoration are separate facts; restoring the
read-only service never converts a missing/failed drain to success. No sell,
foreign cancel, cancel-all, journal deletion or kill -9 path exists.
Drain also requires complete valid merge history. Missing/malformed history,
UNKNOWN outcomes or acknowledgement without settlement keep drain and the
overall result UNKNOWN, even if readonly is restored. `merge_history` reports the
uncertainty separately and retains inventory. Empty complete history or known
terminal results do not replace the other drain proofs; no merge is replayed.

## Loss, concurrency and recovery

The confirmation display includes an action ID. Local pending operation receipts
and target action results are stored before mutation, owner-only. To inspect a
lost response, repeat the same on `--action-id` or off `--action-id`; the wrapper
queries the recorded result instead of submitting start/stop again. Off receipts
must bind operation=off, the exact frozen profile and run ID locally and on the
target. An old run's action ID, on receipt, malformed legacy receipt or changed
profile cannot return a new run's success; mismatches refuse without mutation.
UNKNOWN is
not permission to choose a new ID and retry. A target control lock gives one
mutation owner across prepare/on/off; a competing operation reports BUSY.
The executor retains its existing process lock and append-only journal.

Local run records live under `runs/`; operation receipts under `operations/`.
Target records live under the explicit state directory: `live-control-run.json`,
`control.lock`, `control-actions/`. Keep these with the existing journal, WAL,
process lock and stop result. Never delete state to unblock a run. Stdout is JSON;
a protected redirect (`umask 077`) can retain the sanitized report for task-flow
handoff. Interactive commands emit a proposed-target JSON line before the final
result. Raw SSH/HTTP errors, auth headers and relayer history are never reports.

## Installation and proof limits

Both caller and target need this release. The target also needs the #14 public
`preflight` capability. A complete old release without that capability fails
closed. SSH uses existing ED25519 trust, verifies the profile pin before a target
command, and isolates that exact already-trusted key for the connection; it never
accepts a new key. The operator must already have noninteractive sudo authority.
A literal system-Python bootstrap verifies the exact release selector, COMPLETE,
interpreter and release tree are immutable and root-owned before importing the
privileged release code. It refuses application-owned or writable executable
paths; it does not change ownership or install authority. The bootstrap and
helper require the existing system interpreter and the installed release Python.

The short-lived Linux helper drops real/effective UID and GID to the fixed lp-v2
account, clears supplementary groups and performs application reads/writes with
that identity. Only allowlisted systemctl show/start/stop calls for the two fixed
units temporarily regain the saved operator privilege, with a fixed environment.
A separate fixed read-only show of `open-trader-prediction.service` stays
unprivileged; the legacy unit has no privileged action route.
Process identity is checked under the service UID, and a foreign/mismatched
executor is refused before stopping a unit. Private files remain 0600 and state
0700 owned by lp-v2. A missing sudo/service identity or invalid path blocks before
unit/env mutation. No service-account sudo/polkit grant, new policy, daemon,
chmod/chown or capability installation is performed. Each helper invocation
handles one request; no privileged background process remains.

Check verifies installed source against the pinned repository object and release,
file permissions, environment, journal identity, effective services and port
ownership. Fixed Git reads permit an exact per-command `safe.directory` only
after proving the repository, its control tree and ancestors immutable and
root-owned, with the expected origin and pinned commit. External config includes,
alternate object stores and symlink escapes refuse proof. No Git configuration is
written. When systemctl omits one of the five optional Exec properties, only a
successful unprivileged typed empty-array D-Bus read for that fixed unit/property
proves absence; missing, ambiguous or nonempty evidence still refuses readiness.
The readonly listener may be the verified direct executor or the direct child of
the shipped flock wrapper. Both process identities, service UID, fixed cgroup
and exact arguments must remain stable; extra or unrelated listeners fail proof.
These observations never stop the readonly service. Existing draft units have an on-failure restart policy; a readiness
check requiring no automatic retry does not silently change them. An effective
approved service configuration must already meet the contract. The effective
User/Group, exact single ExecStart/ExecStop, working directory, environment file,
stop timeout and safety settings must agree with the supported shipped launcher.
Its installed bytes must match the pinned Git artifact and the recognized
launcher. Extra commands, arguments, environment inputs or unsupported overrides
refuse startup. Literal credential/state references must resolve to the profile;
market/capital/SHA must match before switching. A valid saved profile can therefore
still fail against an installed service that uses different files.

After start, protected MainPID identity and actual `/proc` arguments must match the
checked invocation. Full signer/Proxy/L2 identity is derived from the actual file
references. Pre/post file owner/mode/inode metadata and derived public identity
must remain stable; raw secrets and hashes of secret material are not reported.
Full Ethereum signer/Proxy addresses compare by their complete hex identity across
valid case presentations. Saved profile bytes, revisions, confirmations and action
receipts remain exact; equivalent account spelling does not authorize profile drift.
This is a trusted single-host operator proof, not attestation against adversarial
same-UID file replacement or a compromised root account.

Handoff also requires the known legacy service inactive and disabled, the fixed
live unit inactive, readonly active, the configured lock free, and a bounded local
process scan finding no alternate `lp-v2 run --live`, including alternate state
paths. Incomplete inspection, including a hidden-PID proc mount, is UNKNOWN. The scan reads command lines, never
process environments, and sends no signals. It does not claim distributed wallet
exclusivity or inventory knowledge in unrelated markets.

The two selected tokens come from verified fixed-market metadata. Authenticated
GET `/balance-allowance` with CONDITIONAL/token_id/signature_type=1 reads each
quantity in integer base units (divided by 1,000,000). Fresh exact condition and
binary Yes/No token identity authorize this observational GET independently of
opening eligibility. An in-play or liquidity refusal still blocks `on`; it does
not turn an unread balance into zero. `CONDITIONAL_IDENTITY_UNAVAILABLE` separates
metadata/account identity failures from `CONDITIONAL_BALANCE_UNAVAILABLE` for
actual balance HTTP/parsing failures. A new state needs both
quantities explicitly zero; an empty directory cannot establish that. Existing
history needs a matching latest stopped process/control/result, full owned
wallet/market/token identity, successful complete reconciliation, terminal known
orders/merges and complete finite nonnegative Position quantities/costs exactly
matching both fresh balances. Missing, foreign, interrupted or unmatched history
is UNKNOWN. Checks read journal snapshots without constructing a writing Journal,
replaying a ledger, adopting inventory or changing allowances. Unsettled WAL
history also remains UNKNOWN. Orphan WAL/SHM/rollback sidecars, including empty
or dangling artifacts without the main database, cannot be treated as new state.
A nonempty rollback journal alongside the database also blocks immutable reads;
check never recovers, checkpoints, deletes or replaces such history. All
handoff/account/executor facts are read again inside the target control lock before stopping readonly. Explicit environment
preparation also repeats the handoff proofs under that lock before writing run.env.

Authenticated L2 GETs prove balance, explicit spender allowances, closed-only
state and order observations independently. Authenticated relayer `/transactions`
GET proves key authentication only; public `/deployed` cannot substitute for it.
The protected relayer file accepts exactly legacy `{key,address}` or Builder
`{kind,key,secret,passphrase,address}` with `kind="builder"`. Both bind the complete
valid signer address, ignoring hex case. Builder strings must be nonblank, contain
no CR/LF/NUL, and the secret must decode nonempty standard or URL-safe base64
(optional padding). Unknown, missing, extra or mixed fields refuse safely.
The same parser constructs the pinned SDK key for the live factory and validates
protected invocation snapshots; snapshots retain metadata and public identity only.
Builder check signs exactly `GET /transactions` with no body using SDK 0.2.0
`POLY_BUILDER_*` headers; legacy retains `RELAYER_API_KEY` and
`RELAYER_API_KEY_ADDRESS`. A 401/403 is FAIL without fallback; server errors,
malformed rows or any foreign owner remain UNKNOWN. Neither headers nor transaction
history appear in reports. Profile v1 still contains only file references;
credential migration and state recovery are separate operator work.
Required pre-start Proxy configuration is a separate concrete fact: signature
type 1, complete signer/derived Proxy identity, the pinned SDK and supported
production chain/relayer/wallet-derivation configuration must agree. This proof
and authenticated GET are both required, along with every fresh public, account,
allowance, release, state, service and operator-authority check. Missing or UNKNOWN
required proof still refuses on; there is no bypass flag.

An actual submitted/confirmed merge belongs to runtime acceptance in issue #5.
CLI check/on/off report `runtime_acceptance.merge.status=NOT_RUN`; account evidence
retains actual merge UNKNOWN. Neither becomes PASS from a supported configuration,
authentication or successful startup. No order, merge, approval or wallet-creation
request is sent to probe readiness. Offline combined-route tests run the actual
controller, serialization, dispatcher, public preflight, SDK parser/auth and
process-lock logic with external OS/systemd/HTTP/clock facts replaced. They are
not evidence of any actual account or installed target's readiness.

## Approved SOP block mapping

This maps the operation/check blocks recorded in issue #15. The delivery owner
must also compare the current private operator SOP before field acceptance.

| Operator block | Automatic evidence and pass condition | Failure/recovery |
| --- | --- | --- |
| Target/version | Existing SSH trust + exact pin; current release/COMPLETE/source/runtime match SHA | Refuse; no release switch |
| Files/wallet | Owned private files; derived signer + complete Proxy/L2 identity | Independent FAIL/UNKNOWN; no signing client |
| Config/state | Exact market/capital/SHA; original 0700 state/journal identity | Refuse drift; preserve all state |
| Executor/handoff | Inactive/disabled legacy service, no alternate local live executor, readonly active, fixed lock/unit/port, zero authenticated orders; both verified token balances match a complete owned stopped snapshot (or explicit new-state zeros) | Busy/conflict/incomplete or unmatched inventory blocks new start |
| Public market | #14 scope/identity/time and every required public check | Missing capability UNKNOWN; no duplicate fee policy |
| Private account | Balance, explicit allowances, closed-only, region, relayer auth and concrete supported Proxy configuration | Missing required evidence UNKNOWN blocks; runtime merge remains NOT_RUN |
| Service/preparation | Existing operator authority, immutable release, effective unit paths, user, env, conflicts, restart/boot policy | Service-owned app state; finite privileged unit actions; no permission install |
| Switch | Same revision/facts/window before and after confirmation, target mutation lock | No unconfirmed mutation; action receipt before start |
| Live readback | Actual protected PID arguments and full derived account identity; two fresh advancing same-run snapshots with accounting SUCCESS | Bounded observation; owned drain/read-only rollback |
| Stop/drain | Existing ExecStop; matching process result + fresh successful journal drain | Preserve failed/old/missing results and residuals |
| Restore | Executor lock released and port free, then read-only identity/mode readback | Restore separate from drain; overall remains nonzero on uncertainty |
| Evidence | Whitelisted JSON statuses and local protected operation/run records | No raw credentials, response bodies or transaction history |

No target SSH/account check, deployment, real on/off, trade, merge, inventory sale
or field acceptance is claimed by this implementation's offline verification.

## Single-market rotation

`trading.market_condition_id` and `LP_V2_MARKET` name the startup seed. Initial
`verify_start` still requires two fresh eligible rounds for that seed. The host records those two independently verified seed rounds in the bound
on/run record. The scanner may finish earlier, but automatic rotation stays
locked until that durable same-action/same-run proof exists. Subsequent score
changes have no cooldown or debounce. Repeating an already verified on action
returns that action's result, including after its active market has rotated.
A new confirmed on can return to the seed only after all retained owned markets
prove flat; restarting the same on restores its last recorded phase instead.
That recovery retains the logical on/run identity only when the prior physical
lease, action, frozen profile and durable rotation state agree. The replacement
executor takes a fresh exclusive lease and records its current PID/start identity;
the original verified seed proof remains bound to that same logical on. Stop
results belong to the current physical lease, so an older process's successful
drain cannot satisfy a stop after restart. A new explicit on gets a new run.

The politics scanner uses keyset pages of 100 until the terminal cursor and
issues one request at a time. Its public client and transport audit are independent
of the account/decision client. Decisions read copies of the last completed
result, so catalog latency cannot block current-market recovery. Coverage includes
pages, rows, requests, maximum in-flight requests, prescreen count, start/finish,
directory/market durations and redacted errors. Ranking books are labelled with
their observation time; entry rereads books and all named public/account checks.
A partial round may be displayed but cannot initiate active rotation.

Only a strictly higher executable score from a complete round initiates exit.
If the current market leads again during exit, opening resumes in that same
market. Ineligibility starts exit on the next decision round even with an expired
window. Exit stops opening, cancels owned opening orders immediately, and keeps
covered BUY hedges and merges. Dust, unaffordable hedges, UNKNOWN requests/cancels,
unsettled merges or inconsistent balances keep the market in recovery; there is
no sale, taker chase or forced liquidation.

Before entry, old-market owned orders, pending requests and uncertainty must be
absent, complete merge history terminal, and both conditional balances zero and
consistent with the owned ledger. Clearance is checked again after drain: a late
fill returns to recovery instead of allowing another market. Target inventory
must independently prove flat, including retained journal history. A rejected
candidate is skipped before activation; once a candidate owns execution, failures
remain its recovery responsibility.

The confirmed action id/window are read from the bound host run record. Selection
checks the window, and the actual first-signature boundary checks it again.
Valid current execution can continue after expiry, but no new market can enter;
invalid current execution exits to flat and displays `needs_new_on`. A standalone
live command without a bound control action invents no automatic-entry window.

State consists of append-only `rotation.sqlite` phase events and one immutable
identity per `markets/<condition_id>/journal.sqlite`. Historical journals are
retained on revisits. A verified legacy root journal is copied into its market
journal without changing or deleting the root; a mismatch fails closed. Invalid
market paths, symlinks, short-prefix collisions and historical request-id collisions
are refused. Order and merge ids include the market prefix. Restart restores
running/exiting/cleared/revalidating phases without resubmitting old intents.

Host checks and stopped inventory handoff resolve the active journal from the
original seed profile and retain full wallet, token, process/run, sequence and
path proof. Sequence numbers are scoped to their own market database. `off` stops
the recorded process and drains every retained owned market, preserving foreign
manual orders. This remains a single-host guard, not distributed wallet exclusivity.
