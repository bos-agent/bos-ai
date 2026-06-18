"""WatermarkStore — per-(scope, chat_id) last-handled revision."""

import pytest

from bos.plugins.memory._watermark import WatermarkStore


@pytest.mark.asyncio
async def test_get_default_zero(tmp_path):
    s = WatermarkStore(tmp_path / "wm.json")
    assert await s.get("workspace", "c1") == 0


@pytest.mark.asyncio
async def test_set_and_get(tmp_path):
    s = WatermarkStore(tmp_path / "wm.json")
    await s.set("workspace", "c1", 5)
    assert await s.get("workspace", "c1") == 5


@pytest.mark.asyncio
async def test_persists_across_instances(tmp_path):
    s1 = WatermarkStore(tmp_path / "wm.json")
    await s1.set("workspace", "c1", 9)
    s2 = WatermarkStore(tmp_path / "wm.json")
    assert await s2.get("workspace", "c1") == 9


@pytest.mark.asyncio
async def test_snapshot_groups_by_scope(tmp_path):
    s = WatermarkStore(tmp_path / "wm.json")
    await s.set("workspace", "c1", 1)
    await s.set("workspace", "c2", 2)
    await s.set("agent-a", "c1", 7)
    snap = await s.snapshot()
    assert snap == {"workspace": {"c1": 1, "c2": 2}, "agent-a": {"c1": 7}}
