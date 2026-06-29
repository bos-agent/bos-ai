# Memory & skills

BOS gives agents two mechanisms for working knowledge that persists beyond a single
turn: **memory** (facts about the world and the user, maintained automatically across
sessions) and **skills** (Markdown playbooks the agent loads on demand when it needs
detailed guidance for a task). This page explains what each mechanism is, how it
works, and the key configuration knobs.

For authoring custom skills see [Extending BOS](../extending/index.md). For exact
configuration keys see [Configuration](../configuration/index.md). For the hands-on
tutorial see [Tutorials](../tutorials/index.md).

---

## Memory

### What it is

**Memory** is platform-managed, persistent knowledge scoped to an actor. It is not
a conversation transcript — it is a curated set of _maxims_ (short, declarative
facts) that the agent recalls across sessions.

The agent does not write to memory directly during a turn. Instead, memory
consolidation runs **off-turn**: when the job runner fires a consolidation job, the
`LLMConsolidator` reads recent conversation history, extracts or updates maxims, and
stores them. This keeps the critical response path fast.

### The consolidation loop

```
agent turn (in-process, fast)
        │
        │  session closes or idle trigger fires
        ▼
job_runner submits consolidation job
        │
        ▼
LLMConsolidator reads chat history
        │
        ▼
Distills and writes maxims to MemoryPlugin store
        │
        ▼
Next agent turn sees updated maxims in system prompt
```

The consolidator is selected in `[harness]`:

```toml
[harness]
consolidator = "LLMConsolidator"   # reads history, writes condensed maxims
job_runner   = "InProcJobRunner"   # schedules consolidation off-turn
```

### MemoryPlugin

`MemoryPlugin` is the built-in plugin that exposes memory to agents. It is enabled
by default in the built-in agent spec. Configure it per agent (or project-wide via
`[agent.defaults.plugin-bindings.MemoryPlugin]`):

```toml
[agents.main.plugin-bindings.MemoryPlugin]
maxims = ["user", "self", "rules"]   # which maxim categories the agent maintains (default)

[agents.main.plugin-bindings.MemoryPlugin.consolidation]
enabled        = false             # off-turn consolidation is OFF by default
retention_days = 30                # maxims older than this are pruned
model          = ""                # consolidation model; defaults to BOS_CONSOLIDATOR_MODEL
```

**`maxims`** is the set of maxim *categories* the agent tracks and surfaces in its
prompt — the default is `["user", "self", "rules"]` (facts about the user, the agent
itself, and standing rules). It is **not** a list of pre-written memory strings.

**`consolidation.enabled`** is `false` by default: turn it on to have the job runner
distill maxims off-turn (`retention_days` prunes stale maxims; `model` overrides the
consolidation model, otherwise `BOS_CONSOLIDATOR_MODEL`).

!!! note "Memory isolation is automatic — there is no `scope` option"
    Each agent's memory is isolated by its **agent identity** (the actor name), so two
    actors keep separate memories with no configuration. `scope` is **not** a valid
    MemoryPlugin setting — including it raises a validation error.

### Memory admin

`boscli memory` provides admin subcommands to inspect, edit, and clear the memory
store for any actor without modifying config or restarting the gateway. Use it when
debugging unexpected agent behaviour or bootstrapping a new deployment.

---

## Skills

### What they are

A **skill** is a Markdown playbook stored as a file the agent can load on demand.
Skills implement _progressive disclosure_: the system prompt lists only the skill's
name and a one-line description; the agent calls the `LoadSkill` tool when it
decides it needs the full instructions. This keeps the context window lean while
making deep guidance available.

```
system prompt (every turn):
  Available skills:
  • coding-discipline — Behavioral guidelines to reduce LLM coding mistakes.
  • python            — Python-specific conventions for this project.

agent decides it needs detail → calls LoadSkill("coding-discipline")
        │
        ▼
full skill body injected into the conversation
```

### The SKILL.md format

Each skill is a directory containing a `SKILL.md` file. The directory name is the
skill's name. The file has a short YAML-ish frontmatter block followed by the full
instruction body:

```markdown
---
name: coding-discipline
description: Behavioral guidelines to reduce common LLM coding mistakes. Load before writing code.
---
## 1. Think Before Coding
...
```

The `description` is what appears in the system prompt listing. The body (everything
after the frontmatter) is returned by `LoadSkill`.

### skill_dirs and the `__builtin__` sentinel

`SkillsPlugin` discovers skills by scanning a list of parent directories. The list
is configured under `[exts.pep_skills_loader.FileSystemSkillsLoader]`:

```toml
[exts.pep_skills_loader.FileSystemSkillsLoader]
skill_dirs = ["__builtin__", "skills"]   # default value
```

The **`__builtin__` sentinel** expands, in place, to:

1. The packaged skills that ship with BOS (`bos.skills`).
2. Skills contributed by installed Python packages via the `bos.skills` entry-point
   group.

Any explicit directory after `__builtin__` is resolved relative to `bos_dir`.
**Later entries win on name collision**, so workspace skills override packaged
skills, and package-contributed skills override the BOS built-ins. This means you
can shadow any built-in skill by creating a directory of the same name in your
`skills/` folder.

Ordering examples:

| `skill_dirs` value | Effective search order (last wins) |
|---|---|
| `["__builtin__", "skills"]` | built-ins → package contributions → `bos_dir/skills/` |
| `["skills", "__builtin__"]` | `bos_dir/skills/` → built-ins → package contributions |
| `["skills"]` | only `bos_dir/skills/`; no built-ins |

### Visibility controls

Configure which skills are visible and loadable via `SkillsPlugin` bindings:

```toml
[agents.main.plugin-bindings.SkillsPlugin]
preload = ["coding-discipline"]    # load these fully into the prompt at startup
allow   = []                       # if non-empty, only these skills are listed
exclude = []                       # skill names never listed or loadable
```

- **`preload`**: skill names to inject fully into the system prompt at startup —
  skips the `LoadSkill` round-trip. Use for skills the agent should _always_ apply
  without being prompted.
- **`allow`**: an explicit allow-list. When non-empty, only listed skills appear in
  the prompt and are loadable.
- **`exclude`**: an explicit block-list. Named skills are removed from the listing
  regardless of `allow`.

### Built-in skills

BOS ships three skills in the `__builtin__` set:

| Skill | Description |
|-------|-------------|
| `coding-discipline` | Behavioral guidelines to reduce common LLM coding mistakes. Load before writing code. |
| `python` | Python-specific conventions (type hints, error handling, testing patterns). |
| `skill-creator` | Instructions for writing and structuring new SKILL.md files. |

### Contributing skills from a package

Install a Python package that declares the `bos.skills` entry point, and its skill
directories will slot in at the `__builtin__` position automatically — no config
change needed in the consuming project:

```toml
# In the package's pyproject.toml:
[project.entry-points."bos.skills"]
my-package = "my_package.skills"   # package whose directory holds skill subdirs
```

For a full walkthrough of authoring and packaging skills see the Extending section.
