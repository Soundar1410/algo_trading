"""``common/margin`` and ``common/market_data/dhan_margin.py`` are a
read-only, calculator-only surface (spec section 3.7's "never submit or
construct an order to calculate margin") — proven the same way
``tests/unit/test_scripts_are_read_only.py`` proves it for the read-only
script tier, plus the independent-review correction that the production
composition root must never wire the offline fallback model.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MARGIN_PACKAGE = REPO_ROOT / "common" / "margin"
DHAN_MARGIN_MODULE = REPO_ROOT / "common" / "market_data" / "dhan_margin.py"
MAIN_MODULE = REPO_ROOT / "runtimes" / "positional_options" / "__main__.py"

MARGIN_FILES = [*sorted(MARGIN_PACKAGE.glob("*.py")), DHAN_MARGIN_MODULE]

_ORDER_TOKENS = (
    "/orders",
    "/superorder",
    "/forever",
    "placeOrder",
    "cancelOrder",
    "modifyOrder",
)


def _broker_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return {name for name in imported if "broker" in name.lower()}


def _string_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_margin_files_exist() -> None:
    """Guards every check below: an empty glob would pass everything."""
    assert len(MARGIN_FILES) >= 4


def test_no_margin_file_imports_a_broker() -> None:
    offenders = {
        str(path.relative_to(REPO_ROOT)): _broker_imports(path)
        for path in MARGIN_FILES
        if _broker_imports(path)
    }
    assert offenders == {}, f"margin code imports a broker: {offenders}"


def test_no_margin_file_references_an_order_endpoint() -> None:
    for path in MARGIN_FILES:
        source = path.read_text(encoding="utf-8")
        code_only = "\n".join(
            line.split("#", 1)[0]
            for line in source.splitlines()
            if not line.strip().startswith("#")
        )
        docstring_text = "\n".join(_string_literals(path))
        for forbidden in _ORDER_TOKENS:
            in_code = forbidden in code_only and forbidden not in docstring_text
            assert not in_code, (
                f"{path.relative_to(REPO_ROOT)} references the order endpoint {forbidden!r}"
            )


def test_dhan_margin_module_only_calls_the_margin_calculator_endpoint() -> None:
    source = DHAN_MARGIN_MODULE.read_text(encoding="utf-8")
    # The only Dhan URL this module may ever construct.
    assert "https://api.dhan.co/v2/margincalculator" in source
    other_dhan_endpoints = re.findall(r"https://api\.dhan\.co/v2/(\w+)", source)
    assert set(other_dhan_endpoints) == {"margincalculator"}, (
        f"dhan_margin.py references unexpected Dhan endpoints: {other_dhan_endpoints}"
    )


def test_dhan_margin_module_constructs_no_broker_or_order_client() -> None:
    tree = ast.parse(DHAN_MARGIN_MODULE.read_text(encoding="utf-8"))
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "DhanMarketFeedAdapter" not in calls
    assert not any("Broker" in name or "OrderClient" in name for name in calls)


def test_production_composition_root_never_wires_the_offline_fallback_model() -> None:
    """Independent-review correction: ``ConservativeMarginModel``/
    ``fallback_model`` must never appear in the production entrypoint — the
    real Dhan margin-calculator source, or a blocked entry, are the only two
    outcomes production may ever reach."""
    source = MAIN_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Only real code — not the comment explaining *why* fallback_model must
    # never appear here, which would otherwise trip this exact check.
    imported_names: set[str] = set()
    keyword_args: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            keyword_args.update(kw.arg for kw in node.keywords if kw.arg is not None)
    assert "ConservativeMarginModel" not in imported_names
    assert "fallback_model" not in keyword_args
    assert "MarginEstimator(" in source
    assert "build_dhan_margin_fetcher" in source
