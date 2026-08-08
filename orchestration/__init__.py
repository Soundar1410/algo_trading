"""Phase 8: everything that supervises a runtime process from *outside* it.

``process_control/`` is the bounded-restart wrapper every LaunchAgent's
``ProgramArguments`` actually points at (never the supervisor entrypoint
directly — ``launchd``'s own ``KeepAlive`` is unbounded). ``launchd/`` holds
the generated property-list files themselves and the generator that writes
them. Named to match the spec's own repository layout (section 9).
"""

from __future__ import annotations
