"""Positional stocks — a weekly, paper-only equity strategy group.

Unlike ``intraday_options`` and ``positional_options``, nothing in this group
is a long-lived worker. Its one strategy, ``wsr1_weekly_stochrsi``, is a
run-to-completion weekly batch job with no tick feed and no supervisor; see
``wsr1_weekly_stochrsi/WSR1_WEEKLY_STOCH_RSI_SPEC.md``, which is authoritative.
"""
