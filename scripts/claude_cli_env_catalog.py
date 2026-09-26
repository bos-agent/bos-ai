#!/usr/bin/env python
"""The Claude Code CLI's environment catalog: every variable name its environment module declares.

BEP 19 §3.12 and §3.13. The CLI bundled in claude-agent-sdk declares the environment variables it
reads in one module, and ``claude_code.py``'s inherited-variable list was checked against all of
them. ``tests/data/claude_cli_env_catalog.txt`` records that catalog, and
``test_the_cli_env_catalog_matches_its_snapshot`` fails when a new CLI declares a name the snapshot
does not hold, or drops one. Classify each new name first (BEP 19 §3.12's rule), then regenerate::

    uv run python scripts/claude_cli_env_catalog.py           # print the bundled CLI's catalog
    uv run python scripts/claude_cli_env_catalog.py --write   # rewrite the snapshot from it

Read from the minified bundle, so it depends on how the CLI is built: the module is the chunk
holding ``ANTHROPIC_BASE_URL:()=>``, where each group of variables is an ``var X={};F(X,{NAME:()=>
parser,…})`` export object and the accessor's shape spreads them all (``var S={...X,...Y,…}``,
read from the CLI 2.1.281 source). A build that changes that shape fails loudly here rather than
yielding a short catalog that would pass.
"""

from __future__ import annotations

import argparse
import mmap
import re
import sys
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parents[1] / "tests" / "data" / "claude_cli_env_catalog.txt"
# CLI 2.1.281 declares 1049. Far fewer means the extraction broke, not that the CLI shrank.
MINIMUM = 800

_WRITE = "uv run python scripts/claude_cli_env_catalog.py --write"
_ANCHOR = b"ANTHROPIC_BASE_URL:()=>"
_ENTRY = rb"[A-Za-z_$][\w$]*:\(\)=>[A-Za-z_$][\w$]*"
_GROUP = re.compile(rb"var ([\w$]+)=\{\};[\w$]+\(\1,\{((?:" + _ENTRY + rb",)*" + _ENTRY + rb")\}\)")
_SHAPE = re.compile(rb"var [\w$]+=\{((?:\.\.\.[\w$]+,)*\.\.\.[\w$]+)\}")


def bundled_cli() -> Path:
    import claude_agent_sdk

    return Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"


def cli_version() -> str:
    from claude_agent_sdk._cli_version import __cli_version__

    return __cli_version__


def extract(binary: Path) -> list[str]:
    """Every name the CLI's environment module declares, sorted. Raises ``LookupError`` when the
    module is not where, or not shaped as, it was."""
    with open(binary, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        anchor = data.find(_ANCHOR)
        if anchor < 0:
            raise LookupError(f"{binary} holds no {_ANCHOR.decode()!r}: the environment module was not found")
        end = data.find(b"// @bun", anchor)
        chunk = data[max(data.rfind(b"// @bun", 0, anchor), 0) : end if end >= 0 else len(data)]
    groups = dict(_GROUP.findall(chunk))
    shapes = [re.findall(rb"\.\.\.([\w$]+)", shape) for shape in _SHAPE.findall(chunk)]
    spread = max((shape for shape in shapes if all(name in groups for name in shape)), key=len, default=[])
    names = sorted({
        name.decode() for group in spread for name in re.findall(rb"(?:^|,)([A-Za-z_$][\w$]*):\(\)=>", groups[group])
    })
    if len(names) < MINIMUM:
        raise LookupError(
            f"{binary}: found {len(names)} names in the environment module, fewer than {MINIMUM} — the bundle "
            f"is no longer shaped as this script reads it; fix the extraction before trusting any comparison"
        )
    return names


def read_snapshot(path: Path = SNAPSHOT) -> tuple[str, list[str]]:
    """The snapshot's header line and its names."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[0], [line for line in lines if line and not line.startswith("#")]


def header(version: str, count: int) -> str:
    return f"# claude CLI {version}: the {count} names its environment module declares (BEP 19 §3.13)."


def write_snapshot(names: list[str], version: str, path: Path = SNAPSHOT) -> None:
    regenerate = ["# Classify each new name first (BEP 19 §3.12), then regenerate with:", f"#   {_WRITE}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([header(version, len(names)), *regenerate, *names]) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help=f"rewrite {SNAPSHOT} from the bundled CLI")
    args = parser.parse_args(argv)
    names = extract(bundled_cli())
    if args.write:
        write_snapshot(names, cli_version())
        print(f"wrote {len(names)} names to {SNAPSHOT}")
    else:
        print("\n".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
