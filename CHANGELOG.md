# CHANGELOG

<!-- version list -->

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
