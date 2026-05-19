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
