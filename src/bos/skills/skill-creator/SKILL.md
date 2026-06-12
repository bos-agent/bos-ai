---
name: skill-creator
description: >
  Create or improve BOS skills (SKILL.md playbooks loaded on demand). Use whenever the user
  wants to write a new skill, turn a workflow from this conversation into a reusable skill,
  edit or package an existing skill, or asks why a skill is missing or not triggering.
---

# Creating BOS skills

A skill is a directory containing a `SKILL.md`: an onboarding guide that turns a
general-purpose agent into a specialist for one task. Make one when a workflow is
worth repeating — multi-step procedures, domain knowledge the model lacks, or
conventions the user keeps re-explaining. Don't make one for what the model
already does well on its own.

## How skills work in BOS

Mechanics that shape how you write the skill:

- **The directory name is the skill's identity.** The loader keys skills by
  directory name; the frontmatter `name` is convention only. Use kebab-case
  (`review-pr`, not `Review PR`), and keep the frontmatter `name` matching it.
- **Only `description` is read for discovery.** Every agent turn renders each
  allowed skill as `<skill name="...">description</skill>` in the system
  prompt. The body is returned only when the agent calls `LoadSkill`.
- **Discovery order, last wins**: builtin skills (packaged with BOS and from
  `bos.skills` entry points), then the workspace `skills/` directory next to
  the config (`.bos/skills/` in the default layout). A workspace skill with
  the same directory name overrides a builtin one.
- **The frontmatter parser is simple.** Flat `key: value` pairs plus `>` / `|`
  block scalars. No nested maps, no lists. Anything beyond `name` and
  `description` is ignored.
- **A running gateway caches the skill list ~5 minutes.** After adding or
  renaming a skill, run `boscli gateway restart` (or wait out the cache) —
  tell the user this when they test immediately and see nothing.

## Process

1. **Capture intent.** If the user says "turn this into a skill", mine the
   conversation first: the steps taken, commands run, corrections the user
   made, output formats they accepted. Then fill gaps with a few focused
   questions — what should the skill enable, what user phrasing should trigger
   it, what does a good result look like? Ask the most important questions
   first; don't front-load a survey.
2. **Draft it.** Create `<bos_dir>/skills/<skill-name>/SKILL.md` (usually
   `.bos/skills/`). No other files unless clearly earned (see Bundled files).
3. **Test it.** Run realistic prompts through the `TestSkill` tool — see
   *Testing the skill* below.
4. **Iterate.** Generalize from failures instead of patching the exact case:
   if the agent misused the skill, the instructions likely lack the *why*; if
   it ignored the skill, the description likely doesn't cover that phrasing.
   The skill must work for prompts you never tested, so prefer reframing over
   piling on rules.

## Writing the frontmatter

The description is the only triggering mechanism, and it costs context on
every turn, so make each word count:

- State what the skill does **and** when to use it — all "when to use"
  information belongs here, never in the body (the body is loaded too late to
  influence triggering).
- Agents under-trigger more than they over-trigger. Be a little pushy: name
  the concrete user phrasings, file types, and situations that should trigger
  it, including ones that don't name the skill.
- Keep it to one to three sentences. It is rendered for every allowed skill
  on every turn; a paragraph-long description taxes every conversation.

```yaml
---
name: quarterly-report
description: >
  Build the quarterly portfolio report. Use whenever the user asks for the
  quarterly report, mentions report season, or wants portfolio performance
  formatted for clients — even if they don't say "report".
---
```

## Writing the body

Write for another agent instance that has tools but none of this
conversation's context:

- **Be concise.** The model is already smart; include only what it doesn't
  know — house conventions, exact commands, schemas, pitfalls. Challenge each
  paragraph: does it justify its token cost?
- **Imperative voice, and explain why.** "Run X before Y because Y reads X's
  output" beats "ALWAYS run X first". Rigid all-caps rules are a yellow flag
  that the reasoning is missing — give the reason and the model will handle
  variations the rule-writer didn't foresee.
- **Match freedom to fragility.** Where many approaches work, give heuristics.
  Where exactly one sequence works (fragile APIs, destructive steps), give the
  exact commands and say what not to deviate from.
- **Use concrete examples** for formats: one good input/output pair beats a
  paragraph of format prose.
- **Keep it under ~150 lines.** If it grows past that, split detail into
  reference files and point to them (see below).
- **Ship nothing extraneous.** No README, no changelog, no setup notes. The
  skill directory is for the agent doing the task, not for documenting the
  skill.

## Bundled files

A skill directory may carry extra files: reference docs the agent reads when
needed, scripts it runs, templates it copies. Bundle a script when testing
shows the agent rewriting the same code every run; bundle a reference when
detail would bloat the body.

Two BOS-specific rules make bundles work:

- `LoadSkill` returns only the SKILL.md text — the agent never learns the
  skill's directory location from it. Reference bundled files by
  config-relative path (e.g. `.bos/skills/my-skill/scripts/build.py`) so the
  agent can find them, and say when to read or run each one.
- The agent needs file/shell tools to use bundles. If the target agent has a
  narrow tool allow-list, keep the skill self-contained instead.

## Wiring and access

Skills are deny-by-default per agent. Relevant config (`[agent.defaults...]`
or per-agent `[agents.<name>...]`):

```toml
[agent.defaults.plugin-bindings.SkillsPlugin]
# skill_dirs = ["__builtin__", "skills"]   # default; add extra dirs here
# allow = "*"                              # or an explicit list of skill names
# exclude = []
# preload = ["coding-discipline"]          # inject full body every turn, no LoadSkill needed
```

Use `preload` only for skills that must always be followed — it spends the
body's tokens on every turn.

## Testing the skill

Use the **TestSkill** tool. It runs a task on a throwaway agent that has only
the skill under test enabled — in-memory chat state, a fresh scan of the
skill directories (a just-written skill is visible immediately), no change to
the project config, and no interaction with the running gateway or real chat
history.

Call it with 2–3 realistic, differently phrased prompts that do **not** name
the skill — naming it would make the triggering check meaningless:

```
TestSkill(name="quarterly-report", task="put together the Q2 client numbers")
```

The result reports two things; judge them separately:

- **Triggered** — whether the test agent chose to load the skill from its
  description alone. If no, tune the description.
- **The response** — whether following the body produced the right result.
  If not, tune the body.

One blind spot: the test agent sees only this skill, so TestSkill cannot
catch a description that loses to a competing skill. Finish with one
end-to-end check through the real agent: wire the skill in (next section),
run `uv run boscli gateway restart` (a running gateway caches the skill list
~5 minutes), then try the same prompts with `uv run boscli ask "..."`.

When wiring in a restricted project, edit minimally and in the right scope.
The effective agent settings are the deep merge of `[agent.defaults]` and the
per-agent `[agents.<name>]` sections (per-agent values win key-by-key), so
find where the relevant list is actually pinned before touching anything:

- *SkillsPlugin not enabled*: append `"SkillsPlugin"` to the pinned
  `plugins.enabled` list. If neither section has a `plugins` block, that
  means *no plugins* (unlike `allow`, where unset means all) — create
  `enabled = ["SkillsPlugin"]` with just that one entry.
- *Pinned `allow` list*: append the skill's directory name and nothing else.
  If `allow` is `"*"` or unset, change nothing.
- Never substitute `"*"` for either fix — a narrowed list is a deliberate
  project choice, and the single appended entry is exactly what the skill
  needs for real use.

To share a skill beyond one workspace, ship it in a Python package: declare a
`bos.skills` entry point naming a package directory whose subdirectories are
skills. `boscli project init --archetype package` scaffolds exactly this
layout, wired and publishable.

## Improving an existing skill

Read the current SKILL.md first and keep the directory name (renaming breaks
configs that allow-list it). Diagnose before editing: a skill that never fires
needs a better description; one that fires but misleads needs a better body;
one that works but slowly may need a bundled script for the repeated part.
Re-test with the same prompts after each change.
