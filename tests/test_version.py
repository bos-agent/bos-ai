import tomllib
from pathlib import Path

from bos import core


def test_core_version_matches_pyproject():
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    assert core.__version__ == pyproject["project"]["version"]
