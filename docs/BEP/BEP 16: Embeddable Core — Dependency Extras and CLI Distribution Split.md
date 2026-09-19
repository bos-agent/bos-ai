# BEP 16: Embeddable Core — Dependency Extras and CLI Distribution Split

- **Status:** Draft
- **Depends on:** BEP 13 (concentric rings — this BEP packages the import graph BEP 13 already built and guards), BEP 4 (extension points / `bos.exts`), BEP 9 (scaffolding, `llm-full.md`), BEP 6 (configuration architecture)
- **Blocked by:** nothing. **Track A** (§3–§7) is implementable now. **Track B** (§8, ASGI-mountable gateway) is *declared and scoped only* — it is not designed in this BEP and must not be presumed by Track A work.

---

## 1. Motivation

BOS is published as one distribution, `bos-ai`, whose install closure is **190 MB across 77 packages** (measured: clean 3.13 venv, `uv pip install .`, no extras, no dev group). A project that wants to *embed* BOS as a library — call `Agent.run()` from its own process, with its own HTTP layer, its own storage, its own UI — pays for a terminal UI framework, a CLI argument parser, an HTML scraper and a search client it never imports.

BEP 13 already did the hard part. The dependency graph is concentric and one-directional, CI-guarded by seven ring-isolation tests. `bos.core.agent` and `bos.core.actor` import stdlib only. `bos.cli` holds every `click` / `rich` / `textual` / `prompt_toolkit` import in the repo; nothing in `bos` imports `bos.cli`.

What remains is not a restructuring. It is three concrete gaps:

1. **One distribution, one dependency set.** The clean import graph is invisible to `pip`. An embedder cannot ask for the part they use.
2. **`bos-ai` installs a console script.** `[project.scripts] boscli = "bos.cli.entry:main"` means `pip install bos-ai` — the library install — produces a `boscli` executable. The library advertises itself as an application.
3. **No worked embedding path.** `Workspace.__init__` has accepted a plain `dict` since BEP 6, and `Workspace.from_discovery()` (the TOML/file path) is called only from `bos.cli` and `bos.runner`. Storage-agnostic configuration is already true and untested as a supported path, with no example and no guard.

### 1.1 What this BEP is *not* fixing

A separate `ConfigStore` port, a `bos-core` PyPI distribution, and a repo split were each considered and rejected in §2.2 with reasons. The measured evidence is that none of them buy anything the extras split does not.

---

## 2. Goals and Non-Goals

### 2.1 Goals

1. `pip install bos-ai` yields a **library**: no console script, and an install closure of roughly **14 MB / 16 packages** (measured for the base dependency set below).
2. Every dependency that serves exactly one ring or one adapter is an **extra**, named for the capability it enables.
3. The `boscli` command keeps working, byte-identically, via the existing `boscli` distribution.
4. `import bos.exts` succeeds on a base install, with any adapter whose optional dependency is absent skipped and logged — not raised.
5. A runnable example proves the embedding path: construct a `Workspace` from an in-memory dict, run a turn, touch no TOML file and no gateway.
6. A CI job that installs the base distribution in a clean environment and imports the library rings — the only check that can catch an extras regression.

### 2.2 Non-Goals

1. **A `bos-core` PyPI distribution, or a separate repo.** The ring guards already enforce the boundary at the level that matters — import direction. A second distribution adds a second version line, namespace-package configuration, and a `semantic-release` multi-package rework, in exchange for a boundary CI already enforces. A second *repo* additionally converts every cross-ring change into a two-repo release.
2. **Moving `bos/cli/` into the `boscli` repo.** `bos.cli` has 16 import statements against modules that are *not* the declared public API (`bos.runner.proc` ×7, `bos.gateway.state` ×3, `bos.gateway.client` ×2, `bos.plugins.skills.plugin`, `bos.plugins.memory.plugin`). CLAUDE.md declares `src/bos/core/__init__.py` as the public surface; none of these are in it. A repo boundary would freeze them into a cross-repo contract. It would also relocate ~1000 lines of tests including `test_cli_ring_isolation.py` — the guard that proves the outermost ring imports only inward — out of the repo whose graph it guards, and would remove `boscli ask`, the cheapest full-stack smoke path bos-ai has. See §3.7 for why the `boscli` distribution stays code-free.
3. **A `ConfigStore` port or any new configuration abstraction.** Storage-agnostic config already works (§3.5). Adding an interface with one implementation is the thing to avoid.
4. **Narrowing `bos.core.__init__`.** It re-exports 27 `_`-prefixed helpers, documented in CLAUDE.md as exported-for-extensions and explicitly unstable. There is no external embedder yet; a breaking change for a user who does not exist is waste. §3.6 documents the contract subset instead.
5. **Any change to ring topology, turn semantics, config keys, wire protocol, or CLI behavior.** Track A is a packaging and distribution change.
6. **Designing the ASGI gateway.** §8 declares the dependency and names the unresolved forks. Track A introduces no seam, adapter, or indirection in anticipation of it.

---

## 3. Design

### 3.1 Runtime shape

Track A introduces no process, actor, job, or service. Its artifacts are:

| Artifact | Runtime form | When it runs | Who invokes it |
|---|---|---|---|
| Extras in `pyproject.toml` | Build/install metadata | Resolution time | `pip` / `uv` |
| `_optional()` in `bos/exts.py` (§3.4) | Module-level function | Once, at `import bos.exts` | Whatever imports `bos.exts` — normally the `[platform] extensions` loader |
| `examples/embed_fastapi.py` | Standalone script, not shipped in the wheel | Manually, or by the CI job in §6.6 | A developer reading it |
| Clean-install CI job | GitHub Actions job | Per push/PR | CI |

Nothing acquires a new lifecycle.

### 3.2 What ships where

**Base dependencies** — every one is imported by a ring at or inside `bos.config`, which is the innermost set an embedder needs:

| Package | Sole consumer | Import style |
|---|---|---|
| `jsonschema>=4.21` | `core/defaults/structured_validator.py:13` | module top-level |
| `filelock==3.25.2` | `core/_utils.py:116` | function-local |
| `pydantic==2.12.5` | `config/schema.py`, `config/workspace.py` | module top-level |
| `python-dotenv==1.2.2` | `config/workspace.py`, `cli/commands/doctor.py` | module top-level |
| `pyyaml==6.0.3` | `plugins/memory/markdown_backend.py` | module top-level |

Measured closure: **14 MB / 16 packages**.

**Extras:**

```toml
[project.optional-dependencies]
litellm = ["litellm==1.84.0"]                                    # +105 MB
gateway = ["aiohttp==3.13.5"]                                    # +10 MB
search  = ["ddgs==9.14.4", "beautifulsoup4==4.14.3"]             # +35 MB
lark    = ["lark-oapi==1.6.8"]                                   # +98 MB
cli     = [
  "bos-ai[gateway]",                                             # bos.cli imports bos.gateway.client
  "click==8.3.2", "rich==14.3.3", "textual==8.2.3",
  "prompt_toolkit==3.0.52", "textual-autocomplete==4.0.6",
]                                                                # +12 MB over gateway
all     = ["bos-ai[litellm,gateway,search,lark,cli]"]
```

Rationale per extra:

- **`litellm`** is the single largest contributor (litellm 63 MB + tokenizers 12 MB + hf_xet 12 MB + openai 11 MB + tiktoken 3.7 MB + huggingface_hub 3 MB). It serves exactly one adapter, `core/defaults/litellm_provider.py`, plus a token-count call in `core/defaults/jsonl_chat_store.py:132`. Both imports are already function-local, so the base install imports `bos.core.defaults` cleanly without it. **Consequence, stated plainly: a base install has no registered LLM provider.** See §5.3 for the error contract.
- **`gateway`** is `aiohttp`, consumed by `bos/gateway/*` and by `extensions/channels/telegram.py:14` (module top-level — see §3.4). `bos.runner` has no third-party imports of its own but imports `bos.gateway`, so `python -m bos.runner` requires this extra.
- **`search`** is the `ddgs` + `beautifulsoup4` chain behind `extensions/tools/knowledge.py` (primp 15 MB + lxml 12 MB + brotli 5 MB + fake_useragent 2.6 MB).
- **`lark`** is unchanged from today, and stays out of every aggregate but `all`: 98 MB for one channel.
- **`cli`** declares `bos-ai[gateway]` recursively because `bos/cli/commands/agent.py` imports `bos.gateway.client.GatewayClient` for the TUI's WebSocket connection. Recursive extras are standard PEP 508 and resolved by both `pip` and `uv`.

### 3.3 Dead dependencies removed

Three lines in `[project]`/`[project.optional-dependencies]` have no consumer. Verified by repo-wide grep for both `import X` and `from X`:

| Dependency | Finding |
|---|---|
| `tomlkit==0.15.0` | Zero references in `src/` or `tests/`. Introduced for BEP 9's TOML-mutation design; the shipped `cli/scaffold/` engine does not use it. |
| `tomli==2.0.1; python_version<'3.11'` | `requires-python = ">=3.13"`, so the environment marker can never evaluate true. Dead line, not a dead package. |
| `tavily-python==0.7.23` (in the current `search`/`all` extras) | Never imported. `extensions/tools/knowledge.py:155` calls `https://api.tavily.com/search` directly with `urllib.request`. The Tavily provider keeps working without the SDK. |

Removing them is behavior-preserving and independent of the rest of this BEP.

### 3.4 `bos.exts` must degrade when an optional dependency is absent

[`src/bos/exts.py`](../../src/bos/exts.py) imports every built-in adapter unconditionally, but wraps entry-point-discovered extensions in `try/except` with a warning. That asymmetry is invisible today because every built-in adapter's dependency is a base dependency, and because `extensions/channels/lark.py` already defers `lark_oapi` into function bodies.

After §3.2, three built-ins acquire a missing top-level dependency on a base install:

- `extensions/tools/knowledge.py:10-12` — `from bs4 import BeautifulSoup`, `from ddgs import DDGS`, `from ddgs.exceptions import DDGSException` → `search`
- `extensions/channels/telegram.py:14` — `from aiohttp import ClientSession, ClientTimeout, FormData` → `gateway`
- `extensions/channels/lark.py:34` — `from bos.gateway import ...`, which imports `aiohttp` → `gateway`

The third is the one a source-level audit misses: `lark.py` already defers its own `lark_oapi` SDK into function bodies, so it looks safe, but it imports `bos.gateway` for `ChannelRuntimeContext` at module level. Both channels therefore need the `gateway` extra to be *importable*, independently of the channel-specific SDK each needs to *run*.

Patching those modules to defer their imports would fix today's three symptoms and leave the asymmetry for the next adapter that moves to an extra. **The fix belongs in `exts.py`**, where all built-in imports converge: an `_optional(module_path, extra)` helper that imports, catches `ModuleNotFoundError`, and logs which extra to install — the same contract the entry-point loop already has. Built-ins whose dependencies are base dependencies keep a plain `import`.

The warning names the extra, so the failure is self-describing:

```
Extension 'bos.extensions.tools.knowledge' not loaded: missing dependency
'ddgs'. Install bos-ai[search] to enable it.
```

**Precise scope:** this makes `import bos.exts` succeed; it does not make the adapter available. A config that names a tool from a skipped module still fails at resolution time, with that module's warning already in the log.

### 3.5 Ownership: configuration storage (already resolved; recorded here)

No change is proposed. This section exists because "pluggable config storage" was a stated motivation and the answer is that BEP 6 already delivered it:

| Concern | Owner | Note |
|---|---|---|
| Config *shape* | `bos.config.schema` (`RootConfig` et al.), and `bos.gateway.config` for gateway shapes | BEP 13 §3.2–3.3 |
| Config *source* | The caller of `Workspace(...)` | `Workspace.__init__` accepts `dict \| RootConfig` |
| File discovery / TOML parsing | `Workspace.from_discovery()` | Called **only** from `bos/cli/commands/agent.py`, `bos/cli/commands/scaffolding.py`, `bos/runner/__main__.py` — the two outermost rings. Its own docstring: *"the legacy convenience constructor. Prefer explicit construction in new code."* |
| `bos_dir` | The caller | `AgentHarness` resolves it to a `Path` and passes it to plugin services and the prompt-template context (`harness.py:343`, `harness.py:513`). Filesystem persistence is a property of the default `JsonlChatStore`/`JsonlMailRoute` adapters, not of the harness. |

An embedder supplies a dict from any source and never touches a file. §3.6's example is the executable proof, and it is the only new thing this BEP adds here.

### 3.6 The embed contract surface

`bos.core.__init__` re-exports 27 `_`-prefixed helpers. They stay (§2.2.4). What changes is documentation: README and the docs site gain an **Embedding** page stating the supported surface —

- `bos.core`: `AgentHarness`, `Agent`, `AgentResult`, the `ep_*` extension points, and the port `Protocol`s (`LLM`, `ChatStore`, `Consolidator`, `ToolSet`, `TurnInterceptor`, `PromptProvider`, `TurnEventSink`)
- `bos.config`: `Workspace`, `RootConfig`, `validate_config`
- everything `_`-prefixed: available to extensions, explicitly unstable, not part of the embed contract

and `examples/embed_fastapi.py`: an in-memory config dict → `Workspace(...)` → `bootstrap_platform()` → `harness()` → `agent.run()`, served from a FastAPI route. It demonstrates adapter injection two ways — a stub `@ep_provider` registered in the example file itself, and `[harness] chat_store = "InMemChatStore"` selecting the built-in in-memory adapter by name — with `[platform] extensions = []` so the embedder imports only the adapters they want rather than all of `bos.exts`. It therefore runs offline, writes nothing to disk, and imports neither `bos.cli` nor `bos.gateway`. The file is an example, not shipped in the wheel, and FastAPI is not a project dependency — §6.6 covers how CI runs it.

### 3.7 Distinguishing three look-alikes

| Name | What it is | Contains code? |
|---|---|---|
| **`bos-ai`** (PyPI distribution) | The library. After this BEP: no console script. | Yes — all of `src/bos/`, including `bos/cli/` |
| **`boscli`** (PyPI distribution, separate repo `bos-agent/boscli`) | A name shim so `uvx boscli` resolves. Exists because tool runners assume PyPI name == command name. | **No** — `src/boscli/__init__.py` is 2 lines; its `[project.scripts]` already targets `bos.cli.entry:main`, a *bos-ai* module |
| **`bos.cli`** (Python package) | The CLI implementation and BEP 13's leaf composition root | Yes — ~4000 lines, stays in `bos-ai` |

The `boscli` distribution's daily auto-release works precisely *because* it holds no code: [`.github/scripts/sync_version.py`](https://github.com/bos-agent/boscli) fetches bos-ai's latest PyPI version and rewrites two strings, so its version is a pure function of bos-ai's. Moving code there would make that function ill-defined — a repo with real changes cannot be auto-bumped to match another repo's version — and would force CLI development against unreleased bos-ai builds. This is the mechanism behind §2.2.2.

`bos-ai` itself keeps a module entry point, `python -m bos.cli`, added as `src/bos/cli/__main__.py` alongside the existing `bos/runner/__main__.py`. Without it, `pip install bos-ai[cli]` would install the CLI's dependencies with no way to invoke the CLI, and this repository's own `uv run boscli ...` workflow would have no replacement. The `boscli` command remains the packaged, user-facing form.

**Change to `boscli`** (one string plus one regex, in the other repo):

```toml
dependencies = ["bos-ai[cli,litellm,search] == <version>"]
[project.scripts]
boscli = "bos.cli.entry:main"     # unchanged
```

`litellm` is **required** here: without it `boscli ask` has no provider. `search` is included so `uvx boscli` keeps today's web-search behavior; `lark` is not, matching today. `sync_version.py`'s pin-rewriting regex `"bos-ai\s*==\s*[^"]+"` must widen to match the bracketed form.

### 3.8 Mounting `bos.cli` commands into a foreign Click application

Out of scope as a deliverable, recorded because it constrains §2.2.2. `bos.cli.entry.cli` is a `click.Group`, so a host application can mount it. Measured behavior:

| Mounting | Result |
|---|---|
| `host.add_command(bos_cli, "bos")` → `host bos gateway status` | Works. `-c/--config` and the `BOS_CONFIG` fallback are preserved, because bos's root callback still runs. |
| Individual command onto a host root whose callback does **not** call `ctx.ensure_object(dict)` | `AttributeError` — commands read `ctx.obj.get("CONFIG")` |
| Same, with `ctx.ensure_object(dict)` in the host root callback | Works, but `-c` and `BOS_CONFIG` are lost (they live on bos's root group); config resolution falls back to `Workspace.from_discovery(".")`, then the `default` preset |

Two known rough edges, neither addressed here: 32 occurrences of the literal `boscli` in help and error text would misname the command when mounted under another root, and the reverse direction already exists (the `boscli.commands` entry-point group, `entry.py:10`, lets a package inject commands *into* boscli).

This capability requires the CLI code to live in `bos-ai` and be importable as a library. It is **not** the path for a core-only embedder, who depends on `bos-ai` (optionally `bos-ai[litellm]`) and never installs the `cli` extra.

---

## 4. Audience flows (end state)

### 4.1 Embedder — a Python application embedding the agent

```bash
pip install bos-ai[litellm]     # or plain bos-ai + their own @ep_provider
```

Gets `bos.core` and `bos.config`, no console script, no terminal UI, no gateway. Builds config from their own store, registers their own `ChatStore` against `ep_chat_store`, calls `agent.run()` from their own request handler, owns their own HTTP surface and concurrency. `examples/embed_fastapi.py` is the reference. Nothing in this flow reads a TOML file or a `.bos/` directory beyond what their own adapters choose to.

### 4.2 End user — the CLI

```bash
uvx boscli ask "..."            # unchanged
pip install boscli              # unchanged
pip install bos-ai[cli]         # new: CLI deps without the boscli shim, no `boscli` command
```

`uvx boscli` and `pip install boscli` behave exactly as today. The removed path is `pip install bos-ai` followed by `boscli` (§5.1).

### 4.3 Operator — running the gateway

Unchanged. `boscli gateway start` spawns `sys.executable -m bos.runner --config ...` (`cli/commands/agent.py:528`) — the **module**, not the console script — so removing `[project.scripts]` from `bos-ai` does not touch process supervision, PID files, the singleton lock, or `boscli gateway status/stop/restart`.

An operator deploying the gateway without the CLI installs `bos-ai[gateway,litellm]` and runs `python -m bos.runner --config <path>`. That path exists today and is unchanged; this BEP only makes it installable without `click`, `rich`, and `textual`.

### 4.4 Background / automated

- **`boscli` daily sync workflow** — unchanged in shape: fetch bos-ai's latest PyPI version, rewrite version and pin, tag, publish. Only the pin regex changes (§3.7).
- **bos-ai `semantic-release`** — unchanged. One version line, one changelog.
- **CI** — gains one job (§6.6). The seven ring-isolation guards are untouched and must stay green.

---

## 5. Compatibility and fallout

### 5.1 Breaking: `pip install bos-ai` no longer provides the `boscli` command

The intended change, and the only user-visible one. Affected call sites:

| Location | Change |
|---|---|
| `CLAUDE.md` Tooling | `uv run boscli ...` → `uv run python -m bos.cli ...`; this repository's own workflow used the console script |
| `README.md` Quick Start | `pip install bos-ai` + `boscli ask` → `uvx boscli ask` (already the documented alternative) or `pip install bos-ai[cli]` |
| `src/bos/llm-full.md` | Any installation instruction naming `pip install bos-ai` as the way to get the CLI |
| Docs site install pages | Same |
| `cli/scaffold/templates/**` | Audit for generated README/docs that tell a scaffolded project's user how to install |

**Compatibility stance:** no shim, no deprecation period. `uvx boscli` and `pip install boscli` — the paths the README already leads with — are unaffected, so the migration for a CLI user is a one-line install change. A `bos-ai` release note calls it out.

### 5.2 Breaking: optional adapters are absent from a base install

`bos-ai` without `[search]` has no `web_search` / page-fetch tools; without `[gateway]` no Telegram channel and no `python -m bos.runner`; without `[lark]` no Lark channel (unchanged from today). Each is announced at `import bos.exts` by §3.4's warning naming the extra. A config referencing an absent adapter fails at resolution, as it does today for a misspelled name.

### 5.3 Breaking: a base install has no LLM provider

`AgentHarness.__aenter__` constructs `LLMClient()` (`harness.py:328`), whose `__init__` body is `pass`, so **opening the harness still succeeds**.

Resolution succeeds too, which is the part that is easy to get wrong: `core/defaults/litellm_provider.py` registers via the `@ep_provider(name="litellm")` decorator **at import time**, and its `import litellm` is inside the function body. On a base install the provider is therefore *registered*, `ep_provider.has("litellm")` is `True`, and a guard in `LLMClient.complete()` would never fire. The failure can only surface from inside the call, as a bare `ModuleNotFoundError: No module named 'litellm'` raised mid-turn.

The guard accordingly lives in `litellm_complete` itself, around its own import, and raises `ValueError` with the remedy:

```
ValueError: The built-in 'litellm' LLM provider needs the litellm package, which
is not installed. Install bos-ai[litellm], or register your own provider with
@ep_provider.
```

What the user sees depends on the caller. `LLMClient.complete()` raises. `Agent.run()` does **not** propagate it: the turn loop catches provider errors and returns them as the turn's output, so an embedder gets `AgentResult(output="(error: The built-in 'litellm' …)")` and a CLI or channel user reads the message as the agent's reply. Both paths carry the remedy; neither shows a traceback.

This is the one place Track A adds a user-facing string that did not exist. Every documented install path for running an agent (`bos-ai[litellm]`, `bos-ai[cli]` via `boscli`, `bos-ai[all]`) includes a provider, so the message targets a deliberate bare install.

### 5.4 Not breaking

- **Import paths.** Every module keeps its name and location. `bos/cli/*.py` still ships inside the `bos-ai` wheel — extras govern *installation of dependencies*, not which files ship. `import bos.cli.entry` on a base install raises `ModuleNotFoundError: click`, which is ordinary extras semantics.
- **Gateway spawn**, config keys, wire protocol, turn semantics, ring topology (§4.3, §2.2.5).
- **`boscli` for `uvx`/`pipx`/`pip install boscli` users** (§4.2).

### 5.5 Third-party impact

A package declaring the `bos.exts` or `boscli.commands` entry-point groups is unaffected — both mechanisms are unchanged. A third party depending on `bos-ai` for the CLI must move to `bos-ai[cli]`. No such package is known.

---

## 6. Implementation plan (dependency-ordered)

Each step rests on one already completed. Steps 1–3 are one PR in `bos-ai`; step 4 is a PR in `bos-agent/boscli` that must land **after** the bos-ai release; steps 5–7 follow in `bos-ai`.

1. **Remove the three dead dependencies** (§3.3). Behavior-preserving and independent; lands first so later steps edit a clean dependency table. Verify: `uv run pytest -q` green, `uv sync` resolves.
2. **Add `_optional()` to `bos/exts.py`** and route the two built-ins that will lose a base dependency through it (§3.4). Do this **before** the extras split — while the dependencies are still present, so the change is behavior-preserving and reviewable on its own. Add a unit test that blocks a module in `sys.modules` and asserts `import bos.exts` still succeeds with a warning naming the extra.
3. **Split the dependency table into extras** (§3.2) and **delete `[project.scripts]`** (§3.3, §5.1). Only now does step 2 become load-bearing.
4. **`bos-agent/boscli`:** widen `sync_version.py`'s pin regex and set `dependencies = ["bos-ai[cli,litellm,search] == <v>"]` (§3.7). Requires the bos-ai release from step 3 to exist on PyPI, because the sync script reads it.
5. **Add the `ep_provider`-missing error message** (§5.3). Independent of 1–4; sequenced here because it is only reachable once the base install can lack `litellm`.
6. **Add `examples/embed_fastapi.py` and the clean-install CI workflow** (§2.1.5–6). The repository currently has no workflow that runs tests — `release.yml` is `workflow_dispatch`-only and `pr-title.yml` checks the PR title — so this is a **new** workflow file, `.github/workflows/packaging.yml`, not a job added to an existing one. It creates a fresh venv, installs the base distribution, asserts `import bos.core`, `import bos.config` and `import bos.exts` all succeed and that no `boscli` executable was produced; a second step installs `fastapi` into that same base venv and runs the example, which supplies its own stub provider and so needs no extra. FastAPI is **not** added to any project dependency group — the workflow installs it.
7. **Documentation** (§3.6, §5.1): the Embedding page, the README Quick Start edit, `llm-full.md` and scaffold-template install-instruction audit, and a release note for the breaking change.

---

## 7. Acceptance criteria

Each states its preconditions.

1. Given a clean Python 3.13 venv and `pip install <bos-ai wheel>`: the closure is ≤ 20 MB, `import bos.core`, `import bos.config` and `import bos.exts` all succeed, and no `boscli` executable is created in the venv's `bin/`.
2. Given the same base install: `import bos.exts` emits a warning naming `bos-ai[search]` for the knowledge tools and `bos-ai[gateway]` for the Telegram channel, and raises nothing.
3. Given the same base install (no `[litellm]`): constructing an `AgentHarness` and running a turn returns an `AgentResult` whose `output` carries the §5.3 message, and calling `LLMClient.complete()` directly raises `ValueError` with it — in neither case a bare `ModuleNotFoundError`.
4. Given a base install plus `fastapi`: `examples/embed_fastapi.py` serves a turn end to end from an in-memory config dict against its own stub `@ep_provider`, leaving its `bos_dir` empty and importing neither `bos.cli` nor `bos.gateway`. It needs no `[litellm]` extra, which is itself the point — an embedder supplying its own provider installs nothing beyond the base.
5. Given `pip install '<wheel>[cli]'`: every `boscli` subcommand's `--help` renders, and `gateway start/status/stop` behaves as on today's `main`.
6. Given the published `boscli` distribution built from step 4: `uvx boscli ask "..."` succeeds against a configured provider, matching today's behavior.
7. `uv run pytest -q`, `uv run ruff check src tests`, and `npx -y pyright src` are green, with pyright at zero errors. All seven ring-isolation guards pass unmodified.

---

## 8. Track B: ASGI-mountable gateway (declared, not designed)

**Readiness: blocked — not implementable from this BEP.** Track B needs its own BEP. It is named here so Track A's scope boundary is explicit and so no Track A reviewer adds a seam for it.

The goal is that a host ASGI application (FastAPI, Starlette, Litestar) can mount the gateway's protocol surface instead of supervising a separate process. The surface is small — four routes and one WebSocket endpoint (`/api/status`, `/api/actors`, `/api/upload-image`, `/api/upload`, `/ws`; `gateway/http.py:56-60`) — but it is built on `aiohttp.web`, which is not ASGI.

Unresolved before that BEP can be written:

1. **Replace `aiohttp` or run both transports?** Fallout of replacing: `gateway/http.py`, `gateway/gateway.py`, `gateway/channels/ws_channel.py`, `gateway/client.py` (the TUI's WebSocket client), `extensions/channels/telegram.py`, `bos/runner/`, and seven `tests/test_gateway_*.py` files.
2. **Splitting `Gateway`'s two responsibilities.** It currently owns both the protocol surface and the process lifecycle (run directory, PID file, singleton lock, state file). An embedder wants only the former. This split is Track B's first step and can be done without changing the transport — but it is **not** part of Track A, by the explicit decision that Track A introduces no anticipatory structure.

**Track A does not depend on Track B, and Track B's design must not be assumed by Track A work.**

---

## 9. Open questions

None outstanding.

- **`search` in the `boscli` pin** — resolved: included. The CLI's default configuration enables the web-search tools, so omitting the extra would change `uvx boscli` behavior rather than merely slim it. The +35 MB on first `uvx` run is accepted (§3.7).
- **De-hardcoding the 32 `boscli` literals** in help and error text (§3.8) — resolved as **deferred, out of scope**. They are invisible unless a host application mounts the Click group, which no audience in §4 does. Revisit only if Track B or a concrete host makes mounting real.

---

## 10. Revision history

- 2026-09-19 — Implementation finding, §5.3 corrected again. The guard does **not** belong in `LLMClient.complete()`: `litellm_provider.py` registers at import time while its `import litellm` is function-local, so on a base install `ep_provider.has("litellm")` is `True` and such a guard never fires. Verified against a real base install — the guard belongs in `litellm_complete` around its own import. Also recorded that `Agent.run()` does not propagate the `ValueError`; the turn loop returns it as the turn's output, which is how a CLI or channel user actually sees it. §7.3 restated accordingly.
- 2026-09-19 — Implementation findings, two corrections. §3.4 listed two built-ins losing a base dependency; it is three — `extensions/channels/lark.py:34` imports `bos.gateway` at module level, so it needs the `gateway` extra to import even though its own SDK import is already deferred. §3.3/§5.1 did not say how to run the CLI once the console script is gone: `src/bos/cli/__main__.py` is added so `python -m bos.cli` works, without which `bos-ai[cli]` installs dependencies for a CLI that cannot be invoked, and `CLAUDE.md`'s `uv run boscli` workflow breaks with no replacement.
- 2026-09-19 — Pre-implementation code re-check, two corrections. §5.3 claimed provider resolution fails at `AgentHarness.__aenter__`; it does not — `LLMClient.__init__` is a no-op and the failure surfaces at the first `complete()` call, so the guard belongs in `core/llm.py` and keeps the existing `ValueError` type. §6.6 assumed an existing CI workflow to add a job to; the repository has none that runs tests (`release.yml` is `workflow_dispatch`-only), so the step creates `.github/workflows/packaging.yml`.
- 2026-09-19 — Review pass: both §9 open questions closed. `search` stays in the `boscli` pin because the CLI's default config enables the web-search tools; de-hardcoding the `boscli` literals is deferred out of scope. No design change.
- 2026-09-19 — Draft. Decisions: single distribution with extras over a `bos-core` package or repo split (§2.2.1–2); CLI implementation stays in `bos-ai` while `[project.scripts]` moves to the `boscli` distribution (§3.3, §3.7); `litellm` becomes an extra and a base install has no provider (§3.2, §5.3); the `bos.exts` degradation fix goes in `exts.py` rather than in the two affected adapter modules (§3.4); `bos.core.__init__` is left unchanged and the embed contract is documented rather than enforced (§2.2.4, §3.6); no `ConfigStore` abstraction, because `Workspace(config=dict)` already provides storage-agnostic configuration (§2.2.3, §3.5); ASGI gateway split into Track B with no anticipatory seam in Track A (§2.2.6, §8). Measured figures (190 MB / 77 packages today; 14 MB / 16 packages base; 24 MB / 24 packages base+gateway) from clean 3.13 venvs. Three dead dependencies found and scheduled for removal: `tomlkit`, the unreachable `tomli` marker line, and `tavily-python` (§3.3).
