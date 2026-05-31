
from bos.named_actors.runner import _agent_overrides, _parse_actors_config


class TestParseActorsConfig:
    def test_no_actors_section_returns_empty(self):
        assert _parse_actors_config({"main": {}}) == {}

    def test_no_main_key_returns_empty(self):
        assert _parse_actors_config({"platform": {}}) == {}

    def test_main_not_dict_returns_empty(self):
        assert _parse_actors_config({"main": "not-a-dict"}) == {}

    def test_actors_not_dict_returns_empty(self):
        assert _parse_actors_config({"main": {"actors": ["list", "not", "dict"]}}) == {}


def test_agent_overrides_remove_runtime_actor_keys():
    overrides = _agent_overrides(
        {
            "agent": "architect",
            "display_name": "Bob",
            "tools": ["ReadFile"],
            "maxims": ["user", "identity"],
        }
    )
    assert overrides == {"tools": ["ReadFile"], "maxims": ["user", "identity"]}
