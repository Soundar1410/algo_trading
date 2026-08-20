"""The unattended daily start entrypoint.

    .venv/bin/python -m orchestration.auto_start [--config-root config] [--dry-run]

This is what the ``com.soundarraj.algotrading.autostart`` LaunchAgent runs, via
a ``/bin/sh`` wrapper that waits for ``/Volumes/Trading`` first (see
``orchestration/launchd/generate_plists.py``). Everything it does is decided by
:class:`~orchestration.auto_start.controller.AutoStartController`; this file
only builds the real dependencies that controller is otherwise given by tests.

It does **not** start the dashboard. The dashboard has exactly one owner — its
own ``RunAtLoad`` LaunchAgent — so that it is available independently of
trading, including at weekends.
"""

from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from common.authentication import AuthBootstrap, AuthCredentials
from common.config import (
    AutoStartConfig,
    load_auto_start_config,
    load_paths,
    load_settings,
)
from common.config.secrets import read_secret
from common.engine.session import MarketSession
from common.logging import get_logger, setup_logging
from common.notifications import build_notifier
from common.process import shutdown_signals
from common.utils.timeutils import now_tz

from .controller import EXIT_OK, EXIT_TERMINAL_REFUSAL, AutoStartController
from .day_claim import DailyNotificationClaim
from .gate import build_session
from .retry import DeadlineWaiter
from .runtime_launcher import RuntimeLauncher

_log = get_logger(__name__)

#: Where the once-per-trading-date notification record lives.
CLAIM_FILENAME = "auto_start_notifications.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config-root", type=Path, default=Path("config"))
    parser.add_argument(
        "--now",
        default=None,
        help="ISO timestamp to evaluate the start window against (testing only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate the gate and preflight, then stop before authenticating.",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    paths = load_paths(settings=settings)
    paths.ensure_writable_dirs()
    # Installed first: everything below this line is redacted.
    redactor = setup_logging(
        level=settings.algo_log_level,
        log_dir=paths.log_root,
        log_file_name="auto_start.log",
        settings=settings,
    )

    cfg = load_auto_start_config(args.config_root)
    session = build_session(cfg)
    stop_event = threading.Event()

    def _clock() -> datetime:
        if args.now:
            return datetime.fromisoformat(args.now)
        return now_tz(cfg.timezone)

    waiter = DeadlineWaiter(
        interval_seconds=cfg.retry_interval_seconds,
        max_interval_seconds=cfg.retry_max_interval_seconds,
        multiplier=cfg.retry_backoff_multiplier,
        clock=_clock,
        stop_event=stop_event,
    )

    def _bootstrap_factory() -> AuthBootstrap:
        credentials = AuthCredentials(
            client_id=read_secret(settings.dhan_client_id) or "",
            pin=read_secret(settings.dhan_pin),
            totp_secret=read_secret(settings.dhan_totp_secret),
            access_token=read_secret(settings.dhan_access_token),
        )
        return AuthBootstrap(
            credentials,
            cache_dir=paths.cache_root,
            on_token_minted=lambda token: redactor.add_secrets([token]),
        )

    def _launcher_factory() -> RuntimeLauncher:
        return RuntimeLauncher(
            python_bin=paths.project_root / ".venv" / "bin" / "python",
            paths=paths,
            config_root=args.config_root,
            clock=_clock,
            stop_event=stop_event,
            handshake_seconds=cfg.runtime_handshake_seconds,
            shutdown_grace_seconds=cfg.shutdown_grace_seconds,
        )

    controller = AutoStartController(
        cfg=cfg,
        config_root=args.config_root,
        paths=paths,
        session=session,
        clock=_clock,
        waiter=waiter,
        stop_event=stop_event,
        notifier=build_notifier(settings),
        claim=DailyNotificationClaim(paths.runtime_root / CLAIM_FILENAME),
        bootstrap_factory=_bootstrap_factory,
        launcher_factory=_launcher_factory,
    )

    if args.dry_run:
        return _dry_run(controller, cfg=cfg, clock=_clock, session=session)

    with shutdown_signals(stop_event.set):
        outcome = controller.run()

    _log.info("auto-start finished: exit=%d %s", outcome.exit_code, outcome.reason)
    print(f"auto-start: {outcome.reason}")
    return outcome.exit_code


def _dry_run(
    controller: AutoStartController,
    *,
    cfg: AutoStartConfig,
    clock: Callable[[], datetime],
    session: MarketSession,
) -> int:
    """Report what the gate and paper-safety checks say, and start nothing."""
    from .gate import evaluate_start_window
    from .paper_safety import verify_paper_only

    decision = evaluate_start_window(cfg, now=clock(), session=session)
    print(f"gate: eligible={decision.eligible} terminal={decision.terminal} — {decision.reason}")
    report = verify_paper_only(controller._config_root)
    print(f"paper-safe: {report.safe}")
    for violation in report.violations:
        print(f"  violation: {violation}")
    for plan in report.plans:
        print(f"  runtime {plan.runtime_id}: {', '.join(plan.strategy_ids) or 'no strategies'}")
    print("dry run: nothing was authenticated and no runtime was started.")
    acceptable = (decision.eligible or decision.quiet) and report.safe
    return EXIT_OK if acceptable else EXIT_TERMINAL_REFUSAL


if __name__ == "__main__":
    sys.exit(main())
