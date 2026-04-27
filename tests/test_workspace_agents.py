from copy import deepcopy
from textwrap import dedent

import pytest

from bos.config.workspace import Workspace


def _write_workspace_config(tmp_path, config_text: str) -> None:
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir(exist_ok=True)
    (bos_dir / "config.toml").write_text(dedent(config_text).strip() + "\n", encoding="utf-8")


def test_resolve_platform_config_keeps_raw_config_unchanged(tmp_path):
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = ["agents"]

        [[platform.agents]]
        name = "main"
        description = "inline"
        """,
    )
    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir()
    (agents_dir / "main.toml").write_text('name = "main"\ndescription = "external"\n', encoding="utf-8")

    ws = Workspace(tmp_path)
    raw_platform = deepcopy(ws.config["platform"])

    resolved = ws.resolve_platform_config()

    assert ws.config["platform"] == raw_platform
    assert resolved["agents"] == [{"name": "main", "description": "external"}]
    assert "agent_dirs" not in resolved


def test_resolve_platform_config_auto_scans_default_agents_dir(tmp_path):
    _write_workspace_config(
        tmp_path,
        """
        [platform]

        [[platform.agents]]
        name = "main"
        description = "inline"
        """,
    )
    # Default agent_dirs = ["agents"], relative to .bos/
    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir()
    (agents_dir / "main.toml").write_text('name = "main"\ndescription = "external"\n', encoding="utf-8")

    resolved = Workspace(tmp_path).resolve_platform_config()

    # Auto-scanned; last-wins means external overrides inline
    assert resolved["agents"] == [{"name": "main", "description": "external"}]


def test_resolve_platform_config_loads_flat_agent_definitions(tmp_path):
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = ["agents"]
        """,
    )
    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir()
    (agents_dir / "foo.toml").write_text('name = "foo"\ndescription = "flat"\n', encoding="utf-8")
    (agents_dir / "bar.toml").write_text(
        'name = "bar"\ndescription = "also flat"\nsystem_prompt = "Hello"\n',
        encoding="utf-8",
    )

    resolved = Workspace(tmp_path).resolve_platform_config()

    assert [agent["name"] for agent in resolved["agents"]] == ["bar", "foo"]
    assert resolved["agents"][0]["system_prompt"] == "Hello"


def test_resolve_platform_config_loads_markdown_agent_with_frontmatter(tmp_path):
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = ["agents"]
        """,
    )
    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir()
    (agents_dir / "researcher.md").write_text(
        dedent("""
        ---
        name: repo-researcher
        description: Focused repo analyst
        tools:
          - ReadFile
          - SearchFiles
        exclude_tools: [WriteFile, Shell]
        reasoning_effort: high
        max_iterations: 3
        ---
        You are the researcher.

        Return concise findings.
        """).lstrip(),
        encoding="utf-8",
    )

    resolved = Workspace(tmp_path).resolve_platform_config()

    assert resolved["agents"] == [
        {
            "name": "repo-researcher",
            "description": "Focused repo analyst",
            "tools": ["ReadFile", "SearchFiles"],
            "exclude_tools": ["WriteFile", "Shell"],
            "reasoning_effort": "high",
            "max_iterations": 3,
            "system_prompt": "You are the researcher.\n\nReturn concise findings.\n",
        }
    ]


def test_resolve_platform_config_loads_markdown_agent_without_frontmatter(tmp_path):
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = ["agents"]
        """,
    )
    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir()
    (agents_dir / "writer.md").write_text("Write clear docs.\n", encoding="utf-8")

    resolved = Workspace(tmp_path).resolve_platform_config()

    assert resolved["agents"] == [{"name": "writer", "system_prompt": "Write clear docs.\n"}]


def test_resolve_platform_config_supports_markdown_exlude_tools_alias(tmp_path):
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = ["agents"]
        """,
    )
    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir()
    (agents_dir / "guarded.md").write_text(
        dedent("""
        ---
        exlude_tools:
          - DangerousTool
        ---
        Stay guarded.
        """).lstrip(),
        encoding="utf-8",
    )

    resolved = Workspace(tmp_path).resolve_platform_config()

    assert resolved["agents"] == [
        {
            "name": "guarded",
            "exclude_tools": ["DangerousTool"],
            "system_prompt": "Stay guarded.\n",
        }
    ]


def test_resolve_platform_config_treats_semantic_frontmatter_conflicts_as_prompt(tmp_path, caplog):
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = ["agents"]
        """,
    )
    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir()
    (agents_dir / "main.md").write_text(
        dedent("""
        ---
        system_prompt: not here
        ---
        Prompt body.
        """).lstrip(),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        resolved = Workspace(tmp_path).resolve_platform_config()

    assert resolved["agents"] == [
        {
            "name": "main",
            "system_prompt": "---\nsystem_prompt: not here\n---\nPrompt body.\n",
        }
    ]
    assert "defines system_prompt in frontmatter" in caplog.text
    assert "using the whole file as system_prompt" in caplog.text


def test_resolve_platform_config_treats_unclosed_frontmatter_as_prompt(tmp_path, caplog):
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = ["agents"]
        """,
    )
    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir()
    content = "---\nname: broken\nThis is actually prompt text.\n"
    (agents_dir / "main.md").write_text(content, encoding="utf-8")

    with caplog.at_level("WARNING"):
        resolved = Workspace(tmp_path).resolve_platform_config()

    assert resolved["agents"] == [{"name": "main", "system_prompt": content}]
    assert "Invalid frontmatter in Markdown agent definition agents/main.md" in caplog.text
    assert "using the whole file as system_prompt" in caplog.text


def test_resolve_platform_config_treats_malformed_frontmatter_as_prompt(tmp_path, caplog):
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = ["agents"]
        """,
    )
    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir()
    content = "---\nname: broken\n  unexpected: indent\n---\nPrompt body.\n"
    (agents_dir / "main.md").write_text(content, encoding="utf-8")

    with caplog.at_level("WARNING"):
        resolved = Workspace(tmp_path).resolve_platform_config()

    assert resolved["agents"] == [{"name": "main", "system_prompt": content}]
    assert "Invalid frontmatter in Markdown agent definition agents/main.md" in caplog.text
    assert "using the whole file as system_prompt" in caplog.text


def test_resolve_platform_config_rejects_non_string_inline_system_prompt(tmp_path):
    _write_workspace_config(
        tmp_path,
        """
        [platform]

        [[platform.agents]]
        name = "main"

        [platform.agents.system_prompt]
        _default = "inline"
        """,
    )

    with pytest.raises(ValueError, match="system_prompt must be a string"):
        Workspace(tmp_path).resolve_platform_config()


def test_resolve_platform_config_uses_exact_name_last_wins_and_tracks_source_history(tmp_path):
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = ["agents"]

        [[platform.agents]]
        name = "main"
        description = "inline"
        """,
    )
    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir()
    (agents_dir / "01-main.toml").write_text('name = "main"\ndescription = "first file"\n', encoding="utf-8")
    (agents_dir / "zz-main.toml").write_text('name = "main"\ndescription = "final file"\n', encoding="utf-8")

    ws = Workspace(tmp_path)
    resolved = ws.resolve_platform_config()

    assert resolved["agents"] == [{"name": "main", "description": "final file"}]
    history = ws.agent_source_history["main"]
    assert [(record.source_kind, record.source_path, record.won) for record in history] == [
        ("inline", "config.toml", False),
        ("file", "agents/01-main.toml", False),
        ("file", "agents/zz-main.toml", True),
    ]


def test_resolve_platform_config_rejects_case_only_name_collisions(tmp_path):
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = ["agents"]

        [[platform.agents]]
        name = "main"
        """,
    )
    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir()
    (agents_dir / "upper.toml").write_text('name = "Main"\n', encoding="utf-8")

    with pytest.raises(ValueError, match=r"'main'.*config.toml.*'Main'.*agents/upper.toml"):
        Workspace(tmp_path).resolve_platform_config()


def test_resolve_platform_config_rejects_non_list_platform_agents(tmp_path):
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agents = ""
        """,
    )

    with pytest.raises(ValueError, match="platform.agents must be a list of tables"):
        Workspace(tmp_path).resolve_platform_config()


def test_resolve_platform_config_rejects_non_list_agent_dirs(tmp_path):
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = "agents"
        """,
    )

    with pytest.raises(ValueError, match="platform.agent_dirs must be a list of strings"):
        Workspace(tmp_path).resolve_platform_config()


def test_resolve_platform_config_clears_stale_source_history_on_failure(tmp_path):
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = ["agents"]

        [[platform.agents]]
        name = "main"
        """,
    )
    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir()
    good_agent = agents_dir / "main.toml"
    good_agent.write_text('name = "main"\n', encoding="utf-8")

    ws = Workspace(tmp_path)
    ws.resolve_platform_config()
    assert "main" in ws.agent_source_history

    good_agent.write_text('name = "Main"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="Case-only agent name collision"):
        ws.resolve_platform_config()

    assert ws.agent_source_history == {}


def test_resolve_platform_config_derives_name_from_filename_stem(tmp_path):
    """When name is omitted from an external agent file, derive it from the filename stem."""
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = ["agents"]
        """,
    )
    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir()
    (agents_dir / "researcher.toml").write_text('description = "no explicit name"\n', encoding="utf-8")

    resolved = Workspace(tmp_path).resolve_platform_config()

    assert resolved["agents"] == [{"name": "researcher", "description": "no explicit name"}]



def test_resolve_platform_config_multiple_agent_dirs(tmp_path):
    """agent_dirs supports multiple directories, scanned in order."""
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = ["agents", "more_agents"]
        """,
    )
    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir()
    (agents_dir / "alpha.toml").write_text('description = "from agents"\n', encoding="utf-8")

    more_dir = tmp_path / ".bos" / "more_agents"
    more_dir.mkdir()
    (more_dir / "beta.toml").write_text('description = "from more_agents"\n', encoding="utf-8")

    resolved = Workspace(tmp_path).resolve_platform_config()

    names = [a["name"] for a in resolved["agents"]]
    assert names == ["alpha", "beta"]
    assert resolved["agents"][0]["description"] == "from agents"
    assert resolved["agents"][1]["description"] == "from more_agents"


def test_resolve_platform_config_relative_path_outside_bos(tmp_path):
    """Relative paths are resolved against .bos/, so '../agents' goes to workspace root."""
    _write_workspace_config(
        tmp_path,
        """
        [platform]
        agent_dirs = ["../agents"]
        """,
    )
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "worker.toml").write_text('description = "outside bos"\n', encoding="utf-8")

    resolved = Workspace(tmp_path).resolve_platform_config()

    assert resolved["agents"] == [{"name": "worker", "description": "outside bos"}]


def test_resolve_platform_config_absolute_path(tmp_path):
    """Absolute paths are used as-is."""
    agents_dir = tmp_path / "ext_agents"
    agents_dir.mkdir()
    (agents_dir / "ext.toml").write_text('description = "absolute"\n', encoding="utf-8")

    _write_workspace_config(
        tmp_path,
        f"""
        [platform]
        agent_dirs = ["{agents_dir.as_posix()}"]
        """,
    )

    resolved = Workspace(tmp_path).resolve_platform_config()

    assert resolved["agents"] == [{"name": "ext", "description": "absolute"}]
