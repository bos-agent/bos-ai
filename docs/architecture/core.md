# Core

`bos.core` is the runtime framework layer.

## Internal Modules

- `contract.py`
  Runtime service contracts and extension points.
- `defaults.py`
  Built-in `_default` implementations registered on those extension points.
- `agent.py`
  `ReactAgent`, `ReactContext`, `AbortTurn`, and the interceptor chain used by the agent loop.
- `actor.py`
  MailBox-driven actor runtime that wraps an agent.
- `harness.py`
  Lifecycle owner for shared services.
- `llm.py`
  Provider-agnostic LLM response types and `LLMClient`.
- `registry.py`
  Extension system primitives.
- `_utils.py`
  Private helpers shared inside `bos.core`.
- `__init__.py`
  Package surface only.

## `contract.py`

`bos.core.contract` owns:

- runtime `Protocol` definitions such as `MailRoute`, `MailBox`, `Channel`, `Agent`, and stores
- `Message`
- `ep_*` extension points

This is the place to look for the framework’s runtime contracts.

## `defaults.py`

`bos.core.defaults` owns the built-in `_default` implementations, such as:

- `InMemMailRoute`
- `InMemMessageStore`
- `InMemMemoryStore`
- `NaiveConsolidator`
- `FileSystemSkillsLoader`
- `litellm_complete`

The rule is simple: if something is the built-in registered default, it belongs here.

## `_utils.py`

`bos.core._utils` is intentionally private.

It contains shared helper functions that are not part of the public framework contract, including:

- extension invocation helpers
- JSON/text/file helpers
- LiteLLM conversion helpers
- extension loading helpers

These helpers may change more freely than the public `bos.core` surface.
