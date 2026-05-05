# CHANGELOG

<!-- version list -->

## v1.1.0 (2026-05-05)

### Bug Fixes

- **config**: Accept any agent@* address in channel topology validation
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

- **consolidator**: Ensure LLMs respond to summarization and update prompt
  ([#16](https://github.com/bos-agent/bos-ai/pull/16),
  [`96dfc00`](https://github.com/bos-agent/bos-ai/commit/96dfc0096f5e084bf328b36974d91091168260a7))

- **squad**: Filter agent_spec to valid SquadAgent params via _build_params
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

- **squad**: Wire start_squad() into bos start and bos tui startup paths
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

- **squad**: Write channel info to agent.state so TUI can discover endpoints
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

### Chores

- **squad**: Fix lint issues — unused imports, import ordering, type annotation
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

### Documentation

- Amend BEP ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

- **config**: Add squad multi-actor example to template.toml
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

- **squad**: Add implementation plan for multi-actor runtime
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

- **squad**: Design spec for multi-actor runtime
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

- **squad**: Move @mention parsing from frontend into ActorRegistry
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

### Features

- **core**: Add harness-level consolidator service
  ([#14](https://github.com/bos-agent/bos-ai/pull/14),
  [`3e85b37`](https://github.com/bos-agent/bos-ai/commit/3e85b37be9c8d5f544e130e6151206d0e8898041))

- **named-actors**: Implement runtime actor routing
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

- **squad**: Add ActorRegistry with @mention parsing and routing
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

- **squad**: Add SquadActor with target_actor attribution
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

- **squad**: Add SquadAgent with tool-noise filtering
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

- **squad**: Add start_squad runner with multi-actor wiring
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

- **squad**: Wire ActorRegistry into HttpChannel for @mention routing
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

### Refactoring

- **core**: Reject new messages during active turn instead of buffering
  ([#15](https://github.com/bos-agent/bos-ai/pull/15),
  [`b7eacb0`](https://github.com/bos-agent/bos-ai/commit/b7eacb00cd213a22508f5590c3f5fdd7bee61e79))

- **squad**: Use _apply for agent construction in _build_squad_agent
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))

### Testing

- **squad**: Add backwards-compat edge case tests for config parsing
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))


## v1.0.0 (2026-05-03)

### Bug Fixes

- **core**: Make _call_tool delegate to _invoke_tool as shared dispatch path
  ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))

- **test**: Include UpdateMemory in star-capabilities assertion
  ([#9](https://github.com/bos-agent/bos-ai/pull/9),
  [`81ed046`](https://github.com/bos-agent/bos-ai/commit/81ed046bfc0074dcefe5566f9f18c4f7fbe62af2))

- **tui**: Re-discover channel port from agent.state on reconnect
  ([#8](https://github.com/bos-agent/bos-ai/pull/8),
  [`5bb4eb1`](https://github.com/bos-agent/bos-ai/commit/5bb4eb1cffd6581bfb6531c5a70abca2deefeb43))

- **tui**: Rebind escape to interrupt_turn, ctrl+c to quit, drop ctrl+q
  ([#8](https://github.com/bos-agent/bos-ai/pull/8),
  [`5bb4eb1`](https://github.com/bos-agent/bos-ai/commit/5bb4eb1cffd6581bfb6531c5a70abca2deefeb43))

- **tui+core**: Reliable httpclient port discover. default agent spec override.
  ([#8](https://github.com/bos-agent/bos-ai/pull/8),
  [`5bb4eb1`](https://github.com/bos-agent/bos-ai/commit/5bb4eb1cffd6581bfb6531c5a70abca2deefeb43))

### Documentation

- Add CLAUDE.md and clean up repo guidance ([#10](https://github.com/bos-agent/bos-ai/pull/10),
  [`0f716e3`](https://github.com/bos-agent/bos-ai/commit/0f716e37d696d945d1a9cb859b68b6d3b9ca7e66))

### Features

- **memory**: Add InMemMemoryExtension replacing InMemMemoryStore
  ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))

- **memory**: Add MarkdownMemoryExtension replacing MarkdownMemoryStore
  ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))

- **memory**: Add MemoryExtension protocol and ep_memory extension point
  ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))

- **memory**: Replace UpdateMemory with Remember/Recall/Forget tools, maxims integration
  ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))

- **memory**: Two-temperature MemoryExtension with Remember/Recall/Forget tools
  ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))

- **memory**: Update core exports for MemoryExtension types
  ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))

- **memory**: Wire MemoryExtension through AgentHarness
  ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))

- **tui**: Auto-focus input prompt on any keypress
  ([#7](https://github.com/bos-agent/bos-ai/pull/7),
  [`e9eefd9`](https://github.com/bos-agent/bos-ai/commit/e9eefd9f300ab10a81a2f9b6a096480b1ce890e6))

### Refactoring

- **core**: Disambiguate agent_name into role vs name
  ([#9](https://github.com/bos-agent/bos-ai/pull/9),
  [`81ed046`](https://github.com/bos-agent/bos-ai/commit/81ed046bfc0074dcefe5566f9f18c4f7fbe62af2))

- **core**: Fetch maxim content from store, add memory_usage param
  ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))

- **core**: Maxims capability override ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))

- **core**: Modularize default extensions and configuration
  ([#12](https://github.com/bos-agent/bos-ai/pull/12),
  [`29f98e6`](https://github.com/bos-agent/bos-ai/commit/29f98e6294c0b5201d72daa56de3a0708ca761ac))

- **core**: Replace list_maxims with dict-based maxims
  ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))

- **harness**: Remove _create_local_tools and ToolRegistry import
  ([#9](https://github.com/bos-agent/bos-ai/pull/9),
  [`81ed046`](https://github.com/bos-agent/bos-ai/commit/81ed046bfc0074dcefe5566f9f18c4f7fbe62af2))

- **harness**: Remove SendMail tool from harness-scoped local tools
  ([#9](https://github.com/bos-agent/bos-ai/pull/9),
  [`81ed046`](https://github.com/bos-agent/bos-ai/commit/81ed046bfc0074dcefe5566f9f18c4f7fbe62af2))

- **memory**: Make the markdown memory extension as default
  ([#12](https://github.com/bos-agent/bos-ai/pull/12),
  [`29f98e6`](https://github.com/bos-agent/bos-ai/commit/29f98e6294c0b5201d72daa56de3a0708ca761ac))

- **memory**: Migrate tests and sources to MemoryExtension and maxims naming
  ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))

- **memory**: Remove deprecated MemoryStore and ep_memory_store
  ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))

- **message_store**: Make the jsonl message store as default
  ([#12](https://github.com/bos-agent/bos-ai/pull/12),
  [`29f98e6`](https://github.com/bos-agent/bos-ai/commit/29f98e6294c0b5201d72daa56de3a0708ca761ac))

### Testing

- **memory**: Add MemoryExtension protocol and tool tests
  ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))


## v0.2.1 (2026-04-30)

### Bug Fixes

- Trigger version bump to resolve pypi conflict
  ([`4571391`](https://github.com/bos-agent/bos-ai/commit/45713918becee2758f3630bea2d3db936053f0bf))


## v0.2.0 (2026-04-30)

### Features

- **config**: Support explicit agent capability allowlists
  ([#4](https://github.com/bos-agent/bos-ai/pull/4),
  [`ee4de30`](https://github.com/bos-agent/bos-ai/commit/ee4de308f73e76b5ee192f976b33da001652bb21))

- **core**: Decouple agent dependencies and enhance CLI
  ([#6](https://github.com/bos-agent/bos-ai/pull/6),
  [`ea1674b`](https://github.com/bos-agent/bos-ai/commit/ea1674be449110b87e4d9459056ccb199af9d013))

- **core**: Support explicit subagent_defaults ([#6](https://github.com/bos-agent/bos-ai/pull/6),
  [`ea1674b`](https://github.com/bos-agent/bos-ai/commit/ea1674be449110b87e4d9459056ccb199af9d013))

- **runtime**: Add durable peer actor tasks ([#2](https://github.com/bos-agent/bos-ai/pull/2),
  [`d3c028f`](https://github.com/bos-agent/bos-ai/commit/d3c028faa318bf97d299620e839a0001d050344f))

### Testing

- **core**: Update test suite for subagent_defaults
  ([#6](https://github.com/bos-agent/bos-ai/pull/6),
  [`ea1674b`](https://github.com/bos-agent/bos-ai/commit/ea1674be449110b87e4d9459056ccb199af9d013))
