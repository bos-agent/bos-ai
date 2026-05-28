import sys

from bos.config.workspace import Workspace
from bos.core import ReActAgent


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


def test_workspace_bootstrap_registers_external_agents_from_resolved_platform(tmp_path, monkeypatch):
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

    registered: list[dict] = []
    monkeypatch.setattr("bos.core.harness.ReActAgent.register", lambda **kwargs: registered.append(kwargs))

    Workspace.from_discovery(tmp_path).bootstrap_platform()

    non_default = [r for r in registered if r.get("name") != "_default"]
    assert non_default == [
        {
            "name": "main",
            "description": "External",
            "system_prompt": "Resolved prompt",
        }
    ]


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
        defaults = ReActAgent.get_defaults(agent_name)

        assert defaults["tools"] is None
        assert defaults["skills"] == ["writer"]
    finally:
        ReActAgent._registry.pop(agent_name, None)
