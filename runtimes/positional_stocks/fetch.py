"""The weekly run of ``wsr1_weekly_stochrsi`` — ``fetch`` mode (spec 6.1, 6.2, 10).

    python -m runtimes.positional_stocks.weekly_run --mode fetch \
        [--as-of auto|YYYY-MM-DD] [--force-refetch]

The only mode that touches the network. It refreshes the symbol-keyed daily
cache that ``decide`` reads offline, then writes a PREVIEW of the decision
for the operator's weekend review. It is imported only when ``--mode fetch``
is given, so ``decide`` never loads the Dhan client, the scrip master or the
authentication stack.

**Order of a run** (after the paper check, the preflight and the lock, shared
with ``decide`` in :func:`~.weekly_run.run`):

1. The 20-minute deadline, the target week and the **end date**:
   ``calendar.expected_last_session(target)``. No request ever asks for a
   later day (Phase 4b-2 D-entry): an unfinished same-day candle in the cache
   would make the next overlap check see a false restatement.
2. The symbols that need a refresh: every universe, held and pending symbol,
   plus NIFTY 50, whose cache does not cover the end date, or whose last full
   fetch was in an earlier calendar month (spec 6.1: the first fetch of each
   month refetches everything; a file with no marker is refetched in full), or
   every symbol with ``--force-refetch`` (limitation 40). A symbol whose cache
   already reaches **past** the end date is never fetched or written (audit
   R6-3). A symbol the scrip master could not resolve last time is skipped
   (spec 5) — unless it is held or pending (audit R6-2).
3. **Idempotent:** nothing to refresh and the preview exists -> exit 0 at
   once, with no network call.
4. Authentication and the token's remaining life, asserted against the whole
   fetch budget. **Token safety (spec 10.3 v1.3, D116):** no new login during
   a trading day's session window or while another runtime's supervisor is
   running — a cached token with enough life, or "login deferred" (exit 1).
   Then the scrip master; a held or pending symbol it cannot resolve counts
   as failed (audit R6-2).
5. NIFTY 50 first. It failing is systemic (exit 5); it lacking the end date
   is "not yet published" (exit 2) — no preview, the next attempt retries,
   and only the schedule's final attempt alerts (spec 10.3 v1.3).
6. Every other symbol, throttled to 3 requests/second (retries included),
   the deadline checked before each: a tail refetch that overlaps 10 cached
   sessions (``merge_tail``; a restated series is refetched in full), or full
   history from 2000-01-01; a restated file is replaced only once its full
   history has arrived (audit R6-1). Failures get a second pass at the end.
7. **Systemic** — the deadline (exit 4), or more than
   :data:`MAX_FAILED_SYMBOLS` failed (exit 5): no preview, a report listing
   what was refreshed, a Telegram alert. **Small** — 10 or fewer failed: the
   preview is written, the failed symbols are stale to it exactly as they
   will be to Monday's decide run (spec 6.2), and the run exits 6.
8. The preview: the decide computation on a temporary copy of the book
   (``positional_stocks.db`` is never created or changed, and no snapshot is
   taken), ``<week_ending>-preview.md``, one Telegram marked PREVIEW.
"""

from __future__ import annotations

import json
import time as _time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from common.authentication import AuthBootstrap, AuthCredentials, AuthError
from common.authentication.exceptions import MissingCredentialsError
from common.authentication.token_cache import StoredToken
from common.config import load_auto_start_config
from common.logging import get_logger
from common.market_data.dhan_historical import (
    DhanHistoricalDataClient,
    HistoricalDataError,
    HttpPost,
)
from common.market_data.scrip_master import (
    EquityScripMaster,
    ScripMasterError,
    resolve_index,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.daily_bars import (
    DailyResponseError,
    parse_daily_response,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.daily_cache import (
    FULL_HISTORY_FROM,
    DailyBarCache,
    assess_index_publication,
    merge_tail,
    refetch_from,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.inputs import InputFileError
from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import WeekKey, week_of
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import DailyBar
from strategies.positional_stocks.wsr1_weekly_stochrsi.pacing import (
    DEFAULT_REQUESTS_PER_SECOND,
    RequestThrottle,
    RunDeadline,
    RunDeadlineExceeded,
    TokenLifeError,
    require_remaining_life,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar

from .report import PreviewInfo, render_failure, week_label
from .telegram_summary import send_alert
from .week_inputs import load_operator_inputs
from .weekly_run import (
    EXIT_DEADLINE,
    EXIT_FETCH_FAILED,
    EXIT_NO_TRADES,
    EXIT_OK,
    EXIT_PREVIEW_PARTIAL,
    Options,
    RunEnvironment,
    _held_and_pending,
    _run_locked,
    as_of_moment,
    refuse,
    target_week,
)

_log = get_logger(__name__)

#: Phase 4b-2 D-entry: more universe symbols than this (5% of 200) failing
#: after the client's retries and the second pass is systemic — no preview.
MAX_FAILED_SYMBOLS = 10

#: Dhan's cash-equity and spot-index request fields (verified in Phase 1).
EQUITY = ("NSE_EQ", "EQUITY")
INDEX = ("IDX_I", "INDEX")

#: Symbols the scrip master could not resolve, remembered between attempts so
#: a later attempt's idempotency check can skip them (spec 5).
STATE_FILE = "fetch_state.json"

#: The minimum token life asked for: the whole fetch budget plus 5 minutes.
TOKEN_MARGIN_SECONDS = 300.0

#: Spec 10.3 v1.3 token safety (D116): on a calendar trading day, no new
#: login from this long before ``auto_start.startup_time`` (when the paper
#: runtimes authenticate) to this long after the market close.
LOGIN_BLACKOUT_MARGIN = timedelta(minutes=30)
#: NSE's cash-market close. Not in ``config/global.yaml``, whose session
#: times are the options runtimes' entry cut-off and square-off.
MARKET_CLOSE = time(15, 30)


class FetchRefused(RuntimeError):
    """Authentication, the token's life or the scrip master: exit 1."""


@dataclass(frozen=True)
class Credentials:
    """What authentication produced — the token itself is never printed."""

    client_id: str
    access_token: str
    #: "environment", "cache" or "generated" (AuthBootstrap's outcome).
    source: str
    #: Seconds of life left, or -1 when undeterminable (it then passes).
    remaining_seconds: float


@dataclass
class FetchServices:
    """Fetch mode's network seams. The real ones come from settings; every
    test injects fakes, so no test reaches the network."""

    #: ``(minimum_seconds, allow_login)``: credentials good for at least that
    #: long. With ``allow_login`` false it may only reuse an environment or
    #: cached token, never mint one (spec 10.3 token safety). Raises
    #: :class:`FetchRefused`.
    authenticate: Callable[[float, bool], Credentials]
    #: Today's instrument master CSV text (through ``ScripMasterCache``).
    scrip_master_text: Callable[[], str]
    #: ``None`` is ``httpx.post``.
    http_post: HttpPost | None = None
    sleep: Callable[[float], None] = _time.sleep
    #: The throttle's clock (the run deadline uses ``RunEnvironment.monotonic``).
    monotonic: Callable[[], float] = _time.monotonic


def default_services(env: RunEnvironment) -> FetchServices:
    """The real services: ``.env`` credentials through ``AuthBootstrap`` (the
    token cache first), a log redactor that learns every minted token, and
    the day-stamped scrip-master cache."""
    from common.config import load_settings
    from common.config.secrets import read_secret
    from common.logging import setup_logging
    from common.market_data.scrip_master import ScripMasterCache

    settings = load_settings()
    paths = env.paths
    redactor = setup_logging(
        level=settings.algo_log_level, log_dir=paths.log_root, settings=settings
    )

    def authenticate(minimum_seconds: float, allow_login: bool) -> Credentials:
        credentials = AuthCredentials(
            client_id=read_secret(settings.dhan_client_id) or "",
            pin=read_secret(settings.dhan_pin),
            totp_secret=read_secret(settings.dhan_totp_secret),
            access_token=read_secret(settings.dhan_access_token),
        )
        result = authenticate_with(
            credentials,
            cache_dir=paths.cache_root,
            minimum_seconds=minimum_seconds,
            allow_login=allow_login,
            on_minted=lambda token: redactor.add_secrets([token]),
        )
        redactor.add_secrets([result.access_token])
        return result

    return FetchServices(
        authenticate=authenticate,
        scrip_master_text=lambda: ScripMasterCache(paths.cache_root).text(),
    )


def authenticate_with(
    credentials: AuthCredentials,
    *,
    cache_dir: Path,
    minimum_seconds: float,
    allow_login: bool,
    on_minted: Callable[[str], None] | None = None,
) -> Credentials:
    """A token for the fetch, through the existing ``AuthBootstrap``.

    With ``allow_login`` false (spec 10.3 token safety) the bootstrap is
    handed no PIN and no TOTP secret, so it *cannot* mint a token: it has no
    login object at all, and a missing or short-lived cached token raises
    before any request to the auth endpoint. The fetch then refuses with
    "login deferred".
    """
    if not credentials.client_id:
        raise FetchRefused("DHAN_CLIENT_ID is not set in .env")
    usable = (
        credentials
        if allow_login
        else AuthCredentials(client_id=credentials.client_id, access_token=credentials.access_token)
    )
    bootstrap = AuthBootstrap(
        usable,
        cache_dir=cache_dir,
        # A cached token with less life than the run needs is passed over:
        # for a fresh login when one is allowed, for a refusal otherwise.
        expiry_margin_seconds=int(minimum_seconds),
        on_token_minted=on_minted,
    )
    try:
        token, outcome = bootstrap.get_token()
    except MissingCredentialsError as exc:
        if not allow_login:
            raise FetchRefused(
                "login deferred: no environment or cached token with "
                f"{minimum_seconds / 60:.0f} minutes of life, and a new login is not allowed now"
            ) from exc
        raise FetchRefused(f"cannot authenticate ({type(exc).__name__}): {exc}") from exc
    except AuthError as exc:
        raise FetchRefused(f"cannot authenticate ({type(exc).__name__}): {exc}") from exc
    try:
        remaining = require_remaining_life(
            StoredToken(token, credentials.client_id, "", None), minimum_seconds
        )
    except TokenLifeError as exc:
        raise FetchRefused(str(exc)) from exc
    return Credentials(credentials.client_id, token, outcome.source, remaining)


def login_block(env: RunEnvironment, calendar: TradingCalendar, now: datetime) -> str | None:
    """Why a new Dhan login is not allowed now (spec 10.3 v1.3, D116), or
    ``None``. Fetch shares ``data/cache/token_cache.json`` with the paper
    runtimes, and whether a new login cancels older tokens is unverified, so:

    * on a calendar trading day, never from 30 minutes before
      ``auto_start.startup_time`` to 30 minutes after the 15:30 close;
    * never while another runtime's supervisor is verifiably running. This
      is read-only: :meth:`~common.process.locks.ProcessLock.current_owner`
      reads the pid file and asks the OS; it creates, locks and changes
      nothing. It also covers a special weekend session, which the existing
      session code does not model (D116): the paper runtimes never start on
      a weekend, and a manual one would be caught here.
    """
    from common.process.locks import supervisor_lock
    from common.utils.timeutils import parse_hhmm

    paths = env.paths
    local = now.astimezone(ZoneInfo(calendar.timezone))
    if calendar.is_trading_day(local.date()):
        startup = parse_hhmm(load_auto_start_config(paths.config_root).startup_time)
        tz = local.tzinfo
        opens = datetime.combine(local.date(), startup, tz) - LOGIN_BLACKOUT_MARGIN
        closes = datetime.combine(local.date(), MARKET_CLOSE, tz) + LOGIN_BLACKOUT_MARGIN
        if opens <= local <= closes:
            return (
                f"{local:%A %d %b %H:%M} IST is a trading day's session window "
                f"({opens:%H:%M}-{closes:%H:%M})"
            )
    runtimes_dir = paths.config_root / "runtimes"
    own = env.config.runtime_id
    for runtime_id in sorted(p.stem for p in runtimes_dir.glob("*.yaml") if p.stem != own):
        lock = supervisor_lock(
            runtime_id=runtime_id, lock_dir=paths.lock_root, pid_dir=paths.pid_root
        )
        owner = lock.current_owner()
        if owner is not None:
            return f"the {runtime_id} supervisor is running (pid {owner.pid})"
    return None


# ---------------------------------------------------------------- the run
@dataclass
class _Tally:
    """What happened to each symbol, for the report."""

    tail: list[str] = field(default_factory=list)
    full: list[str] = field(default_factory=list)
    restated: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)

    @property
    def refreshed(self) -> list[str]:
        return sorted({*self.tail, *self.full})

    def counts(self) -> str:
        return (
            f"tail {len(self.tail)}, full {len(self.full)} (of which restated "
            f"{len(self.restated)}), failed {len(self.failed)}, not in the scrip master "
            f"{len(self.unresolved)}"
        )


@dataclass
class _Fetch:
    options: Options
    env: RunEnvironment
    services: FetchServices
    calendar: TradingCalendar
    cache: DailyBarCache
    target: WeekKey
    end: date
    now: datetime
    reports: Path
    deadline: RunDeadline
    tally: _Tally = field(default_factory=_Tally)

    def say(self, line: str) -> None:
        self.env.out(line)


def run_fetch(options: Options, env: RunEnvironment) -> int:
    """Fetch mode, under the weekly run's lock (see the module docstring)."""
    config = env.config
    paths = env.paths
    deadline = RunDeadline.of_minutes(config.fetch_deadline_minutes, monotonic=env.monotonic)
    calendar = TradingCalendar.from_config(paths.config_root)
    try:
        operator = load_operator_inputs(paths.config_root / "positional_stocks")
    except InputFileError as exc:
        return refuse(env, options, str(exc))
    now = env.now()
    target = target_week(as_of_moment(options.as_of, now, calendar.timezone), calendar)
    end = calendar.expected_last_session(target)
    cache = DailyBarCache.under(paths.cache_root, tz_name=calendar.timezone)
    reports = paths.data_root / "reports" / "positional_stocks"
    reports.mkdir(parents=True, exist_ok=True)
    run = _Fetch(options, env, _services(env), calendar, cache, target, end, now, reports, deadline)
    started = env.monotonic()

    held, pending = _held_and_pending(paths.database_path(config.runtime_id), config.strategy_id)
    kept = set(held) | set(pending)
    symbols = sorted(set(operator.universe.by_symbol) | kept)
    # --force-refetch tries every symbol again, the unresolved ones included.
    # A held or pending symbol is never exempt (spec 6.1 v1.3, audit R6-2).
    known_unresolved = (
        set() if options.force_refetch else set(_load_state(cache).get("unresolved", []))
    ) - kept
    needed = [s for s in (config.index_symbol, *symbols) if _needs_refresh(run, s)]
    needed = [s for s in needed if s not in known_unresolved]
    preview_path = reports / f"{end}-preview.md"
    run.say(
        f"fetch {week_label(target)} through {end}: {len(needed)} of {len(symbols) + 1} "
        "symbol(s) to refresh"
    )
    if not needed and preview_path.is_file():
        run.say(f"up to date: the cache covers {end} and {preview_path.name} exists; nothing to do")
        return EXIT_OK

    lines: list[str] = []
    if needed:
        code = _refresh(run, needed, config.index_symbol, known_unresolved, kept, lines)
        if code is not None:
            return code

    unpublished = _unpublished(run, config.index_symbol)
    if unpublished is not None:
        return unpublished
    failed = [s for s in run.tally.failed if s != config.index_symbol]
    if len(failed) > MAX_FAILED_SYMBOLS:
        return _systemic(
            run,
            "fetch failed",
            f"{len(failed)} symbols failed after the second pass (more than "
            f"{MAX_FAILED_SYMBOLS}): {', '.join(failed)}",
            EXIT_FETCH_FAILED,
        )

    sessions = [b.session for b in cache.read(config.index_symbol) if week_of(b.session) == target]
    lines = [
        f"Target {week_label(target)}, last session {end} (requests end there, never later)",
        f"NIFTY 50 sessions in {week_label(target)}: {', '.join(map(str, sessions))}",
        f"Refreshed {len(run.tally.refreshed)}: {run.tally.counts()}",
        *lines,
        f"Fetch took {env.monotonic() - started:.1f} s of the "
        f"{config.fetch_deadline_minutes:.0f}-minute budget",
    ]
    if run.tally.restated:
        lines.append(f"Restated and refetched in full: {', '.join(run.tally.restated)}")
    if run.tally.unresolved:
        lines.append(
            "Not in the scrip master, skipped (spec 5): " + ", ".join(run.tally.unresolved)
        )
    for symbol, reason in run.tally.failed.items():
        lines.append(f"{symbol}: {reason}")
    for line in lines:
        run.say(line)

    info = PreviewInfo(lines=tuple(lines), failed=tuple(failed))
    decide = Options(mode="decide", as_of=options.as_of)
    code = _run_locked(decide, env, preview=info, deadline=deadline)
    if code == EXIT_OK and failed:
        run.say(f"preview written; {len(failed)} symbol(s) failed: {', '.join(failed)}")
        return EXIT_PREVIEW_PARTIAL
    return code


def _services(env: RunEnvironment) -> FetchServices:
    return env.fetch_services if env.fetch_services is not None else default_services(env)


def _needs_refresh(run: _Fetch, symbol: str) -> bool:
    cached = run.cache.read(symbol)
    if cached and cached[-1].session > run.end:
        # Spec 6.1 v1.3 (audit R6-3): a fetch for an earlier week never
        # replaces or shortens a cache that already reaches further.
        return False
    if run.options.force_refetch:
        return True
    if not cached or cached[-1].session < run.end:
        return True
    return _full_is_due(run, run.cache.metadata(symbol))


def _full_is_due(run: _Fetch, meta: dict[str, object]) -> bool:
    """Spec 6.1: the first fetch of each calendar month refetches everything.
    A file with no marker (written before Phase 4b-2) is refetched in full."""
    stamp = meta.get("full_history_at")
    if not isinstance(stamp, str):
        return True
    last = datetime.fromisoformat(stamp)
    return (last.year, last.month) != (run.now.year, run.now.month)


def _refresh(
    run: _Fetch,
    needed: list[str],
    index_symbol: str,
    known_unresolved: set[str],
    kept: set[str],
    lines: list[str],
) -> int | None:
    """Authenticate, resolve and refresh ``needed``. Returns an exit code to
    stop with, or ``None`` to go on to the preview."""
    services = run.services
    minimum = run.deadline.total_seconds + TOKEN_MARGIN_SECONDS
    block = login_block(run.env, run.calendar, run.now)
    try:
        credentials = services.authenticate(minimum, block is None)
    except FetchRefused as exc:
        reason = str(exc) if block is None else f"{exc} ({block})"
        return refuse(run.env, run.options, reason)
    try:
        text = services.scrip_master_text()
        equities = EquityScripMaster().load_from_text(text)
        index_row = resolve_index(text, index_symbol)
    except (ScripMasterError, OSError, ValueError) as exc:
        return refuse(run.env, run.options, f"scrip master: {exc}")
    life = (
        "undeterminable"
        if credentials.remaining_seconds < 0
        else f"{credentials.remaining_seconds / 3600:.1f} h of life left"
    )
    lines.append(f"Token: source {credentials.source}, {life}")

    stocks = [s for s in needed if s != index_symbol]
    resolved, unresolved = equities.resolve_all(stocks)
    # Spec 6.1 v1.3 (audit R6-2): only a symbol neither held nor pending is
    # skipped quietly. A held or pending one counts as failed: highlighted,
    # an operator action, exit 6, and never exempt from idempotency.
    for symbol in sorted(s for s in unresolved if s in kept):
        run.tally.failed[symbol] = "not in the scrip master (held or pending: counts as failed)"
    run.tally.unresolved = sorted(s for s in unresolved if s not in kept)
    _save_state(
        run.cache,
        {"unresolved": sorted(set(run.tally.unresolved) | (known_unresolved - set(stocks)))},
    )
    ids: dict[str, tuple[str, str, str]] = {
        s: (row.security_id, *EQUITY) for s, row in resolved.items()
    }
    ids[index_symbol] = (index_row.security_id, *INDEX)

    throttle = RequestThrottle(
        DEFAULT_REQUESTS_PER_SECOND, monotonic=services.monotonic, sleep=services.sleep
    )
    client = DhanHistoricalDataClient(
        credentials.client_id,
        credentials.access_token,
        http_post=services.http_post,
        sleep=services.sleep,
        before_request=throttle,
    )
    order = [s for s in needed if s in ids]
    try:
        for symbol in order:
            run.deadline.check(f"fetching {symbol}")
            _one(run, client, symbol, ids[symbol])
            if symbol != index_symbol:
                continue
            if symbol in run.tally.failed:
                return _systemic(
                    run,
                    "fetch failed",
                    f"NIFTY 50 could not be fetched: {run.tally.failed[symbol]}",
                    EXIT_FETCH_FAILED,
                )
            # Spec 6.2: checked at once, so an unpublished week costs one
            # request, not a whole universe.
            unpublished = _unpublished(run, index_symbol)
            if unpublished is not None:
                return unpublished
        second = [s for s in run.tally.failed if s in ids]
        if second:
            run.say(f"second pass: {len(second)} symbol(s)")
        for symbol in second:
            run.deadline.check(f"re-fetching {symbol}")
            del run.tally.failed[symbol]
            _one(run, client, symbol, ids[symbol])
    except RunDeadlineExceeded as exc:
        return _systemic(run, "deadline exceeded", str(exc), EXIT_DEADLINE)
    lines.append(
        f"Requests {client.request_count} at most {throttle.max_per_second}/s; the throttle "
        f"waited {throttle.total_wait_seconds:.1f} s"
    )
    return None


def _unpublished(run: _Fetch, index_symbol: str) -> int | None:
    """Spec 6.2: NIFTY 50 must have the calendar's last session of the week,
    or the week is not yet published — no preview, exit 2."""
    verdict = assess_index_publication(
        [b for b in run.cache.read(index_symbol) if week_of(b.session) <= run.target],
        week=run.target,
        expected_session=run.end,
        calendar=run.calendar,
    )
    if verdict.is_published:
        return None
    # Spec 10.3 v1.3: alert only on the final attempt before the decide run.
    final = run.env.config.schedule.is_final_fetch_attempt(run.now, run.end)
    return _systemic(run, "not yet published", verdict.reason, EXIT_NO_TRADES, alert=final)


def _one(
    run: _Fetch, client: DhanHistoricalDataClient, symbol: str, ident: tuple[str, str, str]
) -> None:
    """Refresh one symbol into the cache; a failure is recorded, never raised."""
    security_id, segment, instrument = ident
    cache, end = run.cache, run.end

    def fetch(start: date) -> list[DailyBar]:
        body: dict[str, Any] = client.fetch_daily(
            security_id=security_id,
            exchange_segment=segment,
            instrument_type=instrument,
            from_date=start,
            to_date=end,
        )
        return parse_daily_response(body, tz_name=run.calendar.timezone, label=symbol)

    def write(bars: Sequence[DailyBar], full_at: str | None) -> None:
        cache.write(
            symbol,
            bars,
            fetched_at=run.now,
            security_id=security_id,
            exchange_segment=segment,
            instrument=instrument,
            full_history_at=full_at,
        )

    try:
        cached = cache.read(symbol)
        meta = cache.metadata(symbol)
        start = refetch_from(cached)
        if cached and cached[-1].session > end:
            return  # audit R6-3: never shorten a cache that reaches further
        if run.options.force_refetch or start is None or _full_is_due(run, meta):
            write(fetch(FULL_HISTORY_FROM), run.now.isoformat())
            run.tally.full.append(symbol)
            return
        if start > end:
            return  # already covers the week beyond any overlap
        outcome = merge_tail(cached, fetch(start))
        if outcome.needs_full_refetch:
            run.say(outcome.describe(symbol))
            # Audit R6-1: the old file stays until the full history has
            # arrived; the write then replaces it atomically. A failed full
            # refetch leaves it untouched and the symbol failed.
            full = fetch(FULL_HISTORY_FROM)
            write(full, run.now.isoformat())
            run.tally.full.append(symbol)
            run.tally.restated.append(symbol)
            return
        stamp = meta.get("full_history_at")
        write(outcome.bars, stamp if isinstance(stamp, str) else None)
        run.tally.tail.append(symbol)
    except (HistoricalDataError, DailyResponseError, ValueError, OSError) as exc:
        run.tally.failed[symbol] = f"{type(exc).__name__}: {exc}"
        run.say(f"{symbol}: FAILED — {type(exc).__name__}")


def _systemic(run: _Fetch, kind: str, reason: str, code: int, *, alert: bool = True) -> int:
    """No preview: report what was refreshed and what failed, alert, and let
    the next scheduled attempt retry. ``alert`` is false only for "not yet
    published" before the final attempt (spec 10.3 v1.3): logged, reported,
    not sent."""
    env = run.env
    config = env.config
    tally = run.tally
    detail = (
        f"{reason}. Refreshed {len(tally.refreshed)} ({tally.counts()}): "
        f"{', '.join(tally.refreshed) or 'none'}."
    )
    if tally.failed:
        detail += " Failed: " + "; ".join(f"{s} ({r})" for s, r in tally.failed.items()) + "."
    if tally.unresolved:
        detail += " Not in the scrip master: " + ", ".join(tally.unresolved) + "."
    status = "not sent: a later fetch attempt is scheduled before the decide run"
    if alert:
        status = send_alert(
            env.notifier,
            f"PREVIEW {week_label(run.target)}: {kind} — {reason}",
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
        )
    text = render_failure(
        strategy_id=config.strategy_id,
        generated_at=env.now(),
        week=run.target,
        kind=f"PREVIEW NOT WRITTEN — {kind}",
        reason=detail,
        dry_run=False,
        notifier_status=status,
    )
    path = run.reports / f"{run.end}-preview-failed.md"
    path.write_text(text, encoding="utf-8")
    run.say(f"NO PREVIEW — {kind}: {reason}")
    run.say(f"report: {path}")
    return code


def _load_state(cache: DailyBarCache) -> dict[str, list[str]]:
    path = cache.root.parent / STATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(cache: DailyBarCache, state: dict[str, list[str]]) -> None:
    path = cache.root.parent / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


__all__ = [
    "MAX_FAILED_SYMBOLS",
    "Credentials",
    "FetchRefused",
    "FetchServices",
    "default_services",
    "run_fetch",
]
