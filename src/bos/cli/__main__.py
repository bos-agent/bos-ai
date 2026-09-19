"""Run the BOS CLI as a module (BEP 16 §3.3).

``bos-ai`` ships no console script — the ``boscli`` command comes from the
separate ``boscli`` distribution. This entry point is how a ``bos-ai[cli]``
install, or a checkout of this repository, invokes the same CLI::

    python -m bos.cli ask "how are you"
    uv run python -m bos.cli gateway status
"""

from __future__ import annotations

from .entry import main

if __name__ == "__main__":
    main()
