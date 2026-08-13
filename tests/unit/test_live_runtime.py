from __future__ import annotations

from common.broker.factory import LiveExecutionBlocked
from runtimes.intraday_options.live_runtime import _acquire_account_live_lock


def test_controlled_rollout_allows_only_one_live_worker_per_account(tmp_path) -> None:
    first = _acquire_account_live_lock(tmp_path, "account-key")
    try:
        try:
            _acquire_account_live_lock(tmp_path, "account-key")
        except LiveExecutionBlocked as exc:
            assert "exactly one live strategy" in str(exc)
        else:  # pragma: no cover - assertion aid
            raise AssertionError("a second account-wide live lease was granted")
    finally:
        first.release()


def test_controlled_rollout_lease_is_recoverable_after_clean_shutdown(tmp_path) -> None:
    first = _acquire_account_live_lock(tmp_path, "account-key")
    first.release()

    second = _acquire_account_live_lock(tmp_path, "account-key")
    second.release()
