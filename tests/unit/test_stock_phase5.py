"""Phase 5 Part A (spec v1.3): audit round 6 fixes and the operational rules.

* R6-2: a held or pending symbol the scrip master cannot resolve counts as
  failed — highlighted, an operator action, exit 6, never "up to date".
* R6-1: a restated symbol's file is replaced only after its full history has
  arrived; a failed full refetch leaves the old file.
* R6-3: a fetch for an earlier week never shortens a longer cache.
* R6-4: every refusal writes a report and alerts (D117).
* R6-5: a STARTED first week can always be redone (test_stock_audit_r5.py).
* The watchlist ignores the quality gate and shows each symbol's status.
* The 0-share reason; "not yet published" alerts only on the final attempt;
  token safety (D116); both ``enabled`` flags gate the job (D115).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time as _time
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest
from _stock_run_fixtures import IST, Root
from _wsr1_rules_fixtures import (
    PARAMS0,
    book,
    ctx,
    index_series,
    kd_tape,
    quality,
    symbol_week,
)
from test_stock_fetch import (
    FRIDAY,
    W38_END,
    Credentials,
    FakeDhan,
    _fetch,
    _preview,
    _prices,
    _setup,
    _warm,
)

from common.authentication import AuthCredentials
from common.authentication.token_cache import TokenCache
from common.notifications.base import RecordingNotifier
from runtimes.positional_stocks import fetch
from runtimes.positional_stocks.fetch import FetchRefused, authenticate_with, login_block
from runtimes.positional_stocks.run_config import RunConfig, Schedule
from runtimes.positional_stocks.weekly_run import (
    EXIT_NO_TRADES,
    EXIT_OK,
    EXIT_PREVIEW_PARTIAL,
    EXIT_REFUSED,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.daily_cache import FULL_HISTORY_FROM
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    FunnelStage,
    OrderAction,
    PendingOrder,
    QualityStatus,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import (
    apply_fill,
    screen,
    sizing,
    watchlist,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar


def _held_a(root: Root, dhan: FakeDhan) -> None:
    """A held from W23; W38 decided; the cache as of W38's end."""
    _warm(root, dhan, FRIDAY)
    root.seed_entry()
    assert root.run("--as-of", W38_END.isoformat()) == EXIT_OK
    _warm(root, dhan, W38_END)


def _events(root: Root) -> list[Any]:
    assert isinstance(root.notifier, RecordingNotifier)
    return list(root.notifier.events)


def _sha_tree(directory: Path) -> dict[str, str]:
    if not directory.exists():
        return {}
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


# ===================================================================== R6-2
def test_r6_2_a_held_symbol_missing_from_the_scrip_master_counts_as_failed(
    tmp_path: Path,
) -> None:
    """The audit repro: a held symbol renamed. Before: exit 0, "Operator
    actions: 0", and every later attempt "up to date"."""
    root, dhan = _setup(tmp_path)
    _held_a(root, dhan)
    dhan.unlisted = {"A"}
    assert _fetch(root, dhan) == EXIT_PREVIEW_PARTIAL
    text = _preview(root).read_text()
    assert "**⚠ FETCH FAILED: A — held — its stop cannot be checked this week.**" in text
    assert "A: not in the scrip master (held or pending: counts as failed)" in text
    assert "Not in the scrip master, skipped" not in text
    message = _events(root)[-1].message
    assert "fetch failed 1" in message and "Operator actions: 0 " not in message
    state = json.loads((root.cache.root.parent / fetch.STATE_FILE).read_text())
    assert state["unresolved"] == []  # never exempt
    root.output.clear()
    assert _fetch(root, dhan) == EXIT_PREVIEW_PARTIAL  # not "up to date"
    assert not any(line.startswith("up to date") for line in root.output)


def test_r6_2_an_unheld_unresolved_symbol_is_still_skipped_quietly(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path, unlisted={"C"})
    assert _fetch(root, dhan) == EXIT_OK
    assert "Not in the scrip master, skipped (spec 5): C" in _preview(root).read_text()


# ===================================================================== R6-1
class _FullFails(FakeDhan):
    """Every full-history request for ``A`` fails; tails succeed."""

    def post(self, endpoint: str, *, json: dict[str, Any], **kw: object) -> httpx.Response:
        response = super().post(endpoint, json=json, **kw)  # records the request
        if json["securityId"] == self.ids()["A"] and json["fromDate"] == str(FULL_HISTORY_FROM):
            return httpx.Response(500, json={"errorMessage": "upstream timeout"})
        return response


def test_r6_1_a_failed_full_refetch_keeps_the_old_file(tmp_path: Path) -> None:
    """The audit repro: held A, a restated overlap, the full refetch fails.
    Before: the file was discarded first — 0 bars, preview-failed, exit 2,
    and Monday decided nothing for the whole book."""
    root = Root.create(tmp_path)
    dhan = _FullFails(root, _prices())
    _held_a(root, dhan)
    before = root.cache.path_for("A").read_bytes()
    dhan.restated.append(("A", date(2026, 9, 21), 0.5))
    assert _fetch(root, dhan) == EXIT_PREVIEW_PARTIAL
    assert root.cache.path_for("A").read_bytes() == before  # untouched
    text = _preview(root).read_text()
    assert "FETCH FAILED: A — held" in text
    positions = text.split("## 4.")[1].split("## 5.")[0]
    assert "| A |" in positions and "no bar 1 week(s)" in positions


def test_r6_1_a_successful_full_refetch_replaces_the_restated_file(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path)
    _warm(root, dhan, W38_END)
    dhan.restated.append(("A", date(2026, 9, 21), 0.5))
    assert _fetch(root, dhan) == EXIT_OK
    assert root.cache.read("A") == dhan.truth("A")


# ===================================================================== R6-3
def test_r6_3_a_fetch_for_an_earlier_week_never_shortens_a_cache(tmp_path: Path) -> None:
    """The audit repro: --as-of 2026-06-12 in a full-refetch month. Before,
    every file was cut to 12 Jun."""
    root, dhan = _setup(tmp_path)
    _warm(root, dhan, FRIDAY, stamp="2026-08-01T08:00:00+05:30")  # full refetch due
    before = _sha_tree(root.cache.root)
    for flags in ((), ("--force-refetch",)):
        assert _fetch(root, dhan, "--as-of", "2026-06-12", *flags) == EXIT_OK
        assert dhan.requests == []
        assert _sha_tree(root.cache.root) == before
    assert _preview(root, date(2026, 6, 12)).is_file()


# ===================================================================== R6-4
@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("paper", "paper mode only"),
        ("preflight", "validate_environment: x"),
        ("input", "universe.csv"),
        ("auth", "cannot authenticate"),
        ("scrip", "scrip master"),
        ("runtime_off", "runtime positional_stocks is disabled in config"),
        ("strategy_off", "strategy wsr1_weekly_stochrsi is disabled in config"),
    ],
)
def test_r6_4_every_fetch_refusal_writes_a_report_and_alerts(
    tmp_path: Path, setup: str, reason: str
) -> None:
    root, dhan = _setup(tmp_path)
    overrides: dict[str, Any] = {}
    if setup == "paper":
        root.config = RunConfig(mode="live", runtime_enabled=True, strategy_enabled=True)
    elif setup == "preflight":
        overrides["preflight"] = lambda: ["validate_environment: x"]
    elif setup == "input":
        (root.path / "config" / "positional_stocks" / "universe.csv").write_text("bad\n")
    elif setup == "auth":

        def fail(minimum: float, allow: bool) -> Credentials:
            raise FetchRefused("cannot authenticate (InvalidCredentialsError): bad PIN")

        overrides["fetch_services"] = dhan.services(auth=fail)
    elif setup == "scrip":
        overrides["fetch_services"] = replace(dhan.services(), scrip_master_text=lambda: "x\n")
    elif setup == "runtime_off":
        root.config = replace(root.config, runtime_enabled=False)
    elif setup == "strategy_off":
        root.config = replace(root.config, strategy_enabled=False)
    assert _fetch(root, dhan, **overrides) == EXIT_REFUSED
    refused = [line for line in root.output if line.startswith("REFUSED")]
    assert refused and reason in refused[-1]
    report = (root.reports / f"{FRIDAY}-preview-failed.md").read_text()
    assert "REFUSED (fetch)" in report and reason in report
    (event,) = _events(root)
    assert event.event_type == "weekly_run_no_trades" and "REFUSED" in event.message
    assert not _preview(root).exists()


def test_r6_4_a_writing_decide_refusal_writes_refused_md_and_a_dry_run_does_not(
    tmp_path: Path,
) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    root.seed_entry()
    (root.path / "config" / "positional_stocks" / "universe.csv").write_text("bad\n")
    assert root.run("--as-of", root.as_of(1), "--dry-run") == EXIT_REFUSED
    assert _events(root) == [] and not root.reports.exists()
    assert root.run("--as-of", root.as_of(1)) == EXIT_REFUSED
    assert (root.reports / f"{root.last_session(1)}-refused.md").is_file()
    assert not (root.reports / f"{root.last_session(1)}.md").exists()
    assert len(_events(root)) == 1


# ========================================================== enabled (D115)
@pytest.mark.parametrize(
    ("runtime", "strategy", "argv", "code", "message"),
    [
        (False, True, (), EXIT_REFUSED, "runtime positional_stocks is disabled in config"),
        (True, False, (), EXIT_REFUSED, "strategy wsr1_weekly_stochrsi is disabled in config"),
        (True, True, (), EXIT_OK, None),
        (False, False, ("--dry-run",), EXIT_OK, None),
    ],
)
def test_both_enabled_flags_gate_a_writing_decide_run(
    tmp_path: Path, runtime: bool, strategy: bool, argv: tuple[str, ...], code: int, message: str
) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    root.seed_entry()
    root.config = RunConfig(runtime_enabled=runtime, strategy_enabled=strategy)
    assert root.run("--as-of", root.as_of(1), *argv) == code
    if message is not None:
        (line,) = [x for x in root.output if x.startswith("REFUSED")]
        assert line == f"REFUSED: {message}"


def test_the_replay_runs_with_both_flags_false(tmp_path: Path) -> None:
    from _stock_run_fixtures import falling

    from runtimes.positional_stocks import replay

    source = Root.create(tmp_path / "source")
    source.standard_cache(a=falling(source.first_session(2)))
    workdir = replay.prepare_workdir(tmp_path / "work", source=source.path)
    evening = datetime.combine(source.last_session(2), time(20, 0), IST)
    summaries = replay.replay(
        workdir, weeks=2, now=lambda: evening, out=lambda _: None, config=RunConfig()
    )
    assert [s.exit_code for s in summaries] == [EXIT_OK, EXIT_OK]


# ================================================================ watchlist
def test_the_watchlist_ignores_the_quality_gate_but_screen_does_not() -> None:
    armed = kd_tape([(15.0, 18.0), (20.0, 22.0), (25.0, 28.0)]).series()
    triggered = kd_tape([(15.0, 18.0), (20.0, 22.0), (28.0, 25.0)]).series()
    fail = quality("F", QualityStatus.FAIL, valid_until=date(2099, 1, 1))
    symbols = {
        "M": symbol_week("M", armed, quality_row=None),
        "F": symbol_week("F", armed, quality_row=fail),
        "P": symbol_week("P", armed),
    }
    entries = watchlist(symbols, index_series(), book(), ctx(), PARAMS0)
    assert {e.symbol: (e.quality, e.valid_until) for e in entries} == {
        "F": ("FAIL", date(2099, 1, 1)),
        "M": ("missing", None),
        "P": ("PASS", symbols["P"].quality.valid_until),  # type: ignore[union-attr]
    }
    # screen() still refuses a trigger without a valid PASS/EVENT_RISK row.
    for row in (None, fail):
        verdict = screen(
            symbol_week("T", triggered, quality_row=row), index_series(), book(), ctx(), PARAMS0
        )
        assert getattr(verdict, "stage", None) is FunnelStage.FILTERED
        assert "needs quality check" in verdict.reason  # type: ignore[union-attr]
    assert not hasattr(
        screen(symbol_week("T", triggered), index_series(), book(), ctx(), PARAMS0), "stage"
    )


def test_the_report_shows_the_quality_status_and_flags_an_expired_row(tmp_path: Path) -> None:
    from dataclasses import replace as dc_replace

    from test_stock_report import _capture, _section

    from runtimes.positional_stocks.report import render
    from strategies.positional_stocks.wsr1_weekly_stochrsi.models import WatchEntry

    root = Root.create(tmp_path)
    root.standard_cache()
    root.seed_entry()
    monkey = pytest.MonkeyPatch()
    try:
        data = _capture(root, monkey, 1)
    finally:
        monkey.undo()
    data = dc_replace(
        data,
        today=date(2026, 9, 27),
        watchlist=(
            WatchEntry("C", 3.25, 22.5, 24.0, "EVENT_RISK", date(2026, 9, 1)),
            WatchEntry("D", 1.0, 20.0, 21.0),
        ),
    )
    section = _section(render(data), 7)
    assert "every filter except the quality gate" in section
    assert "| 1 | C | +3.25 pp | 22.50 | 24.00 | EVENT_RISK | 2026-09-01 (expired) |" in section
    assert "| 2 | D | +1.00 pp | 20.00 | 21.00 | missing | — |" in section


# ======================================================== sizing text (4.8)
def test_a_tranche_below_one_share_is_skipped_with_the_v1_3_reason() -> None:
    size = sizing(0.06, False, PARAMS0)
    order = PendingOrder(
        action=OrderAction.BUY_T1,
        symbol="MRF",
        decided_week=(2026, 38),
        execute_on_or_after=date(2026, 9, 21),
        reason="entry",
        amount=size.tranche_amounts[0],
        sizing=size,
        sector="Tyres",
        group="MRF",
    )
    outcome = apply_fill(
        order,
        None,
        session=date(2026, 9, 21),
        open_price=150_000.0,
        params=PARAMS0,
        at_week_open=True,
    )
    assert outcome.skipped == "one share costs more than the tranche; skipped"


# ========================================= "not yet published": final attempt
@pytest.mark.parametrize(
    ("moment", "final"),
    [
        (datetime(2026, 9, 25, 20, 0, tzinfo=IST), False),  # Friday evening, manual
        (datetime(2026, 9, 26, 8, 0, 5, tzinfo=IST), False),  # Saturday 08:00
        (datetime(2026, 9, 26, 14, 0, 5, tzinfo=IST), False),  # Saturday 14:00
        (datetime(2026, 9, 27, 10, 0, 5, tzinfo=IST), True),  # Sunday 10:00
        (datetime(2026, 9, 28, 9, 0, tzinfo=IST), True),  # Monday, manual
    ],
)
def test_only_the_final_fetch_attempt_of_the_schedule_is_final(
    moment: datetime, final: bool
) -> None:
    assert Schedule().is_final_fetch_attempt(moment, FRIDAY) is final


def test_a_holiday_friday_week_counts_its_attempts_from_the_thursday() -> None:
    thursday = date(2026, 4, 2)  # Good Friday week
    assert not Schedule().is_final_fetch_attempt(datetime(2026, 4, 3, 18, tzinfo=IST), thursday)
    assert Schedule().is_final_fetch_attempt(datetime(2026, 4, 5, 10, 1, tzinfo=IST), thursday)


def test_not_yet_published_alerts_on_the_final_attempt(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path, missing={"NIFTY": {FRIDAY}})
    sunday = datetime(2026, 9, 27, 10, 0, 5, tzinfo=IST)
    assert _fetch(root, dhan, now=lambda: sunday) == EXIT_NO_TRADES
    (event,) = _events(root)
    assert "not yet published" in event.message


# ============================================================ token safety
def _calendar(root: Root) -> TradingCalendar:
    return TradingCalendar.from_config(root.path / "config")


@pytest.mark.parametrize(
    ("moment", "blocked"),
    [
        (datetime(2026, 9, 24, 8, 29, tzinfo=IST), False),
        (datetime(2026, 9, 24, 8, 30, tzinfo=IST), True),
        (datetime(2026, 9, 24, 10, 0, tzinfo=IST), True),
        (datetime(2026, 9, 24, 16, 0, tzinfo=IST), True),
        (datetime(2026, 9, 24, 16, 1, tzinfo=IST), False),
        (datetime(2026, 9, 26, 10, 0, tzinfo=IST), False),  # Saturday
        (datetime(2026, 10, 2, 10, 0, tzinfo=IST), False),  # a holiday Friday
    ],
)
def test_no_login_inside_a_trading_days_session_window(
    tmp_path: Path, moment: datetime, blocked: bool
) -> None:
    root = Root.create(tmp_path)
    assert (login_block(root.env(), _calendar(root), moment) is not None) is blocked


def _supervisor_pid_file(root: Root, runtime_id: str, *, alive: bool) -> None:
    (root.path / "config" / "runtimes").mkdir(parents=True, exist_ok=True)
    (root.path / "config" / "runtimes" / f"{runtime_id}.yaml").write_text(
        f"runtime_id: {runtime_id}\n"
    )
    pids = root.path / "data" / "runtime" / "pid"
    pids.mkdir(parents=True, exist_ok=True)
    pid = os.getpid() if alive else 999_999
    create = psutil.Process().create_time() if alive else 1.0
    (pids / f"{runtime_id}.supervisor.pid").write_text(
        json.dumps(
            {
                "pid": pid,
                "identity": f"{runtime_id}.supervisor",
                "command": "test",
                "acquired_at": "2026-09-26T08:00:00+05:30",
                "create_time": create,
            }
        )
    )


@pytest.mark.parametrize("alive", [True, False])
def test_a_running_supervisor_defers_the_login_and_the_check_is_read_only(
    tmp_path: Path, alive: bool
) -> None:
    root = Root.create(tmp_path)
    _supervisor_pid_file(root, "intraday_options", alive=alive)
    locks = root.path / "data" / "runtime" / "locks"
    pids = root.path / "data" / "runtime" / "pid"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "intraday_options.supervisor.lock").write_text("")
    before = (_sha_tree(locks), _sha_tree(pids), sorted(os.listdir(locks)))
    saturday = datetime(2026, 9, 26, 8, 0, tzinfo=IST)
    block = login_block(root.env(), _calendar(root), saturday)
    assert (block is not None) is alive
    if alive:
        assert "the intraday_options supervisor is running" in block  # type: ignore[operator]
    assert (_sha_tree(locks), _sha_tree(pids), sorted(os.listdir(locks))) == before


def test_a_weekday_fetch_passes_allow_login_false_and_refuses_as_deferred(
    tmp_path: Path,
) -> None:
    root, dhan = _setup(tmp_path)
    seen: list[bool] = []

    def deferred(minimum: float, allow: bool) -> Credentials:
        seen.append(allow)
        raise FetchRefused("login deferred: no environment or cached token with 25 minutes")

    weekday = datetime(2026, 9, 24, 10, 0, tzinfo=IST)
    code = _fetch(root, dhan, now=lambda: weekday, fetch_services=dhan.services(auth=deferred))
    assert code == EXIT_REFUSED and seen == [False]
    (line,) = [x for x in root.output if x.startswith("REFUSED")]
    assert "login deferred" in line and "session window" in line
    assert dhan.requests == []
    assert (root.reports / f"{W38_END}-preview-failed.md").is_file()
    assert len(_events(root)) == 1


def _jwt(exp: float) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'HS256'})}.{part({'exp': int(exp)})}.sig"


class _AuthPost:
    """The HTTP seam of ``DhanTotpLogin``: records, never reaches Dhan."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str, **kw: object) -> httpx.Response:
        self.calls.append(url)
        return httpx.Response(401, json={"message": "invalid"})


def test_a_deferred_login_makes_no_call_to_the_auth_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    post = _AuthPost()
    monkeypatch.setattr(httpx, "post", post)
    TokenCache(tmp_path / "token_cache.json").save(_jwt(_time.time() - 60), client_id="C1")
    credentials = AuthCredentials("C1", pin="1234", totp_secret="JBSWY3DPEHPK3PXP")
    with pytest.raises(FetchRefused, match="login deferred"):
        authenticate_with(credentials, cache_dir=tmp_path, minimum_seconds=1500, allow_login=False)
    assert post.calls == []
    # The control: allowed, the same bootstrap does reach the (faked) endpoint.
    with pytest.raises(FetchRefused, match="cannot authenticate"):
        authenticate_with(credentials, cache_dir=tmp_path, minimum_seconds=1500, allow_login=True)
    assert len(post.calls) == 1 and "auth.dhan.co" in post.calls[0]


def test_a_deferred_login_still_uses_a_cached_token_with_enough_life(tmp_path: Path) -> None:
    token = _jwt(_time.time() + 3600)
    TokenCache(tmp_path / "token_cache.json").save(token, client_id="C1")
    got = authenticate_with(
        AuthCredentials("C1", pin="1234", totp_secret="JBSWY3DPEHPK3PXP"),
        cache_dir=tmp_path,
        minimum_seconds=1500,
        allow_login=False,
    )
    assert got.access_token == token and got.source == "cache"
