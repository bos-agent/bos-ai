import sys

from bos.config.workspace import Workspace
from bos.core import ep_react_interceptor


def test_workspace_bootstrap_registers_agent_step_relay(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        '[platform]\nextensions = ["bos.extensions.bootstrap"]\n',
        encoding="utf-8",
    )

    previous_extension = ep_react_interceptor._extensions.pop("AgentStepRelay", None)
    previous_bootstrap = sys.modules.pop("bos.extensions.bootstrap", None)
    previous_relay = sys.modules.pop("bos.extensions.interceptors.agent_step_relay", None)

    try:
        Workspace(tmp_path).bootstrap_platform()
        assert ep_react_interceptor.has("AgentStepRelay")
    finally:
        ep_react_interceptor._extensions.pop("AgentStepRelay", None)
        if previous_extension is not None:
            ep_react_interceptor._extensions["AgentStepRelay"] = previous_extension

        sys.modules.pop("bos.extensions.bootstrap", None)
        if previous_bootstrap is not None:
            sys.modules["bos.extensions.bootstrap"] = previous_bootstrap

        sys.modules.pop("bos.extensions.interceptors.agent_step_relay", None)
        if previous_relay is not None:
            sys.modules["bos.extensions.interceptors.agent_step_relay"] = previous_relay
