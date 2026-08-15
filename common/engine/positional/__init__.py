"""The generic positional multi-leg engine — a sibling of
:class:`~common.engine.multi_leg_engine.MultiLegEngine`, not a modification
of it, for a strategy whose position lifecycle spans multiple trading
sessions under one durable ``cycle_id`` (spec section 9.2). See
:mod:`common.engine.positional.positional_engine`'s own module docstring for
the full reasoning.

Generic to any positional multi-leg strategy — contains no
``weekly_delta_neutral`` (or any other strategy-specific) branch anywhere in
this package (see ``tests/unit/test_no_weekly_delta_neutral_branches.py``).
"""

from __future__ import annotations
