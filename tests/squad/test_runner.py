# tests/squad/test_runner.py
import pytest
from bos.squad.runner import _parse_actors_config


class TestParseActorsConfig:
    def test_parses_actors_section(self):
        config = {
            "main": {
                "actors": {
                    "researcher": {"agent": "researcher"},
                    "reviewer": {"agent": "reviewer"},
                    "main": {"agent": "main"},
                }
            }
        }
        actors = _parse_actors_config(config)
        assert len(actors) == 3
        assert actors["researcher"] == {"agent": "researcher"}
        assert actors["reviewer"] == {"agent": "reviewer"}
        assert actors["main"] == {"agent": "main"}

    def test_no_actors_section_returns_empty(self):
        actors = _parse_actors_config({"main": {}})
        assert actors == {}

    def test_default_agent_name(self):
        actors = _parse_actors_config({
            "main": {
                "agent": "orchestrator",
                "actors": {
                    "helper": {"agent": "helper"},
                },
            }
        })
        assert len(actors) == 1

    def test_no_main_key_returns_empty(self):
        actors = _parse_actors_config({"platform": {}})
        assert actors == {}

    def test_main_not_dict_returns_empty(self):
        actors = _parse_actors_config({"main": "not-a-dict"})
        assert actors == {}

    def test_actors_not_dict_returns_empty(self):
        actors = _parse_actors_config({"main": {"actors": ["list", "not", "dict"]}})
        assert actors == {}

    def test_filters_non_dict_actor_values(self):
        actors = _parse_actors_config({
            "main": {
                "actors": {
                    "valid": {"agent": "valid"},
                    "also_valid": {"agent": "also"},
                    "not_dict": "just-a-string",
                    "also_not": 42,
                },
            }
        })
        assert len(actors) == 2
        assert "valid" in actors
        assert "also_valid" in actors
        assert "not_dict" not in actors
        assert "also_not" not in actors
