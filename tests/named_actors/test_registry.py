import pytest

from bos.named_actors.registry import ActorRegistry


class FakeMailBox:
    def __init__(self, address):
        self.address = address


@pytest.fixture
def registry():
    reg = ActorRegistry()
    reg.register("main", FakeMailBox("agent@main"), is_default=True, display_name="Main", agent_kind="assistant")
    reg.register("researcher", FakeMailBox("agent@researcher"), display_name="Rae", agent_kind="assistant")
    reg.register("reviewer", FakeMailBox("agent@reviewer"))
    return reg


class TestResolveAddress:
    def test_resolve_known_actor(self, registry):
        assert registry.resolve_address("researcher") == "agent@researcher"

    def test_resolve_default_when_none(self, registry):
        assert registry.resolve_address(None) == "agent@main"

    def test_resolve_default_when_unknown(self, registry):
        assert registry.resolve_address("unknown") == "agent@main"

    def test_resolve_mailbox(self, registry):
        mb = registry.resolve_mailbox("reviewer")
        assert mb.address == "agent@reviewer"

    def test_list_actors(self, registry):
        actors = registry.list_actors()
        assert set(actors.keys()) == {"main", "researcher", "reviewer"}
        assert actors["main"].is_default is True
        assert actors["researcher"].display_label == "Rae (assistant)"


class TestRoute:
    def test_mention_at_start(self, registry):
        result = registry.route("@researcher find papers on X")
        assert result.target_address == "agent@researcher"
        assert result.target_actor == "researcher"
        assert result.content == "find papers on X"
        assert result.metadata["target_actor"] == "researcher"
        assert result.metadata["target_display"] == "Rae (assistant)"

    def test_mention_with_hyphenated_name(self):
        reg = ActorRegistry()
        reg.register("main", FakeMailBox("agent@main"), is_default=True)
        reg.register("code-reviewer", FakeMailBox("agent@code-reviewer"))
        result = reg.route("@code-reviewer review this")
        assert result.target_address == "agent@code-reviewer"
        assert result.content == "review this"

    def test_no_mention_uses_default(self, registry):
        result = registry.route("hello world")
        assert result.target_address == "agent@main"
        assert result.target_actor is None
        assert result.content == "hello world"

    def test_non_string_content_uses_metadata_target(self, registry):
        content = [{"type": "text", "text": "hello"}]
        result = registry.route(content, metadata={"target_actor": "researcher"})
        assert result.target_address == "agent@researcher"
        assert result.target_actor == "researcher"
        assert result.content == content

    def test_mention_unknown_actor_treated_as_text(self, registry):
        result = registry.route("@unknown do something")
        assert result.target_address == "agent@main"
        assert result.target_actor is None
        assert result.content == "@unknown do something"

    def test_metadata_target_actor_overrides(self, registry):
        result = registry.route("do something", metadata={"target_actor": "reviewer"})
        assert result.target_address == "agent@reviewer"
        assert result.target_actor == "reviewer"
        assert result.content == "do something"

    def test_mention_wins_over_metadata(self, registry):
        result = registry.route("@researcher find papers", metadata={"target_actor": "reviewer"})
        assert result.target_address == "agent@researcher"
        assert result.target_actor == "researcher"
        assert result.content == "find papers"

    def test_no_default_raises(self):
        reg = ActorRegistry()
        reg.register("helper", FakeMailBox("agent@helper"))
        with pytest.raises(KeyError):
            reg.route("hello")

    def test_leading_whitespace_stripped_before_mention_match(self, registry):
        result = registry.route("  @researcher find papers")
        assert result.target_address == "agent@researcher"
        assert result.target_actor == "researcher"
        assert result.content == "find papers"

    def test_mention_at_end_of_string_without_trailing_content(self, registry):
        result = registry.route("@researcher")
        assert result.target_address == "agent@researcher"
        assert result.target_actor == "researcher"
        assert result.content == ""

    def test_mention_with_only_trailing_whitespace(self, registry):
        result = registry.route("@researcher   ")
        assert result.target_address == "agent@researcher"
        assert result.target_actor == "researcher"
        assert result.content == ""

    def test_content_without_leading_mention_preserved(self, registry):
        result = registry.route("hello @researcher")
        assert result.target_address == "agent@main"
        assert result.content == "hello @researcher"

    def test_mention_case_insensitive(self, registry):
        result = registry.route("@Researcher find papers")
        assert result.target_address == "agent@researcher"
        assert result.target_actor == "researcher"
        assert result.content == "find papers"

    def test_mention_case_insensitive_upper(self, registry):
        result = registry.route("@RESEARCHER find papers")
        assert result.target_address == "agent@researcher"
        assert result.target_actor == "researcher"
        assert result.content == "find papers"
