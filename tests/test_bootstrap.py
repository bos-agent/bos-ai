import sys

from bos.config.workspace import Workspace
from bos.core import ep_agent_spec


def test_workspace_bootstrap_loads_extensions_bundle(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        '[platform]\nextensions = ["bos.exts"]\n',
        encoding="utf-8",
    )

    previous_extensions = sys.modules.pop("bos.exts", None)

    try:
        Workspace.from_discovery(tmp_path).bootstrap_platform()
        assert "bos.exts" in sys.modules
    finally:
        sys.modules.pop("bos.exts", None)
        if previous_extensions is not None:
            sys.modules["bos.exts"] = previous_extensions


def test_workspace_bootstrap_registers_external_agents_from_resolved_platform(tmp_path):
    bos_dir = tmp_path / ".bos"
    agents_dir = bos_dir / "agents"
    bos_dir.mkdir()
    agents_dir.mkdir(parents=True)
    (bos_dir / "config.toml").write_text(
        """
[platform]
agent_dirs = ["agents"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (agents_dir / "main.toml").write_text(
        """
name = "main"
description = "External"
system_prompt = "Resolved prompt"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    try:
        Workspace.from_discovery(tmp_path).bootstrap_platform()

        assert ep_agent_spec.has("main")
        spec = ep_agent_spec.invoke("main")
        assert spec["name"] == "main"
        assert spec["system_prompt"] == "Resolved prompt"
        assert spec["description"] == "External"
    finally:
        ep_agent_spec._extensions.pop("main", None)


def test_workspace_bootstrap_normalizes_agent_capability_strings(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    agent_name = "capability-main"
    (bos_dir / "config.toml").write_text(
        f"""
[platform]

[[platform.agents]]
name = "{agent_name}"
description = "Capabilities"
tools = "*"
skills = ["writer"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    try:
        Workspace.from_discovery(tmp_path).bootstrap_platform()
        spec = ep_agent_spec.invoke(agent_name)

        assert spec["tools"] is None
        assert spec["skills"] == ["writer"]
    finally:
        ep_agent_spec._extensions.pop(agent_name, None)
