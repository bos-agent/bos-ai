"""The BOS embedding SDK (BEP 18).

What this module exports is the embedding contract. What it does not export is
not promised — including every ``_``-prefixed helper ``bos.core`` re-exports for
extensions, which remain available and explicitly unstable.
"""

from ._app import BosApp
from ._bootstrap import bootstrap as bootstrap
from ._bootstrap import open_harness

# `bootstrap` is deliberately absent from __all__: BEP 18 §3.8 does not promise it.
# It stays importable — bos.cli uses it — but __all__ is the embedder's contract.
__all__ = ["BosApp", "open_harness"]
