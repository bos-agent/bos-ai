"""WatermarkStore — per-chat last-handled revision (per-agent file)."""

import pytest

from bos.plugins.memory._watermark import WatermarkStore


@pytest.mark.asyncio
async def test_get_default_zero(tmp_path):
    s = WatermarkStore(tmp_path / "wm.json")
    assert await s.get("never-existed") == 0


@pytest.mark.asyncio
async def test_set_and_get(tmp_path):
    s = WatermarkStore(tmp_path / "wm.json")
    await s.set("c1", 5)
    assert await s.get("c1") == 5


@pytest.mark.asyncio
async def test_persists_across_instances(tmp_path):
    s1 = WatermarkStore(tmp_path / "wm.json")
    await s1.set("c1", 9)
    s2 = WatermarkStore(tmp_path / "wm.json")
    assert await s2.get("c1") == 9


@pytest.mark.asyncio
async def test_snapshot_returns_all_chats(tmp_path):
    s = WatermarkStore(tmp_path / "wm.json")
    await s.set("c1", 1)
    await s.set("c2", 2)
    await s.set("c3", 7)
    snap = await s.snapshot()
    assert snap == {"c1": 1, "c2": 2, "c3": 7}
