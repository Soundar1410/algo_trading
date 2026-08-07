"""``NOTIFIER_FROM_SETTINGS`` must survive the ``spawn`` pickle round trip.

A real bug, caught by the existing supervisor end-to-end suite the first time
this sentinel shipped as a plain ``object()``: ``multiprocessing``'s ``spawn``
start method pickles ``run_worker``'s arguments to hand them to the child.
Unpickling a plain sentinel object does not preserve its identity — the
child's ``notifier is NOTIFIER_FROM_SETTINGS`` check silently came back
``False``, and the un-recognised sentinel value fell through to being used
*as* a notifier, raising ``AttributeError`` the moment anything called
``.send()`` on it. Every worker in the group crashed with exit code 1.
`Enum` members are pickle-safe singletons by design; this file pins that
property directly, rather than relying only on the (real, but indirect)
coverage the spawning supervisor tests already provide.
"""

from __future__ import annotations

import pickle

from runtimes.intraday_options.worker import NOTIFIER_FROM_SETTINGS, _NotifierSentinel


class _PlainSentinel:
    """Module-level so pickle can even attempt it (a local class cannot be
    pickled at all, which would make the point below untestable rather than
    proven)."""


def test_the_sentinel_survives_a_pickle_round_trip_with_identity_intact():
    restored = pickle.loads(pickle.dumps(NOTIFIER_FROM_SETTINGS))
    assert restored is NOTIFIER_FROM_SETTINGS


def test_the_sentinel_is_an_enum_member_not_a_plain_object():
    """Pins *why* the round trip above works, not just that it does — a
    future refactor back to a plain sentinel class would silently reintroduce
    the bug even though nothing about its usage looks wrong."""
    assert isinstance(NOTIFIER_FROM_SETTINGS, _NotifierSentinel)


def test_the_sentinel_is_distinct_from_a_freshly_unpickled_instance_of_its_class():
    """The failure mode in one assertion: a plain object() sentinel would
    fail exactly this, silently, only across a real process boundary."""
    plain = _PlainSentinel()
    restored = pickle.loads(pickle.dumps(plain))
    assert restored is not plain, (
        "if this ever starts passing, plain objects became pickle-identity-safe "
        "and the Enum requirement above may be revisited"
    )
