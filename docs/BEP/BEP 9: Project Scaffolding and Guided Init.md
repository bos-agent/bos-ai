# BEP 9: Project Scaffolding and Guided Init

Status: **accepted — implemented** (reconciled with the implementation 2026-06-11)

---

## Core Insight

BOS supports two usage modes: running the out-of-box presets (`boscli ask`, `boscli -c team gateway start`) and building a custom agent topology in a project — own agents, tools, skills, channels, and plugins. The first mode is well served. The second mode begins with `boscli init`, which copies `template.toml` (a ~90%-commented reference file) into the working directory and stops. A new project builder gets:

1. **No working baseline** — no agents defined, no model configured, no directories (`agents/`, `extensions/`, `skills/`) created. The gateway boots into the Python `_default` agent, and nothing in the project demonstrates how to change that.
2. **No discovery path** — the extension seams (markdown agent specs, `@ep_tool` registration, multi-actor routing, channels, plugin bindings) are only discoverable by reading template comments or source code.
3. **No model/credential setup** — the most common first-run failure is a missing model or API key, discovered only as a runtime error after `gateway start`.
4. **No day-2 support** — adding a second agent or a first custom tool means hand-writing files whose formats the user has never seen.

BEP 9 introduces a `boscli project` command group that turns project setup into a guided flow producing a **runnable, multi-file baseline** that demonstrates every extension seam by example, plus incremental generators (`project add`) and a configuration health check (`project doctor`).

The design metric for this BEP: **time from `pip install bos-ai` to a custom tool call answered in the TUI.** Every flow decision is ranked against that number.

---

## Goals

1. **`boscli project init` — guided, archetype-based scaffolding**:
   - An interactive wizard that asks for the project's purpose, an archetype, and model/provider setup, then writes a complete project: config, agents, an example custom tool, skills directory, `.env`, `.gitignore`, and a project README.
   - Every archetype produces a config that **boots successfully with no further editing** (given valid credentials).
   - Non-interactive operation via flags (`--archetype`, `--model`, `--yes`) for scripting and CI.
2. **Scaffolded files as in-place documentation**: each generated file carries concise comments explaining its format and discovery rules, so the project itself teaches the extension model.
3. **Model and credential setup inside the flow**: provider selection (litellm API-key providers, or the OAuth providers behind `boscli auth`), env var capture into `.env`, and an optional live credential probe before declaring success.
4. **`boscli project add` — day-2 generators** for agents, tools, and channels that reuse the same templates as `init`.
5. **`boscli project doctor` — static health check**: config validation, referenced-path existence, env var and credential presence, with an optional live model probe.
6. **One front door**: the top-level `boscli init` is replaced by `boscli project init`. A `--minimal` flag preserves the old copy-the-template behavior.

---

## Non-Goals

1. **LLM-assisted topology generation**: an agent that drafts agents/workflows/config *structure* from a prose description is explicitly deferred. The baseline must work offline and deterministically; templates created here are the substrate a future assisted mode would parameterize. (Bounded *content* generation inside a fixed topology — the `team` archetype's specialist prose, with a deterministic fallback — is in scope; see *Specialist content generation*.)
2. **Preset changes**: the built-in presets (`default`, `team`) and their `~/.bos/presets/<name>` run-dir mechanics are untouched. Presets serve usage mode one; this BEP serves mode two.
3. **Extension marketplace / remote templates**: archetypes and file templates ship inside the `bos-ai` package only. No fetching templates from the network.
4. **Editing arbitrary existing configs**: generators only append well-defined entries (see TOML mutation rules). There is no general config migration or refactoring tooling. `doctor` is read-only.
5. **New runtime features**: this BEP is CLI + packaged templates only. No changes to harness, actor, gateway, or config schema semantics (BEP 6/7 stand as-is).
6. **Skill authoring tooling**: `init` creates the `skills/` directory and one example skill; a richer `project add skill` generator is deferred until the skills format stabilizes.

---

## CLI Command Surface

### 1. `boscli project init [DIRECTORY]`

Initialize a project in `DIRECTORY` (default `.`). Refuses to run if a config is already discoverable there (same rule as today's `initialize_workspace`), with the hint to use `boscli project add` instead.

```
$ boscli project init
┌ BOS project setup
│
│ What is this agent project for?
│ > A research assistant that monitors fixed-income markets
│
│ Choose a starting topology:
│ > 1. assistant — single agent with memory and skills
│   2. team — a coordinator agent that delegates to specialists
│   3. service — headless HTTP gateway, API-first
│   4. telegram-bot — an agent wired to a Telegram channel
│
│ Choose a model provider:
│ > 1. API key (any litellm model id, e.g. anthropic/claude-…, gpt-…)
│   2. OpenAI Codex subscription (boscli auth codex)
│   3. Gemini CLI subscription (boscli auth gemini-cli)
│   4. Google Antigravity (boscli auth antigravity)
│   5. Skip — configure the model later
│
│ Model id: anthropic/claude-sonnet-4-6
│ ANTHROPIC_API_KEY (stored in .env, leave empty to skip): ****
│
│ Initialize a git repository? [Y/n]
│
├ Wrote bos.toml, agents/, extensions/, skills/, .env, .gitignore, README.md
├ Config validates ✓ · credential probe ✓ (1 model call)
│
│ Next steps:
│   boscli gateway start
│   boscli tui
└ Try: "use the WordCount tool on this sentence"
```

Flags:

| Flag | Effect |
|---|---|
| `--archetype <name>` | Skip the archetype question. |
| `--model <id>` | Skip provider/model questions; implies the API-key path. |
| `--purpose <text>` | Skip the purpose question. |
| `--yes` | Accept defaults for all unanswered questions (archetype `assistant`, model skipped); never prompts. |
| `--minimal` | Old `boscli init` behavior: copy `template.toml` only, no wizard, no scaffold. |
| `--dotbos` | Use `.bos/config.toml` layout instead of root `bos.toml` (carried over). |
| `--git` | Run `git init` + write `.gitignore` without asking (carried over). |
| `--no-probe` | Skip the live credential probe. |

Wizard step semantics:

1. **Purpose** (free text): stored verbatim in three places — the main agent's `system_prompt` preamble, the README's first paragraph, and a `# purpose:` comment at the top of the config. It is user-owned text after that; nothing re-derives from it.
2. **Archetype**: selects the template set (below).
3. **Provider/model**:
   - *API key path*: prompt for a litellm model id and the matching API key env var. The env var name is inferred from a small BOS-maintained prefix → env-var map (`anthropic/` → `ANTHROPIC_API_KEY`, `gpt-`/`openai/` → `OPENAI_API_KEY`, `gemini/` → `GEMINI_API_KEY`, …), shown as the default and overridable at the prompt. The map changes rarely; occasional drift fixes against litellm are an accepted maintenance cost. The key value is written to `.env`; the model id to `[agent.defaults].model`.
   - *OAuth paths*: invoke the corresponding `boscli auth <provider>` flow inline, then write the provider's `[exts.ep_provider.<impl>]` entry and model default.
   - *Skip*: scaffold everything, leave `model` commented with a pointer, and print a warning in next-steps.
4. **Git**: as today's `--git` (default yes when the directory is not already inside a git repo).
5. **Write + verify**: render templates, run `validate_config()` on the result (a scaffold that fails validation is a bug — abort and clean up), then run the credential probe unless `--no-probe`/skipped-model.

The credential probe is one minimal LLM call through the configured provider (`ping` → expect any response). Failure does not roll back the scaffold; it prints the error and points at `boscli project doctor`.

### 2. `boscli project add <kind> <name>`

Incremental generators. All reuse `init`'s template files and all are idempotent-safe: they refuse to overwrite an existing target and print what they created plus any follow-up action.

| Command | Writes | TOML mutation |
|---|---|---|
| `project add agent <name>` | `agents/<name>.md` (frontmatter + prompt skeleton) | none required (agent_dirs discovery); `--actor` additionally appends `[runtime.actors.<name>]` |
| `project add tool <name>` | `extensions/<snake_name>.py` with one `@ep_tool` stub | none (extensions dir discovery) |
| `project add channel telegram` | nothing | appends a `[[runtime.channels]]` entry; prompts for `bot_id` and writes `TELEGRAM_BOT_TOKEN=` placeholder into `.env` |

Generators run from anywhere inside the project (config discovered by the standard upward search). If the relevant discovery directory (`agents/`, `extensions/`) is not in the config's `agent_dirs`/`extensions` lists, the generator says so and prints the exact line to add rather than silently producing a dead file.

### 3. `boscli project doctor`

Read-only diagnosis of the discovered project, exit code 0/1:

```
$ boscli project doctor
✓ config         bos.toml parses and validates (BEP 6 schema)
✓ paths          agent_dirs, extensions, skills dirs exist
✓ agents         3 agent specs load (researcher.md, poet.md, inline main)
✗ env            TELEGRAM_BOT_TOKEN referenced by channel 'telegram:main' but unset
✓ credentials    codex_auth.default.json present
✓ gateway        port 5920 free (no gateway running for this project)
- model probe    skipped (use --probe)
```

Checks, in order: config parse + `validate_config()`; existence of every path referenced by `platform.agent_dirs` / `platform.extensions` (local paths only) / skills plugin bindings / `envfile`; external agent file loadability (frontmatter parse warnings surface here); every `*_env` reference (`api_key_env`, `token_env`, channel `settings.*_env`) resolvable from the environment after applying `[platform.envs]` + `envfile`; presence of `~/.bos/auth/*` credential files for configured OAuth providers; gateway port conflict against a live `gateway.state`. `--probe` adds the live model call per configured provider.

`doctor` never writes. Fix suggestions are printed as commands or config lines, not applied.

### 4. Removal of top-level `boscli init`

`boscli init` is removed from `_LAZY_COMMANDS`; `boscli project init --minimal` covers its behavior. Consistent with BEP 6's stance, no deprecation shim is kept — there is no production usage to migrate. README and template comments update accordingly.

### 5. Config source banner

Every command that resolves a workspace (`ask`, `tui`, `gateway start/stop/status/restart`, `project add`, `project doctor`) prints **one stderr line stating which config source is in use** — always, not only on fallback:

```
Using project: /home/me/my-agent (bos.toml)
Using built-in preset: default (~/.bos/presets/default)
Using config file: /path/to/custom.toml
```

This replaces the earlier idea of a conditional "no project found" hint: the user should never have to guess whether a command hit their project or silently fell back to a preset. The line is emitted from the shared workspace-resolution helpers in `cli/commands/agent.py` (so all commands stay consistent), goes to stderr only (stdout stays pipeable), and is suppressed when stderr is not a terminal.

---

## Archetypes

An archetype is a directory of templates under `src/bos/cli/scaffold/<archetype>/`, plus shared fragments under `src/bos/cli/scaffold/_shared/` (example tool, example skill, `.gitignore`, README skeleton). Adding an archetype is adding a directory — no schema or wizard-code changes.

All archetypes share the common scaffold:

```
<project>/
  bos.toml               # archetype config, real values, sparse comments
  .env                   # captured secrets (gitignored)
  .gitignore             # .env, run/
  README.md              # purpose, topology diagram, file map, next commands
  agents/                # markdown agent specs (auto-discovered)
  extensions/
    project_tools.py     # one working @ep_tool — the teaching artifact
  skills/
    example-skill/SKILL.md
```

| Archetype | Topology | Distinctive content |
|---|---|---|
| `assistant` | one actor (`main`), memory + skills plugins on | the default; minimal config surface |
| `team` | `main` coordinator + two specialist agents in `agents/`, `SubagentPlugin` wired with the specialists enabled; second actor exposed for `@mention` routing | demonstrates `agents/*.md`, `plugin-bindings.SubagentPlugin`, `[runtime.actors.*]`. Specialist content is **generated from the purpose answer** via a predefined meta-prompt (see *Specialist content generation*), falling back to lightly themed static templates when no model is configured or the call fails |
| `service` | one actor, HTTP gateway pinned to a fixed port, `BOS_GATEWAY_API_KEY` generated into `.env` | demonstrates API-first usage; README documents the WS/HTTP endpoints |
| `telegram-bot` | one actor + a `[[runtime.channels]]` TelegramChannel entry | demonstrates channel config + token env wiring |

Every archetype must pass a CI test that scaffolds into a temp dir, validates the config, bootstraps the platform, and creates the main agent (no LLM call). Templates that drift from the schema fail the build, not the user.

### The example tool (`_shared/project_tools.py`)

The single most important scaffold file: the moment a user sees their own registered tool called by their agent, the entire extension-point model is understood. Kept trivial and dependency-free:

```python
"""Project-local tools. Any @ep_tool in this directory is auto-discovered
via [platform.extensions] = [..., "./extensions"]."""
from bos.core import ep_tool


@ep_tool(
    name="WordCount",
    description="Count the words in a text.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Text to count."}},
        "required": ["text"],
    },
)
async def word_count(text: str) -> str:
    return f"{len(text.split())} words"
```

The README's suggested first prompt ("use the WordCount tool …") closes the loop inside the first session.

### The example agent (`agents/<name>.md`)

Generated agent specs use the markdown format (frontmatter → spec fields, body → `system_prompt`) because it is the lowest-friction format for prompt-heavy editing. The `team` archetype ships two; `project add agent` produces the same skeleton:

```markdown
---
description: <one line, shown to the coordinator for delegation>
tools:
  enabled: ["ReadFile", "WebSearch"]
---
You are <name>, a specialist agent for …
```

(Frontmatter is the simple YAML mapping accepted by `_parse_simple_yaml_mapping` — flat keys, inline lists, one level of nested mapping; the body becomes `system_prompt`.)

### Specialist content generation (`team` archetype)

The `team` archetype's two specialist specs are produced by **bounded generation**: one LLM call against the just-configured model, using a predefined meta-prompt packaged with the archetype (`scaffold/team/specialists_prompt.txt`). This is content generation inside a fixed topology — not topology generation, which remains a non-goal.

Mechanics:

- Runs after provider setup and the credential probe (it needs a working model), reusing the same harness LLM client path as the probe.
- The meta-prompt receives the purpose answer and asks for structured output: exactly two specialists, each `{name, description, system_prompt}`, with constraints stated in the prompt (kebab-case names, one-line delegation-oriented description, focused system prompt, no tool references).
- The output is **validated, then rendered through the standard agent skeleton**: names must match the agent-name regex and not collide; description and system_prompt are inserted into the same `.md` template `project add agent` uses. The frontmatter structure, `tools` allow-list, and all config wiring (`SubagentPlugin` enablement, actor entries) come from the deterministic template — the model supplies prose only.
- **Fallback**: if the model was skipped, the call fails, or validation rejects the output (one retry), `init` falls back to the lightly themed static templates (purpose text substituted via `string.Template`) and says so. The team archetype therefore still scaffolds offline and deterministically; generation is an enhancement layered on top.
- `--yes` (model skipped) and `--no-generate` both take the fallback path. The CI scaffold test exercises the fallback templates; the generation path is covered by a unit test with a stubbed provider.

---

## Template Rendering and TOML Mutation Rules

**Rendering**: templates are plain files with `string.Template` (`${purpose}`, `${model}`, `${project_name}`) substitution — no template-engine dependency. Files needing no substitution are copied verbatim.

**Source of truth**: `src/bos/config/template.toml` remains the exhaustive commented reference (reachable via `--minimal`). Archetype configs are intentionally sparse — real values, short comments, a header pointing at the reference template. The two serve different readers and are maintained separately; the CI scaffold test keeps archetypes honest against the schema.

**TOML mutation** (the `project add` cases that must touch config): performed with `tomlkit` (new dependency, CLI-only import) to preserve user comments and formatting. Mutations are strictly **append-only of new tables/array entries** — `[runtime.actors.<new>]`, `[[runtime.channels]]`. Generators never modify or delete existing keys; anything beyond an append is printed as a suggested snippet instead. If `tomlkit` round-tripping fails on a hand-edited file, the generator falls back to printing the snippet.

---

## Implementation Notes

- New module `src/bos/cli/commands/project.py` exposing the `project` click group; registered in `_LAZY_COMMANDS` (`"project": "bos.cli.commands.project:project"`). The existing `init` entry is removed.
- `initialize_workspace()` in `config/workspace.py` stays as the `--minimal` path. The scaffold path gets a sibling `scaffold_workspace(workspace, archetype, answers, *, dotbos)` that owns directory creation and template rendering, returning the list of written paths (for the abort-and-clean-up path and for output).
- Wizard prompts use `click.prompt`/`click.confirm` (already the CLI's interaction idiom — see takeover prompt in `agent.py`); no TUI dependency in `init`.
- The credential probe reuses the harness LLM client against the validated config (`async with workspace.harness()` → one `ask`-less direct provider call), not a parallel code path.
- `doctor` builds on `validate_config()` + `Workspace.resolve_agents()` with checks as small composable functions returning `(status, label, detail)`; `--probe` is the only network-touching check.
- Entry-point CLI plugins (BEP-adjacent, `boscli.commands` group) can extend `project` with their own subcommands via the existing group-merge mechanism — generators for third-party extension kinds need no core changes.

### Implementation reconciliation (2026-06-11)

- **Specialist generation mechanics**: `init` always scaffolds the team archetype with the fallback specialists first (the config must reference concrete names to validate), then — after a successful probe — generates and *replaces* the pristine files: fallback `agents/*.md` are removed, generated ones written, and `team/bos.toml.tmpl` re-rendered with the new names. The config is still untouched-by-the-user at that point, so the overwrite is safe.
- **Banner call site**: emitted from `_get_ws_and_rd()` (which `ask`, `tui`, and all `gateway` subcommands share) and from the `project add`/`doctor` discovery helper.
- **`add tool` filename**: the module file is the snake_case form of the tool name (`FetchQuote` → `extensions/fetch_quote.py`).
- **Git default**: `--git/--no-git`; when unset, defaults to yes only if the directory is not already inside a git work tree (asked interactively, applied silently under `--yes`).
- **Probe/generation client**: both go through `Workspace.from_discovery → resolve_agents → bootstrap_platform → LLMClient().complete(...)` — the same path the runtime uses, no parallel code.

---

## Resolved Design Questions

Decisions from the 2026-06-11 review (details integrated into the sections above):

1. **Config source visibility**: instead of a conditional fallback hint, *every* workspace-resolving command prints an explicit "Using project / preset / config file: …" line (see *Config source banner*).
2. **API-key env inference**: BOS maintains the prefix → env-var map; occasional drift fixes against litellm are an accepted maintenance cost. The prompt default remains overridable.
3. **`team` specialist content** (revised 2026-06-11, second pass): generated from the purpose answer via a predefined meta-prompt against the configured model — prose only, rendered through the deterministic agent skeleton, with lightly themed static templates as the no-model/failure fallback.
4. **Windows pass**: deferred to implementation; `.env` handling and the `git init` default get a Windows check during implementation, with no expected design impact.

---

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-06-11 | Initial BEP 9 draft | Pivot support for the build-your-own-topology use case: guided `project init`, archetype scaffolds, `project add` generators, `project doctor` |
| 2026-06-11 | Design review pass | Resolve open issues: always-on config source banner (replaces fallback hint), BOS-maintained API-key env map, lightly themed team specialists, Windows pass deferred to implementation |
| 2026-06-11 | Specialist generation revision | Team specialists move from static themed templates to bounded meta-prompt generation (prose only, deterministic skeleton + wiring, themed templates as fallback); Non-Goals carve-out clarified |
| 2026-06-11 | Implementation reconciliation | Implemented: `cli/scaffold/` engine + 4 archetypes, `project init/add/doctor`, banner, init removal, tomlkit dependency; documented generation replace-after-probe mechanics, banner call site, snake_case tool filenames, git default |
