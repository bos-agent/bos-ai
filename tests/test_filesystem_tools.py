import pytest

from bos.extensions.tools import filesystem


@pytest.mark.asyncio
async def test_read_file_returns_one_based_line_numbers(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = await filesystem.tool_read_file(str(path))

    assert result == "1\talpha\n2\tbeta\n3\tgamma\n"


@pytest.mark.asyncio
async def test_read_file_preserves_line_numbers_with_offset_and_limit(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")

    result = await filesystem.tool_read_file(str(path), line_offset=1, limit=2)

    assert result == "2\tbeta\n3\tgamma\n"


@pytest.mark.asyncio
async def test_read_file_empty_file_message_unchanged(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")

    result = await filesystem.tool_read_file(str(path))

    assert result == "(Reached end of file or file is empty)"


@pytest.mark.asyncio
async def test_write_file_creates_new_file_without_prior_read(tmp_path):
    filesystem._READ_FILES.clear()
    path = tmp_path / "new.txt"

    result = await filesystem.tool_write_file(str(path), "created\n")

    assert result == f"Successfully wrote to {path}."
    assert path.read_text(encoding="utf-8") == "created\n"


@pytest.mark.asyncio
async def test_write_file_refuses_to_overwrite_existing_file_before_read(tmp_path):
    filesystem._READ_FILES.clear()
    path = tmp_path / "existing.txt"
    path.write_text("original\n", encoding="utf-8")

    result = await filesystem.tool_write_file(str(path), "changed\n")

    assert result == f"Error: Refusing to overwrite existing file '{path}' before it has been read with ReadFile."
    assert path.read_text(encoding="utf-8") == "original\n"


@pytest.mark.asyncio
async def test_write_file_overwrites_existing_file_after_read(tmp_path):
    filesystem._READ_FILES.clear()
    path = tmp_path / "existing.txt"
    path.write_text("original\n", encoding="utf-8")

    read_result = await filesystem.tool_read_file(str(path))
    write_result = await filesystem.tool_write_file(str(path), "changed\n")

    assert read_result == "1\toriginal\n"
    assert write_result == f"Successfully wrote to {path}."
    assert path.read_text(encoding="utf-8") == "changed\n"


@pytest.mark.asyncio
async def test_edit_file_rejects_ambiguous_old_string(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("target\nmiddle\ntarget\n", encoding="utf-8")

    result = await filesystem.tool_edit_file(str(path), old_string="target", new_string="changed")

    assert result == (
        "Error: old_string found 2 times at or after line 0. "
        "Provide a more specific old_string or set replace_all=true."
    )
    assert path.read_text(encoding="utf-8") == "target\nmiddle\ntarget\n"


@pytest.mark.asyncio
async def test_edit_file_allows_unique_old_string_after_line_offset(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("target\nmiddle\ntarget\n", encoding="utf-8")

    result = await filesystem.tool_edit_file(
        str(path),
        old_string="target",
        new_string="changed",
        line_offset=2,
    )

    assert result == f"Successfully edited {path}."
    assert path.read_text(encoding="utf-8") == "target\nmiddle\nchanged\n"


@pytest.mark.asyncio
async def test_edit_file_replace_all_allows_multiple_matches(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("target\nmiddle\ntarget\n", encoding="utf-8")

    result = await filesystem.tool_edit_file(
        str(path),
        old_string="target",
        new_string="changed",
        replace_all=True,
    )

    assert result == f"Successfully replaced all 2 occurrences in {path}."
    assert path.read_text(encoding="utf-8") == "changed\nmiddle\nchanged\n"


def _seed_dotbos_and_visible(tmp_path):
    (tmp_path / ".bos").mkdir()
    (tmp_path / ".bos" / "secret.txt").write_text("needle in dotbos\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("needle in workspace\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_glob_search_ignores_dotbos_by_default(tmp_path):
    _seed_dotbos_and_visible(tmp_path)

    result = await filesystem.tool_glob_search("**/*.txt", str(tmp_path))

    assert "visible.txt" in result
    assert ".bos" not in result


@pytest.mark.asyncio
async def test_glob_search_replace_ignore_searches_dotbos(tmp_path):
    _seed_dotbos_and_visible(tmp_path)

    # replace_ignore drops the default set (including ".bos"), exposing it.
    result = await filesystem.tool_glob_search("**/*.txt", str(tmp_path), replace_ignore=[".git"])

    assert "visible.txt" in result
    assert "secret.txt" in result


@pytest.mark.asyncio
async def test_glob_search_extend_ignore_adds_to_defaults(tmp_path):
    _seed_dotbos_and_visible(tmp_path)
    (tmp_path / "skip").mkdir()
    (tmp_path / "skip" / "other.txt").write_text("x\n", encoding="utf-8")

    result = await filesystem.tool_glob_search("**/*.txt", str(tmp_path), extend_ignore=["skip"])

    assert "visible.txt" in result
    assert "skip" not in result  # extended ignore
    assert ".bos" not in result  # default ignore still applies


@pytest.mark.asyncio
async def test_glob_search_remove_ignore_searches_dotbos(tmp_path):
    _seed_dotbos_and_visible(tmp_path)

    # remove_ignore subtracts ".bos" from the default set, exposing it.
    result = await filesystem.tool_glob_search("**/*.txt", str(tmp_path), remove_ignore=[".bos"])

    assert "visible.txt" in result
    assert "secret.txt" in result


@pytest.mark.asyncio
async def test_grep_search_ignores_dotbos_by_default(tmp_path):
    _seed_dotbos_and_visible(tmp_path)

    result = await filesystem.tool_grep_search("needle", str(tmp_path))

    assert "visible.txt" in result
    assert ".bos" not in result


@pytest.mark.asyncio
async def test_grep_search_replace_ignore_searches_dotbos(tmp_path):
    _seed_dotbos_and_visible(tmp_path)

    # replace_ignore drops the default set (including ".bos"), exposing it.
    result = await filesystem.tool_grep_search("needle", str(tmp_path), replace_ignore=[".git"])

    assert "visible.txt" in result
    assert "secret.txt" in result


@pytest.mark.asyncio
async def test_grep_search_remove_ignore_searches_dotbos(tmp_path):
    _seed_dotbos_and_visible(tmp_path)

    # remove_ignore subtracts ".bos" from the default set, exposing it.
    result = await filesystem.tool_grep_search("needle", str(tmp_path), remove_ignore=[".bos"])

    assert "visible.txt" in result
    assert "secret.txt" in result


def _ctx(workspace, chat_id: str = "chat-1"):
    from bos.core import ParentTurn, ToolContext

    return ToolContext(parent=ParentTurn(chat_id=chat_id, turn_id="t", agent_name="a"), workspace=str(workspace))


@pytest.mark.asyncio
async def test_relative_paths_resolve_against_the_workspace_not_the_process_cwd(tmp_path, monkeypatch):
    filesystem._READ_FILES.clear()
    workspace, elsewhere = tmp_path / "ws", tmp_path / "elsewhere"
    workspace.mkdir()
    elsewhere.mkdir()
    (workspace / "a.txt").write_text("alpha\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)  # an embedding host's cwd, not the workspace
    ctx = _ctx(workspace)

    assert await filesystem.tool_read_file("a.txt", context=ctx) == "1\talpha\n"
    assert await filesystem.tool_write_file("a.txt", "beta\n", context=ctx) == "Successfully wrote to a.txt."
    assert await filesystem.tool_edit_file("a.txt", "beta", "gamma", context=ctx) == "Successfully edited a.txt."
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "gamma\n"
    assert list(elsewhere.iterdir()) == []


@pytest.mark.asyncio
async def test_a_read_licenses_an_overwrite_only_in_the_chat_that_read(tmp_path, monkeypatch):
    filesystem._READ_FILES.clear()
    monkeypatch.chdir(tmp_path)  # whatever resolves wrongly lands here, not in the repo
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("original\n", encoding="utf-8")
    await filesystem.tool_read_file("a.txt", context=_ctx(workspace, "chat-a"))

    other_chat = await filesystem.tool_write_file("a.txt", "from b\n", context=_ctx(workspace, "chat-b"))
    same_chat = await filesystem.tool_write_file("a.txt", "from a\n", context=_ctx(workspace, "chat-a"))

    assert other_chat == "Error: Refusing to overwrite existing file 'a.txt' before it has been read with ReadFile."
    assert same_chat == "Successfully wrote to a.txt."


@pytest.mark.parametrize("grep_binary", [True, False], ids=["rg-or-grep", "python-fallback"])
@pytest.mark.asyncio
async def test_searches_run_in_the_workspace_and_answer_relative_to_it(tmp_path, monkeypatch, grep_binary):
    # Under a directory the searches ignore by name: the ignore check must see the
    # path within the workspace, never the workspace's own location.
    workspace = tmp_path / "build" / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "a.py").write_text("needle = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    if not grep_binary:
        monkeypatch.setattr(filesystem.os, "system", lambda _cmd: 1)
    ctx = _ctx(workspace)

    assert await filesystem.tool_glob_search("src/*.py", context=ctx) == "src/a.py"
    grep = await filesystem.tool_grep_search("needle", context=ctx)
    assert "a.py" in grep and "needle = 1" in grep
    assert str(workspace) not in grep


@pytest.mark.asyncio
async def test_an_agent_hands_its_workspace_to_the_tools(tmp_path, monkeypatch):
    from conftest import create_test_agent

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("alpha\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    agent = create_test_agent(tools=["ReadFile"], workspace=str(workspace))

    assert await agent._invoke_tool("ReadFile", path="a.txt", chat_id="c") == "1\talpha\n"
