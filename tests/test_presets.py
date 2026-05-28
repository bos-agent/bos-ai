"""Tests for ep_agent_spec ExtensionPoint and agent spec resolution."""

import pytest

from bos.config.presets.default import bos_tools_usage, get_default_agent_spec
from bos.config.workspace import WorkspaceResolutionError, resolve_config_source
from bos.core.contract import ep_agent_spec
from bos.core.registry import Extension


class TestEpAgentSpec:
    def test_extension_point_exists(self):
        assert ep_agent_spec is not None
        assert "Agent spec" in ep_agent_spec.description

    def test_default_preset_registered(self):
        assert ep_agent_spec.has("_default")

    def test_default_agent_spec(self):
        spec = ep_agent_spec.invoke("_default")
        assert spec["name"] == "_default"
        assert spec["tools"] == "*"
        assert "system_prompt" in spec
        assert "tools_usage" in spec
        assert "plugins" in spec

    def test_unknown_spec_raises(self):
        with pytest.raises(ValueError, match="Extension 'nonexistent' not found"):
            ep_agent_spec.invoke("nonexistent")

    def test_describe_lists_available_specs(self):
        available = ep_agent_spec.describe()
        assert "_default" in available
        assert isinstance(available["_default"], str)

    def test_register_custom_spec(self):
        captured_spec = {"name": "custom_test", "system_prompt": "hello"}

        try:
            ep_agent_spec.register(
                Extension(
                    name="test-custom",
                    fn=lambda s=captured_spec: s,
                    description="Test custom agent spec",
                )
            )
            assert ep_agent_spec.has("test-custom")
            assert ep_agent_spec.invoke("test-custom") == captured_spec
        finally:
            ep_agent_spec._extensions.pop("test-custom", None)

    def test_overwrite_registered_spec(self):
        """Re-registering a name overwrites (existing EP behavior)."""
        name = f"overwrite-test-{id(object())}"

        old_spec = {"name": "old"}
        new_spec = {"name": "new"}

        try:
            ep_agent_spec.register(Extension(name=name, fn=lambda s=old_spec: s))
            ep_agent_spec.register(Extension(name=name, fn=lambda s=new_spec: s))
            assert ep_agent_spec.invoke(name) == new_spec
        finally:
            ep_agent_spec._extensions.pop(name, None)


class TestDefaultAgentSpec:
    def test_get_agent_spec_returns_correct_name(self):
        spec = get_default_agent_spec()
        assert spec["name"] == "_default"

    def test_get_agent_spec_has_system_prompt(self):
        spec = get_default_agent_spec()
        assert isinstance(spec["system_prompt"], str)
        assert len(spec["system_prompt"]) > 100
        assert "<role>" in spec["system_prompt"]

    def test_get_agent_spec_tools_is_star(self):
        spec = get_default_agent_spec()
        assert spec["tools"] == "*"

    def test_get_agent_spec_has_all_plugins(self):
        spec = get_default_agent_spec()
        plugins = spec["plugins"]
        for name in ("MemoryPlugin", "PlanPlugin", "TaskPlugin", "SkillsPlugin", "SubagentPlugin"):
            assert name in plugins
            assert plugins[name]["enabled"] is True

    def test_get_agent_spec_has_tools_usage(self):
        spec = get_default_agent_spec()
        tus = spec["tools_usage"]
        for tool in ("Bash", "ReadFile", "WriteFile", "EditFile", "GlobSearch", "GrepSearch", "WebSearch", "WebFetch"):
            assert tool in tus
            assert isinstance(tus[tool], str)
            assert len(tus[tool]) > 20

    def test_bos_tools_usage_module_export(self):
        assert "Bash" in bos_tools_usage
        assert "ReadFile" in bos_tools_usage
        assert all(isinstance(v, str) and len(v) > 20 for v in bos_tools_usage.values())


class TestResolveConfigSource:
    def test_file_path_resolves(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text('name = "file-agent"\nsystem_prompt = "from file"\n')
        config_path, bos_dir, config = resolve_config_source(str(toml_file))
        assert config_path == toml_file.resolve()
        assert bos_dir == tmp_path
        assert config["name"] == "file-agent"

    def test_nonexistent_file_raises(self):
        with pytest.raises(WorkspaceResolutionError, match="Config file not found"):
            resolve_config_source("nonexistent-config-zzz")
