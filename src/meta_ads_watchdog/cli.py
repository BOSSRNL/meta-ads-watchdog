"""Command line: `meta-ads-watchdog scan | reactivate | check-config`.

Exit codes: 0 done, 1 something failed (see the log), 2 the config is invalid,
3 a team was skipped because another run for it was still going.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Sequence

import requests
from dotenv import load_dotenv

from . import __version__
from .config import Config, ConfigError, Team, load_config
from .discord import Notifier, Webhook, plain
from .graph import GraphClient, GraphError
from .messages import TEXT
from .reactivate import run_reactivate
from .sale_days import day_mode
from .scan import local_today, run_scan
from .state import QUEUE_FILE

log = logging.getLogger("meta_ads_watchdog")

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_SKIPPED = 0, 1, 2, 3
DEFAULT_CONFIG = "watchdog.config.json"
LOG_BYTES = 1_000_000
LOG_BACKUPS = 5


class RedactFilter(logging.Filter):
    """Removes secrets (the token, webhook URLs) from every log line, tracebacks included."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        for secret in self.secrets:
            message = message.replace(secret, "***")
        record.msg, record.args = message, None
        return True


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = _parser().parse_args(argv)
    config_path = Path(args.config)
    load_dotenv(config_path.resolve().parent / ".env", override=False)
    try:
        config = load_config(config_path, os.environ)
        teams = _pick_teams(config, args.team)
    except ConfigError as error:
        print(f"Config error: {error}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        _setup_logging(config, args.command, teams if args.team else None, args.verbose)
    except OSError as error:
        print(f"Cannot write the log in {config.state_dir}: {error}", file=sys.stderr)
        return EXIT_FAILED
    try:
        return args.handler(config, teams, args)
    except Exception:  # last resort: the log keeps the details, secrets redacted
        log.exception("unexpected error")
        return EXIT_FAILED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meta-ads-watchdog", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
            ("scan", _scan, "check every active campaign and pause the ones breaking a rule"),
            ("reactivate", _reactivate, "turn back on campaigns paused for overspending before today"),
            ("check-config", _check_config, "validate the config; --online also tests the token")):
        command = commands.add_parser(name, help=help_text)
        command.set_defaults(handler=handler)
        command.add_argument("--config", default=DEFAULT_CONFIG, help=f"default: {DEFAULT_CONFIG}")
        command.add_argument("--team", action="append", help="only this team (repeatable)")
        command.add_argument("--verbose", action="store_true", help="show every campaign checked")
        if name == "check-config":
            command.add_argument("--online", action="store_true",
                                 help="read-only check of the token and each ad account")
        else:
            command.add_argument("--dry-run", action="store_true",
                                 help="change nothing, even if the config says dry_run: false")
    return parser


def _pick_teams(config: Config, names: list[str] | None) -> tuple[Team, ...]:
    if not names:
        return config.teams
    wanted = {name.strip().lower() for name in names}
    teams = tuple(team for team in config.teams if team.name.lower() in wanted)
    missing = wanted - {team.name.lower() for team in teams}
    if missing:
        raise ConfigError(f"no team named {', '.join(sorted(missing))} in the config")
    return teams


def log_file_name(command: str, teams: tuple[Team, ...] | None) -> str:
    """One log per command (and per team set), so parallel scheduled tasks never
    rotate the same file, which fails on Windows while another process has it open."""
    suffix = "-" + "-".join(team.slug for team in teams) if teams else ""
    return f"{command}{suffix}.log"


def _setup_logging(config: Config, command: str, teams: tuple[Team, ...] | None,
                   verbose: bool) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    file = RotatingFileHandler(config.state_dir / log_file_name(command, teams),
                               maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    file.setLevel(logging.INFO)
    secrets = [config.access_token] + [url for team in config.teams
                                       for url in (team.webhook, team.issues_webhook) if url]
    redact = RedactFilter(secrets)
    for handler in (console, file):
        handler.setFormatter(formatter)
        handler.addFilter(redact)
    for old in log.handlers:
        old.close()
    log.handlers[:] = [console, file]
    log.setLevel(logging.DEBUG)
    log.propagate = False


def _clients(config: Config) -> tuple[GraphClient, requests.Session]:
    session = requests.Session()
    return GraphClient(config.access_token, config.graph_version, session), session


def _notifier_factory(config: Config, session: requests.Session,
                      dry_run: bool) -> Callable[[Team], Notifier]:
    prefix = TEXT[config.language]["dry_run"] if dry_run else ""

    def make(team: Team) -> Notifier:
        return Notifier(Webhook(team.webhook, session), Webhook(team.issues_webhook, session),
                        config.state_dir / team.slug / QUEUE_FILE, prefix)
    return make


def _exit_code(failed: bool, skipped: list[str]) -> int:
    if failed:
        return EXIT_FAILED
    return EXIT_SKIPPED if skipped else EXIT_OK


def _skipped(skipped: list[str]) -> str:
    return f", skipped (another run still going): {', '.join(skipped)}" if skipped else ""


def _scan(config: Config, teams: tuple[Team, ...], args: argparse.Namespace) -> int:
    dry_run = config.dry_run or args.dry_run
    graph, session = _clients(config)
    result = run_scan(config, graph, _notifier_factory(config, session, dry_run),
                      datetime.now(timezone.utc), teams, dry_run)
    print(f"Scan finished{' (dry run)' if dry_run else ''}: paused {result.paused}, "
          f"errors {result.errors}{_skipped(result.skipped_teams)}")
    return _exit_code(bool(result.errors or result.token_failed), result.skipped_teams)


def _reactivate(config: Config, teams: tuple[Team, ...], args: argparse.Namespace) -> int:
    dry_run = config.dry_run or args.dry_run
    graph, session = _clients(config)
    result = run_reactivate(config, graph, _notifier_factory(config, session, dry_run),
                            datetime.now(timezone.utc), teams, dry_run)
    print(f"Reactivation finished{' (dry run)' if dry_run else ''}: on {result.turned_on}, "
          f"retry {result.retry}, check by hand {result.manual}, errors {result.errors}"
          + (", STOPPED early" if result.aborted else "") + _skipped(result.skipped_teams))
    failed = bool(result.aborted or result.retry or result.errors)
    return _exit_code(failed, result.skipped_teams)


def _check_config(config: Config, teams: tuple[Team, ...], args: argparse.Namespace) -> int:
    today = local_today(config, datetime.now(timezone.utc))
    mode = day_mode(today, config.sale_days)
    print(f"Config OK. {len(teams)} team(s), "
          f"{sum(len(team.ad_accounts) for team in teams)} ad account(s).")
    print(f"Mode: {'DRY RUN' if config.dry_run else 'LIVE (pauses for real)'}. "
          f"Time zone {config.timezone}. Today {today}: {mode.reason or 'normal day'}.")
    for team in teams:
        print(f"- {team.name}: {len(team.ad_accounts)} account(s), "
              f"Discord {'on' if team.webhook else 'OFF'}, "
              f"issues channel {'on' if team.issues_webhook else 'off'}")
    if not args.online:
        return EXIT_OK
    return _check_online(config, teams)


def _check_online(config: Config, teams: tuple[Team, ...]) -> int:
    graph, _ = _clients(config)
    failed = False
    try:
        graph.get("/me", {"fields": "id"})
        print("Token OK.")
    except GraphError as error:
        print(f"Token FAILED: {plain(error, 300)}")
        return EXIT_FAILED
    for account in (a for team in teams for a in team.ad_accounts):
        try:
            info = graph.get(f"/{account}", {"fields": "name,account_status,currency,timezone_name"})
        except GraphError as error:
            failed = True
            print(f"  {account}: FAILED {plain(error, 300)}")
            continue
        zone = plain(info.get("timezone_name"), 40)
        warning = "" if zone == str(config.timezone) else "  <- differs from the config time zone"
        print(f"  {account}: {plain(info.get('name'))} | status {plain(info.get('account_status'), 10)}"
              f" | {plain(info.get('currency'), 10)} | {zone}{warning}")
    return EXIT_FAILED if failed else EXIT_OK
