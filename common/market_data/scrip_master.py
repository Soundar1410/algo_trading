"""Dhan instrument-master loader for index options.

Ported from the reference repository's ``framework/market_data/scrip_master.py``
(Phase 4 Part 1). This is what turns a strike/expiry the engine *chose* into a
contract the broker actually recognises, closing runbook limitation 17.

Why the CSV rather than the Option Chain API
--------------------------------------------
Runbook limitation 17 and section 8 both said the fix was "an
``OptionChainResolver`` backed by :class:`~common.market_data.option_chain.OptionChainService`".
**That cannot work.** Dhan's ``/v2/optionchain`` response is keyed by strike and
carries prices, open interest and greeks — it has no per-strike ``security_id``,
so it cannot name a tradable contract. The reference reached the same conclusion
and recorded the reasoning its docstring still carries: the daily instrument
master is "more reliable than the rate-limited Option Chain API and works outside
market hours". Resolution here is a dict lookup with **no per-trade API call**.

``OptionChainService`` keeps its real job — live per-strike quotes and greeks —
and is untouched by this module.

What was left behind, and what Phase 1 brought over (D34)
---------------------------------------------------------
The reference's ``EquityScripMaster`` was originally **not** ported: intraday
stocks were Phase 5, nothing here consumed it, and porting it would have added
an unexercised parser — the same judgement Phase 3 Part 2a made about the five
unported indicators.

``wsr1_weekly_stochrsi`` (``positional_stocks``, Phase 1) is that consumer, so
**the cash-equity half is now ported** as :class:`EquityScripMaster`, together
with :func:`resolve_index` for the NIFTY 50 spot id the same strategy needs.
D34's own test is what decides the boundary, so it also decides what stays out:

* the reference's **F&O half** — ``fno_lot_size``, ``fno_equities`` and the
  ``FUTSTK``/``OPTSTK`` underlying extraction — is still not ported. This
  strategy trades NSE cash equity only and never asks what has a derivative;
* the reference let a **blank** ``SEM_SERIES`` through (``series and series !=
  "EQ"``). Spec section 5 says "NSE, series EQ", so this tightens to a strict
  equality. Verified against the 2026-09-21 master: the strict filter yields
  2,690 distinct symbols with **no duplicate**, and resolves all 200 NIFTY 200
  constituents including ``BAJAJ-AUTO`` and ``M&M``.

Equity rows are ``SEM_INSTRUMENT_NAME=EQUITY`` on ``SEM_SEGMENT=E``; the spot
index rows are ``SEM_INSTRUMENT_NAME=INDEX`` on ``SEM_SEGMENT=I``, which is the
``IDX_I`` feed segment. NIFTY 50 is one unambiguous row on the 2026-09-21
master (``SEM_TRADING_SYMBOL=NIFTY``, ``SEM_CUSTOM_SYMBOL=Nifty 50``,
security id ``13``) — and ``13`` is what :data:`INDEX_REGISTRY` already
carries, which a test asserts as agreement rather than this module
hard-coding it.

Adaptations from the reference, each deliberate
-----------------------------------------------
* ``urllib.request.urlopen`` → ``httpx``, matching the Phase 2 authentication
  port and giving one HTTP client across the repository.
* Download is separated from parsing and both are injectable, so **no default
  test needs a network**: :meth:`ScripMaster.load_from_text` takes CSV text and
  :class:`ScripMasterCache` serves a day-stamped local copy.
* ``nearest_expiry`` used a naive ``datetime.now().date()``. It now resolves
  "today" in IST via :func:`common.utils.timeutils.now_ist`, because an expiry
  decision taken at 23:30 UTC must already be tomorrow's in Mumbai.

Verified column names (``api-scrip-master.csv``): ``SEM_SMST_SECURITY_ID``,
``SEM_INSTRUMENT_NAME`` (``OPTIDX`` for index options), ``SEM_TRADING_SYMBOL``
(``"NIFTY-Jul2026-29300-CE"``), ``SEM_CUSTOM_SYMBOL``, ``SEM_EXPIRY_DATE``
(``"YYYY-MM-DD HH:MM:SS"``), ``SEM_STRIKE_PRICE``, ``SEM_OPTION_TYPE``,
``SEM_LOT_UNITS``, ``SEM_EXM_EXCH_ID`` (``NSE``/``BSE``), ``SEM_SEGMENT`` (``D``).
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from common.logging import get_logger
from common.models import OptionType
from common.utils.timeutils import now_ist

_log = get_logger(__name__)

#: Dhan's public daily instrument master. Contains no credential and is not
#: account-specific, so the cached copy below is ordinary data, not a secret.
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

#: Index options carry this instrument name. Futures (``FUTIDX``) and the equity
#: universe (``EQUITY``/``OPTSTK``/``FUTSTK``) are ignored by :class:`ScripMaster`.
_INDEX_OPTION_INSTRUMENT = "OPTIDX"


class ScripMasterError(RuntimeError):
    """The instrument master could not be fetched or contained no usable rows."""


@dataclass(frozen=True)
class IndexMeta:
    """Identifiers needed to stream an index and resolve its options.

    The spot index and its options live in **different exchange segments**, which
    is why both are recorded here: subscribing an option on the index's segment
    silently returns nothing. See :data:`SEGMENT_CODES`.
    """

    #: Broker id of the spot index itself, used for LTP and the underlying feed.
    security_id: str
    #: Feed segment for the spot index, e.g. ``"IDX_I"``.
    segment: str
    #: Feed segment for its options, e.g. ``"NSE_FNO"`` / ``"BSE_FNO"``.
    fno_segment: str
    #: Listing exchange, used to filter master rows: ``"NSE"`` / ``"BSE"``.
    exchange: str


#: Best-known values from Dhan's annexure. Verify an entry against the live
#: broker before trading a new underlying — the smoke suite is where that goes.
INDEX_REGISTRY: dict[str, IndexMeta] = {
    "NIFTY": IndexMeta(security_id="13", segment="IDX_I", fno_segment="NSE_FNO", exchange="NSE"),
    "BANKNIFTY": IndexMeta(
        security_id="25", segment="IDX_I", fno_segment="NSE_FNO", exchange="NSE"
    ),
    "SENSEX": IndexMeta(security_id="51", segment="IDX_I", fno_segment="BSE_FNO", exchange="BSE"),
}

#: Numeric MarketFeed exchange-segment codes — stable WebSocket protocol
#: constants, and the reason a single adapter-wide segment cannot serve an
#: options runtime: the underlying is ``0`` while its options are ``2``.
SEGMENT_CODES: dict[str, int] = {
    "IDX_I": 0,
    "NSE_EQ": 1,
    "NSE_FNO": 2,
    "NSE_CURRENCY": 3,
    "BSE_EQ": 4,
    "MCX_COMM": 5,
    "BSE_CURRENCY": 7,
    "BSE_FNO": 8,
}


def _tick_size_in_rupees(raw: object) -> float | None:
    """Convert ``SEM_TICK_SIZE`` from paise to rupees, or ``None`` if unusable.

    See :data:`TICK_SIZE_PAISE_PER_RUPEE` for the evidence that the column is in
    paise, and for why the result is advisory rather than authoritative. A missing
    or unparseable value returns ``None`` rather than a default, because "we do
    not know this instrument's tick" and "its tick is 0.05" must stay
    distinguishable — the fill model skips its tick rule for the first and
    enforces it for the second.
    """
    if raw is None or isinstance(raw, bool) or not isinstance(raw, str | int | float):
        return None
    try:
        paise = float(raw)
    except (TypeError, ValueError):
        return None
    if paise <= 0:
        return None
    return paise / TICK_SIZE_PAISE_PER_RUPEE


def segment_code(segment: str) -> int:
    """Map a named feed segment to its numeric code.

    Raises:
        KeyError: on an unknown segment. Deliberately not defaulted — guessing
            a segment subscribes to an instrument that does not exist there and
            fails by delivering silence, which is the hardest failure to notice.
    """
    try:
        return SEGMENT_CODES[segment]
    except KeyError:
        raise KeyError(
            f"Unknown exchange segment {segment!r}. Known: {sorted(SEGMENT_CODES)}."
        ) from None


def resolve_index_meta(
    underlying: str,
    *,
    index_security_id: str | None = None,
    index_segment: str | None = None,
    fno_segment: str | None = None,
) -> IndexMeta:
    """Resolve an underlying's metadata, honouring explicit config overrides.

    An underlying absent from :data:`INDEX_REGISTRY` is still tradable by
    supplying all three overrides, so a new index needs configuration rather than
    a code change.
    """
    base = INDEX_REGISTRY.get(underlying.upper())
    if base is None and not (index_security_id and index_segment and fno_segment):
        raise ValueError(
            f"Unknown underlying {underlying!r} and no explicit index_security_id/"
            "index_segment/fno_segment override provided in config."
        )
    return IndexMeta(
        security_id=index_security_id or (base.security_id if base else ""),
        segment=index_segment or (base.segment if base else "IDX_I"),
        fno_segment=fno_segment or (base.fno_segment if base else "NSE_FNO"),
        exchange=base.exchange if base else "NSE",
    )


#: What ``SEM_TICK_SIZE`` is denominated in. **Paise, not rupees** — verified
#: against the live master in Phase 4 Part 5: NIFTY and SENSEX ``OPTIDX`` rows
#: carry ``5.0000`` for a real ₹0.05 tick, and ``FUTCUR`` USDINR carries
#: ``0.2500`` for a real ₹0.0025 one.
#:
#: **The unit is not uniformly trustworthy outside index options.** In the same
#: file ``FUTIDX`` NIFTY and NSE ``EQUITY`` RELIANCE both carry ``10.0000``,
#: neither of which divides to the tick those instruments are commonly quoted in.
#: So the value is parsed and converted here, and the paper fill model treats it
#: as *advisory*: the tick it enforces comes from configuration, and a
#: disagreement is a warning rather than a rejection. Taking the column at face
#: value as rupees would put NIFTY options on a ₹5 grid and refuse every order.
TICK_SIZE_PAISE_PER_RUPEE = 100.0


@dataclass(frozen=True)
class OptionRow:
    """One tradable index-option contract as the master describes it."""

    security_id: str
    strike: float
    option_type: OptionType
    expiry: str  # "YYYY-MM-DD"
    lot_size: int
    symbol: str  # human-readable (SEM_CUSTOM_SYMBOL)
    #: Minimum price increment **in rupees**, converted from the master's paise.
    #: ``None`` when the column is absent or unparseable, which the fill model
    #: treats as "no tick known" and skips the rule rather than guessing.
    tick_size: float | None = None


# --------------------------------------------------------------------- transport
def fetch_scrip_master_text(*, url: str = SCRIP_MASTER_URL, timeout: float = 60.0) -> str:
    """Download the instrument master and return decoded CSV text.

    Separated from parsing so every default test can exercise the parser with no
    network. The only caller that reaches the network is
    :meth:`ScripMasterCache.text`, and only on a cache miss.
    """
    import httpx  # deferred: keeps the import off the worker's spawn path

    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ScripMasterError(
            f"Could not download the Dhan scrip master from {url}: {exc}"
        ) from exc
    return response.text


class ScripMasterCache:
    """A day-stamped local copy of the instrument master.

    The master is republished daily and is several megabytes, so refetching it
    per process start is both wasteful and a startup failure mode: a worker
    restarting mid-session would depend on an HTTP call succeeding before it
    could resolve a contract it already holds. The cache is keyed by IST date, so
    a new trading day always refetches and a same-day restart never does.

    Stored under ``data/cache/`` (already gitignored). It carries no credential —
    the master is a public instrument list — so no secret-file mode is applied
    and none is implied.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        today: Callable[[], date] = lambda: now_ist().date(),
        fetcher: Callable[[], str] = fetch_scrip_master_text,
    ) -> None:
        self._dir = Path(cache_dir)
        self._today = today
        self._fetcher = fetcher

    def path_for(self, day: date) -> Path:
        return self._dir / f"dhan_scrip_master_{day.isoformat()}.csv"

    def cached_text(self) -> str | None:
        """Today's copy if it exists and is non-empty, else ``None``."""
        path = self.path_for(self._today())
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="ignore")
        return text or None

    def text(self) -> str:
        """Today's master, from cache when possible and the network otherwise."""
        cached = self.cached_text()
        if cached is not None:
            _log.debug("scrip master served from cache %s", self.path_for(self._today()))
            return cached
        _log.info("downloading Dhan scrip master")
        fetched = self._fetcher()
        self._write(fetched)
        return fetched

    def _write(self, text: str) -> None:
        """Write today's copy atomically, so a crash cannot leave a partial CSV.

        A truncated master is worse than an absent one: it parses cleanly and
        resolves *some* strikes, so the failure would surface as an unexplained
        ``KeyError`` on one contract rather than as a fetch error at startup.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(self._today())
        fd, tmp_name = tempfile.mkstemp(dir=self._dir, prefix=".scrip_master_", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def prune(self, *, keep: int = 3) -> int:
        """Delete all but the newest ``keep`` cached masters. Returns how many went."""
        existing = sorted(self._dir.glob("dhan_scrip_master_*.csv"))
        doomed = existing[:-keep] if keep > 0 else existing
        for path in doomed:
            path.unlink(missing_ok=True)
        return len(doomed)


# ------------------------------------------------------------------------ parsing
class ScripMaster:
    """Loads and indexes index-option contracts from Dhan's master CSV."""

    def __init__(self, underlying: str, *, exchange: str | None = None) -> None:
        self._underlying = underlying.upper()
        meta = INDEX_REGISTRY.get(self._underlying)
        self._exchange = (exchange or (meta.exchange if meta else "NSE")).upper()
        self._by_key: dict[tuple[str, float, OptionType], OptionRow] = {}
        #: The same rows indexed the way the *broker* asks for them. An order
        #: carries a ``security_id`` and nothing else, so the strike/expiry key
        #: cannot answer "what are this instrument's exchange rules?".
        self._by_security_id: dict[str, OptionRow] = {}
        self._expiries: list[str] = []
        self._lot_size: int | None = None

    @property
    def underlying(self) -> str:
        return self._underlying

    @property
    def exchange(self) -> str:
        return self._exchange

    # ---------------------------------------------------------------- loading
    def load_from_text(self, text: str) -> ScripMaster:
        """Parse master CSV text. The only parsing entry point.

        Rows are skipped rather than fatal when individually unusable (an
        unparseable strike, an option type that is neither CE nor PE): one bad
        row in a multi-megabyte daily file must not cost the whole session. An
        empty *result*, on the other hand, does raise — that means the filters
        matched nothing, which is a configuration error wearing a data error's
        clothes.
        """
        self._by_key.clear()
        self._by_security_id.clear()
        expiries: set[str] = set()
        skipped = 0

        for row in csv.DictReader(io.StringIO(text)):
            if (row.get("SEM_INSTRUMENT_NAME") or "").upper() != _INDEX_OPTION_INSTRUMENT:
                continue
            raw_type = (row.get("SEM_OPTION_TYPE") or "").strip().upper()
            if raw_type not in ("CE", "PE"):
                continue

            symbol = row.get("SEM_TRADING_SYMBOL") or ""
            # Split on the first hyphen only: the underlying is the leading
            # token, and comparing the whole token stops NIFTYNXT50 rows from
            # matching a NIFTY master through a shared prefix.
            if symbol.split("-", 1)[0].upper() != self._underlying:
                continue

            exch = (row.get("SEM_EXM_EXCH_ID") or "").upper()
            if exch and exch != self._exchange:
                continue  # an NSE underlying must never match a BSE row

            expiry = (row.get("SEM_EXPIRY_DATE") or "")[:10]
            try:
                strike = float(row["SEM_STRIKE_PRICE"])
                lot = int(float(row["SEM_LOT_UNITS"]))
            except (KeyError, TypeError, ValueError):
                skipped += 1
                continue

            option_row = OptionRow(
                security_id=str(row["SEM_SMST_SECURITY_ID"]),
                strike=strike,
                option_type=OptionType(raw_type),
                expiry=expiry,
                lot_size=lot,
                symbol=row.get("SEM_CUSTOM_SYMBOL") or symbol,
                tick_size=_tick_size_in_rupees(row.get("SEM_TICK_SIZE")),
            )
            self._by_key[(expiry, strike, option_row.option_type)] = option_row
            self._by_security_id[option_row.security_id] = option_row
            expiries.add(expiry)
            self._lot_size = lot

        self._expiries = sorted(expiries)
        if not self._by_key:
            raise ScripMasterError(
                f"No {self._underlying} {_INDEX_OPTION_INSTRUMENT} contracts found in the "
                f"scrip master (exchange={self._exchange})."
            )
        if skipped:
            _log.warning(
                "scrip master: skipped %d unparseable %s row(s)", skipped, self._underlying
            )
        _log.info(
            "scrip master loaded: %d %s contracts across %d expiries (lot=%s)",
            len(self._by_key),
            self._underlying,
            len(self._expiries),
            self._lot_size,
        )
        return self

    def load(self, *, cache: ScripMasterCache) -> ScripMaster:
        """Load today's master through ``cache``, fetching only on a miss."""
        return self.load_from_text(cache.text())

    # ---------------------------------------------------------------- queries
    @property
    def lot_size(self) -> int | None:
        """Lot size as the *exchange* states it, not as configuration guesses it."""
        return self._lot_size

    @property
    def expiries(self) -> list[str]:
        return list(self._expiries)

    def nearest_expiry(self, on: date | None = None) -> str:
        """The soonest expiry on or after ``on`` — today in IST by default."""
        reference = on or now_ist().date()
        for expiry in self._expiries:
            if datetime.strptime(expiry, "%Y-%m-%d").date() >= reference:
                return expiry
        raise ScripMasterError(
            f"Every listed {self._underlying} expiry is before {reference.isoformat()} "
            f"(newest is {self._expiries[-1]}). The scrip master is stale."
        )

    def get(self, strike: float, option_type: OptionType, expiry: str) -> OptionRow | None:
        return self._by_key.get((expiry, float(strike), option_type))

    def by_security_id(self, security_id: str) -> OptionRow | None:
        """The contract with this broker id, or ``None`` if the master has none.

        ``None`` is what makes the paper broker's ``INVALID_INSTRUMENT`` rule
        meaningful: an order for an id the exchange's own daily master does not
        list is an order that could not have been placed.
        """
        return self._by_security_id.get(str(security_id))

    def strikes_for_expiry(self, expiry: str) -> list[float]:
        return sorted({key[1] for key in self._by_key if key[0] == expiry})

    def tick_sizes(self) -> frozenset[float]:
        """Every distinct tick size across the loaded contracts. For assertions
        and for the startup log — a series whose rows disagree is worth seeing."""
        return frozenset(
            row.tick_size for row in self._by_key.values() if row.tick_size is not None
        )

    def atm_band(
        self, spot: float, expiry: str, strike_step: int, half_width: int
    ) -> list[OptionRow]:
        """All CE/PE rows within ``half_width`` strikes of ATM.

        Used to pre-subscribe a band around the money so the contract the
        strategy eventually picks is already streaming when it enters — which
        also sidesteps limitation 15, where a runtime subscription needs a tick
        before it is applied.
        """
        if strike_step <= 0:
            raise ValueError("strike_step must be positive")
        atm = round(spot / strike_step) * strike_step
        rows: list[OptionRow] = []
        for offset in range(-half_width, half_width + 1):
            strike = atm + offset * strike_step
            for option_type in (OptionType.CE, OptionType.PE):
                row = self.get(strike, option_type, expiry)
                if row is not None:
                    rows.append(row)
        return rows


# ---------------------------------------------------------------- cash equities
#: NSE cash-equity rows carry this instrument name. ``SEM_SERIES`` then
#: separates the tradable ``EQ`` series from ``BE``/``SM``/``SG``/``GS`` and the
#: rest, which this strategy's universe never contains.
_EQUITY_INSTRUMENT = "EQUITY"
_EQUITY_SERIES = "EQ"

#: Spot index rows (``SEM_SEGMENT`` ``I``), as opposed to ``OPTIDX``/``FUTIDX``.
_INDEX_INSTRUMENT = "INDEX"

#: Feed segment for NSE cash equity, matching :data:`SEGMENT_CODES`.
NSE_EQUITY_SEGMENT = "NSE_EQ"
#: Feed segment for a spot index.
INDEX_SEGMENT = "IDX_I"


@dataclass(frozen=True)
class EquityRow:
    """One NSE cash-equity instrument as the master describes it."""

    security_id: str
    symbol: str
    company_name: str | None = None
    exchange_segment: str = NSE_EQUITY_SEGMENT
    #: Minimum price increment in rupees, converted from the master's paise —
    #: and **advisory only** here, for the same reason :class:`OptionRow`'s is.
    #: The equity rows are exactly where :data:`TICK_SIZE_PAISE_PER_RUPEE`'s
    #: docstring records the column as untrustworthy: RELIANCE carries
    #: ``10.0000``, which does not divide to the ₹0.05 it actually trades in.
    tick_size: float | None = None


@dataclass(frozen=True)
class IndexRow:
    """One spot index — the id and segment needed to fetch its own history."""

    security_id: str
    symbol: str
    name: str
    exchange_segment: str = INDEX_SEGMENT


def _normalise_symbol(value: str | None) -> str:
    """Upper-case and strip, without destroying valid punctuation.

    ``BAJAJ-AUTO`` and ``M&M`` are real NSE symbols, so nothing here strips a
    hyphen or an ampersand — the reference learned the same lesson.
    """
    return (value or "").strip().upper()


class EquityScripMaster:
    """Indexes NSE cash equities (series ``EQ``) from Dhan's master CSV.

    Parsing is separated from downloading exactly as :class:`ScripMaster` does,
    so every default test exercises the parser with no network, and
    :meth:`load` reuses the same day-stamped :class:`ScripMasterCache`.

    Unlike the reference this is ported from, it knows nothing about stock
    derivatives — see the module docstring for why that half stayed behind.
    """

    def __init__(self, *, exchange: str = "NSE") -> None:
        self._exchange = exchange.upper()
        self._by_symbol: dict[str, EquityRow] = {}

    # ---------------------------------------------------------------- loading
    def load_from_text(self, text: str) -> EquityScripMaster:
        """Parse master CSV text. The only parsing entry point.

        Individually unusable rows are skipped rather than fatal, matching
        :meth:`ScripMaster.load_from_text`: one bad row in a multi-megabyte
        daily file must not cost the whole run. An empty *result* does raise —
        that means the filters matched nothing, which is a configuration error
        wearing a data error's clothes.
        """
        self._by_symbol.clear()
        skipped = 0

        for row in csv.DictReader(io.StringIO(text)):
            if (row.get("SEM_INSTRUMENT_NAME") or "").upper() != _EQUITY_INSTRUMENT:
                continue
            if (row.get("SEM_EXM_EXCH_ID") or "").upper() != self._exchange:
                continue
            # Strict equality, not the reference's "blank or EQ" — spec
            # section 5 says series EQ, and BE/SM/SG rows are not this
            # universe's instruments.
            if (row.get("SEM_SERIES") or "").upper() != _EQUITY_SERIES:
                continue

            symbol = _normalise_symbol(row.get("SEM_TRADING_SYMBOL"))
            security_id = str(row.get("SEM_SMST_SECURITY_ID") or "").strip()
            if not symbol or not security_id:
                skipped += 1
                continue

            self._by_symbol[symbol] = EquityRow(
                security_id=security_id,
                symbol=symbol,
                company_name=(row.get("SEM_CUSTOM_SYMBOL") or "").strip() or None,
                tick_size=_tick_size_in_rupees(row.get("SEM_TICK_SIZE")),
            )

        if not self._by_symbol:
            raise ScripMasterError(
                f"No {self._exchange} cash-equity rows (series {_EQUITY_SERIES}) found in the "
                "scrip master."
            )
        if skipped:
            _log.warning("scrip master: skipped %d unusable cash-equity row(s)", skipped)
        _log.info("equity scrip master loaded: %d %s symbols", len(self._by_symbol), self._exchange)
        return self

    def load(self, *, cache: ScripMasterCache) -> EquityScripMaster:
        """Load today's master through ``cache``, fetching only on a miss."""
        return self.load_from_text(cache.text())

    # ---------------------------------------------------------------- queries
    def get(self, symbol: str) -> EquityRow | None:
        """Resolve one cash symbol, case-insensitively. ``None`` if absent.

        ``None`` rather than a raise is what lets spec section 5's rule hold —
        "a symbol that cannot be resolved is skipped and reported; it never
        blocks the rest of the run".
        """
        return self._by_symbol.get(_normalise_symbol(symbol))

    def resolve_all(self, symbols: Iterable[str]) -> tuple[dict[str, EquityRow], list[str]]:
        """Resolve many symbols at once: ``(resolved, unresolved)``.

        The caller gets both halves in one pass so the unresolved ones can be
        reported rather than silently missing from the resolved map.
        """
        resolved: dict[str, EquityRow] = {}
        unresolved: list[str] = []
        for symbol in symbols:
            row = self.get(symbol)
            if row is None:
                unresolved.append(_normalise_symbol(symbol))
            else:
                resolved[row.symbol] = row
        return resolved, unresolved

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_symbol))

    def __len__(self) -> int:
        return len(self._by_symbol)


def resolve_index(text: str, name: str, *, exchange: str = "NSE") -> IndexRow:
    """Resolve a spot index by trading symbol or custom symbol, e.g. ``"NIFTY 50"``.

    Matching is exact (case- and whitespace-insensitive) on either column, never
    a prefix: ``"NIFTY"`` must not be satisfied by ``NIFTY 100``, and a
    ``startswith`` would make exactly that mistake. Verified against the
    2026-09-21 master, where ``NIFTY`` / ``Nifty 50`` is one unambiguous row
    among 190 ``INDEX`` rows whose trading and custom symbols are each unique.

    Raises:
        ScripMasterError: no row matched, or — impossibly, but not silently —
            more than one did.
    """
    wanted = _normalise_symbol(name)
    matches: list[IndexRow] = []
    for row in csv.DictReader(io.StringIO(text)):
        if (row.get("SEM_INSTRUMENT_NAME") or "").upper() != _INDEX_INSTRUMENT:
            continue
        if (row.get("SEM_EXM_EXCH_ID") or "").upper() != exchange.upper():
            continue
        trading = _normalise_symbol(row.get("SEM_TRADING_SYMBOL"))
        custom = _normalise_symbol(row.get("SEM_CUSTOM_SYMBOL"))
        if wanted not in (trading, custom):
            continue
        security_id = str(row.get("SEM_SMST_SECURITY_ID") or "").strip()
        if not security_id:
            continue
        matches.append(
            IndexRow(
                security_id=security_id,
                symbol=trading,
                name=(row.get("SEM_CUSTOM_SYMBOL") or "").strip() or trading,
            )
        )

    if not matches:
        raise ScripMasterError(
            f"No {exchange.upper()} spot-index row matches {name!r} in the scrip master."
        )
    if len({row.security_id for row in matches}) > 1:
        raise ScripMasterError(
            f"{name!r} matches {len(matches)} different {exchange.upper()} index rows "
            f"(security ids {sorted({r.security_id for r in matches})}). Refusing to guess."
        )
    return matches[0]
