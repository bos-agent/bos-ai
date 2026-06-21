"""``TurnEvent`` is owned by the agent core (``bos.core.agent``).

This module re-exports it for backward compatibility. It is imported lazily
(never at ``bos.protocol`` package-init), so pulling ``bos.core`` inward here
does not create an import-time cycle.
"""

from bos.core.agent import TurnEvent

__all__ = ["TurnEvent"]
