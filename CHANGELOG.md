# CHANGELOG

<!-- version list -->

## v1.1.0 (2026-05-05)

### Features

- **named actor**: Implement multi-actor runtime, ActorRegistry, and HttpChannel @mention routing ([#13](https://github.com/bos-agent/bos-ai/pull/13), [`420dcf0`](https://github.com/bos-agent/bos-ai/commit/420dcf0c1bb34161431ac8276051f7f8b517d030))
- **core**: Add harness-level consolidator service ([#14](https://github.com/bos-agent/bos-ai/pull/14), [`3e85b37`](https://github.com/bos-agent/bos-ai/commit/3e85b37be9c8d5f544e130e6151206d0e8898041))

### Bug Fixes

- **core**: Reject new messages during active turn instead of buffering ([#15](https://github.com/bos-agent/bos-ai/pull/15), [`b7eacb0`](https://github.com/bos-agent/bos-ai/commit/b7eacb00cd213a22508f5590c3f5fdd7bee61e79))
- **consolidator**: Ensure LLMs respond to summarization and update prompt ([#16](https://github.com/bos-agent/bos-ai/pull/16), [`96dfc00`](https://github.com/bos-agent/bos-ai/commit/96dfc0096f5e084bf328b36974d91091168260a7))


## v1.0.0 (2026-05-03)

### Features

- **memory**: Implement MemoryExtension protocol with Remember/Recall/Forget tools ([#11](https://github.com/bos-agent/bos-ai/pull/11), [`8f62c46`](https://github.com/bos-agent/bos-ai/commit/8f62c46d022c72d03578fc0fe48b9e9b72049368))
- **core**: Modularize default extensions, set Markdown memory and JSONL store as defaults ([#12](https://github.com/bos-agent/bos-ai/pull/12), [`29f98e6`](https://github.com/bos-agent/bos-ai/commit/29f98e6294c0b5201d72daa56de3a0708ca761ac))
- **tui**: Auto-focus input prompt on any keypress ([#7](https://github.com/bos-agent/bos-ai/pull/7), [`e9eefd9`](https://github.com/bos-agent/bos-ai/commit/e9eefd9f300ab10a81a2f9b6a096480b1ce890e6))

### Bug Fixes

- **tui**: Reliable HTTP client port discovery and TUI reconnects ([#8](https://github.com/bos-agent/bos-ai/pull/8), [`5bb4eb1`](https://github.com/bos-agent/bos-ai/commit/5bb4eb1cffd6581bfb6531c5a70abca2deefeb43))
- **core**: Disambiguate agent_name into role vs name, cleanup local tools ([#9](https://github.com/bos-agent/bos-ai/pull/9), [`81ed046`](https://github.com/bos-agent/bos-ai/commit/81ed046bfc0074dcefe5566f9f18c4f7fbe62af2))

### Documentation

- Add CLAUDE.md and clean up repo guidance ([#10](https://github.com/bos-agent/bos-ai/pull/10), [`0f716e3`](https://github.com/bos-agent/bos-ai/commit/0f716e37d696d945d1a9cb859b68b6d3b9ca7e66))


## v0.2.1 (2026-04-30)

### Bug Fixes

- Trigger version bump to resolve pypi conflict ([`4571391`](https://github.com/bos-agent/bos-ai/commit/45713918becee2758f3630bea2d3db936053f0bf))


## v0.2.0 (2026-04-30)

### Features

- **core**: Decouple agent dependencies and enhance CLI ([#6](https://github.com/bos-agent/bos-ai/pull/6), [`ea1674b`](https://github.com/bos-agent/bos-ai/commit/ea1674be449110b87e4d9459056ccb199af9d013))
- **config**: Support explicit agent capability allowlists ([#4](https://github.com/bos-agent/bos-ai/pull/4), [`ee4de30`](https://github.com/bos-agent/bos-ai/commit/ee4de308f73e76b5ee192f976b33da001652bb21))
- **runtime**: Add durable peer actor tasks ([#2](https://github.com/bos-agent/bos-ai/pull/2), [`d3c028f`](https://github.com/bos-agent/bos-ai/commit/d3c028faa318bf97d299620e839a0001d050344f))
