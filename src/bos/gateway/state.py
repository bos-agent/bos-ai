from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GatewayRunDir:
    bos_dir: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "bos_dir", Path(self.bos_dir).expanduser().resolve())

    @property
    def root(self) -> Path:
        return self.bos_dir / "run"

    @property
    def pid_file(self) -> Path:
        return self.root / "gateway.pid"

    @property
    def state_file(self) -> Path:
        return self.root / "gateway.state"

    @property
    def log_file(self) -> Path:
        return self.root / "gateway.log"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)


def read_gateway_state(run_dir: GatewayRunDir) -> dict[str, Any]:
    try:
        return json.loads(run_dir.state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_gateway_state(run_dir: GatewayRunDir, snapshot: dict[str, Any]) -> None:
    run_dir.ensure()
    payload = dict(snapshot)
    payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
    tmp = run_dir.root / f".gateway.state.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        tmp.replace(run_dir.state_file)
    finally:
        tmp.unlink(missing_ok=True)
