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
