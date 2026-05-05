import pytest

from bos.named_actors.runner import _agent_overrides, _parse_actors_config


class TestParseActorsConfig:
    def test_parses_actors_section(self):
        config = {
            "main": {
                "actors": {
                    "researcher": {"agent": "assistant"},
                    "reviewer": {"agent": "assistant"},
                    "main": {"agent": "main"},
                }
            }
        }
        actors = _parse_actors_config(config)
        assert len(actors) == 3
        assert actors["researcher"] == {"agent": "assistant"}
        assert actors["reviewer"] == {"agent": "assistant"}
        assert actors["main"] == {"agent": "main"}

    def test_no_actors_section_returns_empty(self):
        assert _parse_actors_config({"main": {}}) == {}

    def test_no_main_key_returns_empty(self):
        assert _parse_actors_config({"platform": {}}) == {}

    def test_main_not_dict_returns_empty(self):
        assert _parse_actors_config({"main": "not-a-dict"}) == {}

    def test_actors_not_dict_returns_empty(self):
        assert _parse_actors_config({"main": {"actors": ["list", "not", "dict"]}}) == {}

    def test_filters_non_dict_actor_values(self):
        actors = _parse_actors_config({
            "main": {
                "actors": {
                    "valid": {"agent": "assistant"},
                    "also_valid": {"agent": "assistant"},
                    "not_dict": "just-a-string",
                    "also_not": 42,
                },
            }
        })
        assert set(actors) == {"valid", "also_valid"}


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


@pytest.mark.parametrize("field", ["identity", "memory_scope", "role_label", "name"])
def test_parse_actors_rejects_forbidden_identity_fields(field):
    with pytest.raises(ValueError, match="forbidden field"):
        _parse_actors_config({"main": {"actors": {"bob": {"agent": "architect", field: "bad"}}}})
