"""Test-only fixture strategy — never enabled by any committed
``config/strategies/*.yaml``, never approved for real trading (CLAUDE.md
restricts this repository to ``c921_ema_cross_buy`` and
``weekly_delta_neutral`` only). Exists solely so
``tests/integration/test_positional_runtime_multi_strategy.py`` can prove
:class:`~runtimes.positional_options.supervisor.PositionalOptionsSupervisor`
genuinely drives **two** independent positional workers under one shared
feed hub, without depending on a second real trading strategy ever being
approved. See that module's own docstring, and ``strategy.py`` here, for
what it does and does not do.
"""
