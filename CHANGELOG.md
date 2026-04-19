# CHANGELOG


## v0.1.0 (2026-04-19)

### Bug Fixes

- Skill loader and other bugs fix. update on the config template.
  ([#2](https://github.com/bos-agent/bos-ai/pull/2),
  [`493864c`](https://github.com/bos-agent/bos-ai/commit/493864c57a28247949cb8df1643860bdc9b86265))

* fix the regression

* fix the bugs in skill loader

### Chores

- Setup PSR and github flow ([#1](https://github.com/bos-agent/bos-ai/pull/1),
  [`1bcb8e2`](https://github.com/bos-agent/bos-ai/commit/1bcb8e2a2c68d63ad145b33aee4d6a45b9149d41))

* add homepage link and license

* setup PSR and cicd

### Features

- Mailbox redesign to mailroute + mailbox bound with address
  ([#5](https://github.com/bos-agent/bos-ai/pull/5),
  [`fb7dc69`](https://github.com/bos-agent/bos-ai/commit/fb7dc695f2d2c77d5bde4c09be0ec1b78d8f5980))

* Bind mailbox ownership to addresses instead of caller-provided senders

The runtime now separates unbound mail routing from bound single-address mailboxes so actors,
  channels, and tools carry identity by construction instead of by convention. This also keeps a
  compatibility layer for the previous mailbox names and legacy harness config while migrating the
  default template and runtime wiring to the new split.

Constraint: Preserve current channel and actor behavior while removing ordinary sender spoofing from
  the default API

Constraint: Keep the old mailbox imports and config path working for at least this review cycle

Rejected: Ship a hard rename with no compatibility aliases | would silently break legacy config and
  imports

Rejected: Keep SendMail actor-only with no fallback binding | breaks direct harness-created agent
  and subagent use

Confidence: high

Scope-risk: moderate

Reversibility: clean

Directive: Treat MailRoute as the privileged transport surface; new runtime code should depend on
  bound MailBox capabilities unless sender preservation is explicitly required

Tested: uv run pytest -q (22 passed)

Tested: uv run ruff check src tests

Tested: LSP diagnostics directory (0 errors, 0 warnings)

Not-tested: Third-party external channel extensions still using the old run(mailbox, address)
  signature

* Remove fake recipient routing from the TUI HTTP client

The TUI now depends directly on HttpChannelClient semantics instead of carrying a mailbox-shaped
  send API with a meaningless empty recipient argument. This keeps the client honest about the
  transport contract and makes the call sites easier to read.

Constraint: Preserve the current HttpChannel server behavior where the server chooses the configured
  target address

Rejected: Keep HttpChannelClient mailbox-like with recipient="" convention | hides
  transport-specific behavior behind a misleading API

Scope-risk: narrow

Directive: Keep HttpChannelClient transport-specific; do not reintroduce generic mailbox semantics
  unless there is a real second client transport to abstract over

Tested: uv run pytest -q tests/test_http_channel.py tests/test_harness.py tests/test_mailbox.py (13
  passed)

Tested: uv run ruff check src/bos/extensions/channels/http_client.py src/bos/cli/tui_app.py src
  tests

Not-tested: Full interactive TUI session against a live agent process

* Prefer current actor mailbox in step relay

AgentStepRelay now uses CURRENT_MAILBOX when the interceptor runs inside an actor-owned ask context,
  and only falls back to reconstructing a bound mailbox from actor metadata when no current mailbox
  is available. This keeps the relay aligned with the capability-based mailbox model instead of
  rebuilding actor identity unnecessarily.

Constraint: Preserve existing relay behavior for contexts where the interceptor does not have a
  current bound mailbox

Rejected: Continue rebinding from actor_address unconditionally | duplicates identity reconstruction
  that the actor already established

Directive: Mailbox-aware runtime helpers should prefer CURRENT_MAILBOX when available and only
  reconstruct from route metadata as a compatibility fallback

Tested: uv run pytest -q tests/test_harness.py tests/test_mailbox.py tests/test_http_channel.py (13
  passed)

Tested: uv run ruff check src/bos/extensions/interceptors/agent_step_relay.py

Not-tested: Full live agent step streaming session against a running UI client

* Remove the fake mailbox compatibility layer

The previous mailbox compatibility shim only preserved names, not the old protocol behavior, which
  made the refactor boundary misleading. This change removes the fake aliases and legacy config path
  so the runtime exposes only the real MailRoute/MailBox model.

Constraint: Prefer an explicit API break over a misleading compatibility story that cannot preserve
  the old mailbox contract

Rejected: Keep Mailbox = MailRoute and mailbox= config alias | import-level survival without
  protocol compatibility is dishonest and fragile

Directive: Only call a migration path compatible when the old call pattern still executes
  end-to-end; import aliases alone are not compatibility

Tested: uv run pytest -q tests/test_harness.py tests/test_mailbox.py tests/test_http_channel.py (12
  passed)

Tested: uv run ruff check src/bos/core/__init__.py src/bos/core/contract.py src/bos/core/defaults.py
  src/bos/core/harness.py src/bos/extensions/mailboxes/jsonl_mailbox.py tests/test_harness.py

Not-tested: Third-party downstream code that still imports or instantiates the removed legacy
  mailbox names/config

* Repair channel runtime wiring after mail-route refactor

The mail-route transition left three concrete runtime defects: the TUI entrypoint instantiated
  ChatApp with the wrong keyword, AgentStepRelay could not import CURRENT_MAILBOX from bos.core
  during bootstrap, and BroadcastChannel still required a bespoke run signature instead of the new
  bound-mailbox channel contract.

This commit fixes those boundaries directly and adds regression tests for bootstrap registration,
  TUI startup construction, and broadcast channel contract behavior. The runner now treats
  BroadcastChannel like the other channel implementations instead of special-casing an extra route
  argument.

Constraint: Keep the new mail-route design intact without reintroducing mailbox compatibility shims

Rejected: Add backward-compatibility aliases for mailbox-era APIs | would preserve the wrong
  abstraction during active refactor

Directive: Keep channel implementations on the single-argument bound-mailbox contract; do not pass
  MailRoute through channel run paths again

Tested: ruff check .; pytest -q

Not-tested: Manual interactive bos chat session against a live HttpChannel

* small refactors

- Run agent in docker container
  ([`ba19b7a`](https://github.com/bos-agent/bos-ai/commit/ba19b7a2807e4610653456c174cf168fa7aa84c6))

- Run agentactor as a standalone process. support channels of http and telegram
  ([#3](https://github.com/bos-agent/bos-ai/pull/3),
  [`bcecf00`](https://github.com/bos-agent/bos-ai/commit/bcecf00c17c22a4f3c3d55512fbd422978da6c95))

* big feature. not tested yet

* inteceptor works. and mailbox address naming

* broadcast channel

* telegaram and broadcast channels

### Refactoring

- Make channel routing explicit and stabilize primary actor address
  ([#6](https://github.com/bos-agent/bos-ai/pull/6),
  [`90573da`](https://github.com/bos-agent/bos-ai/commit/90573daf492aa5f505be8fc260ee3b3188d17a23))

* update license to BSD 2

* Make channel routing explicit and stabilize the primary actor address

Channel wiring now lives in resolved config instead of runner-side special cases. BroadcastChannel
  is treated as a built-in channel with explicit bind/target addresses, shallow topology validation,
  and stable availability through bos.core. The primary actor mailbox is fixed at agent@main while
  the selected agent name is still recorded in runtime state.

Constraint: Channel routing must remain shallow (leaf -> actor or leaf -> broadcast -> actor)

Constraint: Runner should not encode config-specific broadcast wiring policy

Rejected: Keep main.broadcast_address special casing in runner | preserves schema leakage and blocks
  multiple broadcast groups

Rejected: Keep actor address derived from selected agent name | makes channel configs unstable
  across main agent swaps

Confidence: high

Scope-risk: moderate

Reversibility: clean

Directive: Keep routing semantics in Workspace resolution and built-in channel validation, not in
  runner task orchestration

Tested: uv run python -m pytest -q

Tested: uv run ruff check src tests

Not-tested: Live migration of ignored local workspace configs outside tracked files

- **core**: Code structure deep refactoring ([#4](https://github.com/bos-agent/bos-ai/pull/4),
  [`daea7d2`](https://github.com/bos-agent/bos-ai/commit/daea7d24c7ff0a2630c14d602b661299b23a48fd))

* refactor(protocol): introduce shared envelope types

* refactor(workspace): extract workspace module

* refactor(actor): extract agent actor runtime

* refactor(harness): extract harness lifecycle

* chore(runner): clean up unused proc imports

* minor changes

* refactor(core): package runtime and config modules

* refactor(extensions): consolidate extension implementations

* refactor(extensions): separate bootstrap from package root

* refactor(config): move workspace template under config

* refactor(core): extract registry and llm modules

* refactor(core): extract react agent module

* refactor(core): extract state services module

* refactor(core): extract react interceptors module

* refactor(core): split contracts and defaults

* refactor(core): extract private utilities module

* refactor(core): simplify package re-exports

* refactor(core): simplify util re-exports

* refactor(core): merge react interceptors into agent

* refactor(core): remove package back-imports

* docs(architecture): add package boundary overview

* refactor(core): use relative imports and clean annotations

* refactor(core): keep protocol imports absolute
