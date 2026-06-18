"""End-to-end wiring of BEP 11 services into the harness."""

import pytest

import bos.exts  # noqa: F401 — registers default ep impls (mail_route, chat_store, ...)
from bos.config.schema import HarnessConfig


class TestHarnessConfig:
    def test_default_job_runner_field(self):
        cfg = HarnessConfig()
        assert cfg.job_runner == "_default"

    def test_explicit_job_runner_field(self):
        cfg = HarnessConfig(job_runner="custom")
        assert cfg.job_runner == "custom"

    def test_unknown_field_still_rejected(self):
        with pytest.raises(Exception):  # pydantic ValidationError
            HarnessConfig(unknown="x")


class TestHarnessServices:
    @pytest.mark.asyncio
    async def test_services_exposed_on_plugin_services(self, tmp_path):
        from bos.core.harness import AgentHarness

        async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as h:
            svc = h._plugin_services
            assert svc.events is not None
            assert svc.jobs is not None
            assert svc.background_llm is not None
            assert h.jobs is svc.jobs

    @pytest.mark.asyncio
    async def test_drain_called_before_other_teardown(self, tmp_path):
        from bos.core.harness import AgentHarness

        order: list[str] = []

        class _SpyJobRunner:
            async def start(self): pass
            async def drain(self, *, timeout): order.append("drain")
            def bind_trigger(self, *a, **kw): pass
            async def submit(self, job): return "x"
            async def status(self, jid): return "queued"
            async def list(self, *, filter=None): return []
            async def retry(self, jid): pass
            async def cancel(self, jid): pass
            async def aclose(self): order.append("aclose")

        h = AgentHarness(bos_dir=tmp_path, workspace=tmp_path)
        await h.__aenter__()
        try:
            spy = _SpyJobRunner()
            h.jobs = spy
            h._owned.append(spy)
        finally:
            await h.__aexit__(None, None, None)
        assert order.index("drain") < order.index("aclose")
