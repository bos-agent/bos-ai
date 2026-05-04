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
