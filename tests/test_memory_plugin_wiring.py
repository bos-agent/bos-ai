"""Lock tests for MemoryHarnessPlugin lifecycle.

P0 locks current behavior: setup() stores services and creates no backend.
P4 extends setup to also construct the operation service, watermark store,
consolidator, and bind a JobRunner trigger."""

import pytest

from bos.core.contract import PluginServices
from bos.plugins.memory.plugin import MemoryHarnessPlugin


@pytest.mark.asyncio
async def test_current_setup_is_minimal(tmp_path):
    h = MemoryHarnessPlugin()
    await h.setup(PluginServices(
        bos_dir=tmp_path, workspace=tmp_path, llm=None, consolidator=None, subagents=None,
    ))
    assert h._services is not None
    assert h._backend is None
