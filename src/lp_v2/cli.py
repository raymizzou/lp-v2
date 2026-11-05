"""Command-line entry point."""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from lp_v2.status_server import serve


@dataclass(frozen=True, slots=True)
class RunConfig:
    capital_usdc: Decimal
    started_at: datetime
    code_sha: str = "UNKNOWN"
    credentials: object = field(default=None, repr=False, compare=False)
    live: bool = False
    market: str | None = None
    private_key_file: str | None = field(default=None,repr=False)
    state_dir: str | None = None
    relayer_credentials_file: str | None = field(default=None,repr=False)
    markets: tuple[str, ...] = ()


@dataclass(frozen=True,slots=True)
class ReportConfig:
    state_dir: str
    day: str
    out: str
    credentials: object = field(default=None,repr=False)


@dataclass(frozen=True, slots=True)
class PreflightConfig:
    capital_usdc: Decimal
    started_at: datetime
    market: str
    code_sha: str


@dataclass(frozen=True,slots=True)
class StopConfig:
    state_dir: str


def _positive_capital(value: str) -> Decimal:
    try:
        capital = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("must be a finite positive number") from None
    if not capital.is_finite() or capital <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return capital


def parse_run(argv: Sequence[str] | None) -> RunConfig | StopConfig | ReportConfig | PreflightConfig:
    parser = argparse.ArgumentParser(prog="lp-v2")
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--market", required=True)
    preflight.add_argument("--capital-usdc", required=True, type=_positive_capital)
    preflight.add_argument("--code-sha", required=True)
    stop = commands.add_parser("stop")
    stop.add_argument("--state-dir",required=True)
    report = commands.add_parser('report',description='Read-only UTC daily CSV + JSON outside state; missing facts and unset safety thresholds remain UNKNOWN. See docs/M5_REPORT.md for provenance.')
    report.add_argument('--state-dir',required=True)
    report.add_argument('--day',required=True,help='UTC YYYY-MM-DD')
    report.add_argument('--out',required=True,help='Output prefix outside state; writes .csv and .json')
    report.add_argument('--credentials-file',help='Explicit owner-only existing L2 credentials; GET only; never a signing key')
    run = commands.add_parser("run")
    run.add_argument("--capital-usdc", required=True, type=_positive_capital)
    run.add_argument('--live',action='store_true')
    run.add_argument('--market', action='append')
    run.add_argument('--private-key-file')
    run.add_argument('--state-dir')
    run.add_argument('--relayer-credentials-file')
    run.add_argument('--code-sha', default='UNKNOWN')
    run.add_argument('--credentials-file', help='Explicit owner-only JSON with existing L2 credentials; never auto-discovered')
    args = parser.parse_args(argv)
    if args.command == "stop":
        return StopConfig(args.state_dir)
    if args.command == 'report':
        from datetime import date
        from lp_v2.report import output_paths
        try:
            if date.fromisoformat(args.day).isoformat() != args.day:
                raise ValueError('date')
            output_paths(args.state_dir,args.out)
            credentials=None
            if args.credentials_file:
                import json
                from lp_v2.readonly_sdk import L2Credentials
                with owner_file(args.credentials_file) as source:
                    credentials=L2Credentials(**json.load(source))
        except Exception:
            parser.error('report requires an exact UTC date, safe output outside state, and valid explicit L2 credentials')
        return ReportConfig(args.state_dir,args.day,args.out,credentials)
    import re
    if args.code_sha != 'UNKNOWN' and not re.fullmatch('[0-9a-f]{40}', args.code_sha):
        parser.error('--code-sha must be an exact 40-character SHA')
    if args.command == 'preflight':
        if not re.fullmatch('0x[0-9a-fA-F]{64}', args.market):
            parser.error('preflight requires an exact --market condition id')
        if args.code_sha == 'UNKNOWN':
            parser.error('preflight requires an exact code SHA')
        return PreflightConfig(args.capital_usdc,datetime.now(UTC),args.market,args.code_sha)
    if args.live:
        markets = tuple(dict.fromkeys(args.market or ()))
        if not 1 <= len(markets) <= 3 or any(not re.fullmatch('0x[0-9a-fA-F]{64}', cid) for cid in markets):
            parser.error('live requires 1–3 exact --market condition ids')
        if not args.private_key_file or not args.state_dir or not args.credentials_file:
            parser.error('live requires explicit signing, L2 credential and state paths')
        try:
            with owner_file(args.private_key_file):
                pass
            if args.relayer_credentials_file:
                with owner_file(args.relayer_credentials_file):
                    pass
        except Exception:
            parser.error('signing/relayer file must be an owned regular 0600 file, without symlinks')
    elif any((args.market,args.private_key_file,args.state_dir,args.relayer_credentials_file)):
        parser.error('live options require --live')
    credentials = None
    if args.credentials_file:
        import json, os, stat
        from lp_v2.readonly_sdk import L2Credentials
        try:
            fd = os.open(args.credentials_file, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as f:
                info = os.fstat(f.fileno())
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
                    raise ValueError('permissions')
                credentials = L2Credentials(**json.load(f))
        except Exception:
            parser.error('cannot load explicit owner-only L2 credential file')
    if args.live and (credentials.signature_type != 1 or not all(re.fullmatch('0x[0-9a-fA-F]{40}',v) for v in (credentials.signer_address,credentials.wallet_address))):
        parser.error('live requires explicit POLY_PROXY identity')
    return RunConfig(capital_usdc=args.capital_usdc, started_at=datetime.now(UTC), code_sha=args.code_sha, credentials=credentials,
        live=args.live,market=markets[0] if args.live else None,markets=markets if args.live else (),private_key_file=args.private_key_file,state_dir=args.state_dir,
        relayer_credentials_file=args.relayer_credentials_file)


def owner_file(path):
    """Open the validated inode; callers never print file content or I/O errors."""
    import os,stat
    fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise ValueError('owner-only regular file required')
        return os.fdopen(fd)
    except BaseException:
        os.close(fd)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["live"]:
        from lp_v2.live_control import main as live_main
        return live_main(argv[1:])
    config = parse_run(argv)
    if isinstance(config,ReportConfig):
        from lp_v2.report import write_report
        try:
            write_report(config)
        except Exception:
            print('Report failed; state was opened read-only. Check input facts and output access.')
            return 1
        print('Report CSV and JSON written; consult notes and safety UNKNOWN reasons.')
        return 0
    if isinstance(config,PreflightConfig):
        import json
        from lp_v2.preflight import run_preflight
        result = run_preflight(config)
        print(json.dumps(result))
        return 0 if result["status"] == "PASS" else 2
    if isinstance(config,StopConfig):
        from lp_v2.live_process import stop_process
        return stop_process(config.state_dir)
    print(
        f"Startup capital: {config.capital_usdc} USDC; "
        f"started at {config.started_at.isoformat()}"
    )
    if config.live:
        from lp_v2.live_process import ProcessLock
        from lp_v2.live_runtime import MarketCheckFailure
        try:
            with ProcessLock(config.state_dir) as lease:
                result=serve(config)
                lease.finish(result if type(result) is int else 1)
                return result if type(result) is int else 1
        except MarketCheckFailure as error:
            print(f'Live startup failed: stage={error.stage} reason_code={error.reason_code} market={error.market}')
            return 1
        except Exception:
            print('Live startup or shutdown failed; inspect protected local journal.')
            return 1
    serve(config)
    return 0
