# CHANGELOG

<!-- version list -->

## v0.0.0 (2026-04-27)


## v0.2.0 (2026-04-27)

### Bug Fixes

- **config**: Anchor broadcast state under bos dir
  ([#18](https://github.com/bos-agent/bos-ai/pull/18),
  [`50879ab`](https://github.com/bos-agent/bos-ai/commit/50879ab3e458f607b5813c2e6fe711874be9f7fc))

### Build System

- **release**: Upgrade semantic-release v10 and tighten top-level docs
  ([#8](https://github.com/bos-agent/bos-ai/pull/8),
  [`d16da5c`](https://github.com/bos-agent/bos-ai/commit/d16da5cfef0a657a619d78940baa43f7279562e3))

### Chores

- Remove local OMX planning artifacts ([#18](https://github.com/bos-agent/bos-ai/pull/18),
  [`50879ab`](https://github.com/bos-agent/bos-ai/commit/50879ab3e458f607b5813c2e6fe711874be9f7fc))

### Documentation

- **agents**: Require semantic PR titles ([#14](https://github.com/bos-agent/bos-ai/pull/14),
  [`16fb4fb`](https://github.com/bos-agent/bos-ai/commit/16fb4fbb268053b31ee460bdfdf142c7c1e99aca))

### Features

- Enable image-first multimodal transport ([#12](https://github.com/bos-agent/bos-ai/pull/12),
  [`78d02bc`](https://github.com/bos-agent/bos-ai/commit/78d02bc5c7a9d4ea8a96de9107db61405fcdca00))

- Make turn events an explicit runtime output for frontend to consume
  ([#11](https://github.com/bos-agent/bos-ai/pull/11),
  [`5a4e2cb`](https://github.com/bos-agent/bos-ai/commit/5a4e2cb73b32f06b9e795fa54858a25aaecda57d))

- **channel**: Support resumable multi-client HTTP sessions
  ([#18](https://github.com/bos-agent/bos-ai/pull/18),
  [`50879ab`](https://github.com/bos-agent/bos-ai/commit/50879ab3e458f607b5813c2e6fe711874be9f7fc))

- **config**: Inject workspace context into channels
  ([#18](https://github.com/bos-agent/bos-ai/pull/18),
  [`50879ab`](https://github.com/bos-agent/bos-ai/commit/50879ab3e458f607b5813c2e6fe711874be9f7fc))

- **config**: Refine BOS config loading and add explicit external agent definitions
  ([#14](https://github.com/bos-agent/bos-ai/pull/14),
  [`16fb4fb`](https://github.com/bos-agent/bos-ai/commit/16fb4fbb268053b31ee460bdfdf142c7c1e99aca))

### Refactoring

- **agent**: Make system_prompt string-only ([#16](https://github.com/bos-agent/bos-ai/pull/16),
  [`6b3dbf8`](https://github.com/bos-agent/bos-ai/commit/6b3dbf898f3202107fac00a58ef6a6becb55b246))

- **channel**: Unify actor-owned session resets and interactive channel takeover
  ([#13](https://github.com/bos-agent/bos-ai/pull/13),
  [`221047b`](https://github.com/bos-agent/bos-ai/commit/221047b18ba2edb5dd6d6df5f9fafbc439f2ecf5))

- **config**: Resolve broadcast channel state options
  ([#18](https://github.com/bos-agent/bos-ai/pull/18),
  [`50879ab`](https://github.com/bos-agent/bos-ai/commit/50879ab3e458f607b5813c2e6fe711874be9f7fc))

- **core**: Align interceptor APIs with turn terminology
  ([#16](https://github.com/bos-agent/bos-ai/pull/16),
  [`6b3dbf8`](https://github.com/bos-agent/bos-ai/commit/6b3dbf898f3202107fac00a58ef6a6becb55b246))

- **core**: Rename conversations to chats ([#19](https://github.com/bos-agent/bos-ai/pull/19),
  [`e5e8048`](https://github.com/bos-agent/bos-ai/commit/e5e8048167143595f4b2e948da413e7eb52f469e))

- **harness**: Default agent mode. exam subagent orchstration.
  ([#9](https://github.com/bos-agent/bos-ai/pull/9),
  [`abd3c1c`](https://github.com/bos-agent/bos-ai/commit/abd3c1c06eba4922355b625672b7ef068c9b7b2a))


## v0.1.0 (2026-04-19)

### Features

- mailbox redesign to mailroute + mailbox bound with address ([#5](https://github.com/bos-agent/bos-ai/pull/5), [`fb7dc69`](https://github.com/bos-agent/bos-ai/commit/fb7dc695f2d2c77d5bde4c09be0ec1b78d8f5980))
- run agent in docker container ([`ba19b7a`](https://github.com/bos-agent/bos-ai/commit/ba19b7a2807e4610653456c174cf168fa7aa84c6))
- run agentactor as a standalone process. support channels of http and telegram ([#3](https://github.com/bos-agent/bos-ai/pull/3), [`bcecf00`](https://github.com/bos-agent/bos-ai/commit/bcecf00c17c22a4f3c3d55512fbd422978da6c95))

### Bug Fixes

- skill loader and other bugs fix. update on the config template ([#2](https://github.com/bos-agent/bos-ai/pull/2), [`493864c`](https://github.com/bos-agent/bos-ai/commit/493864c57a28247949cb8df1643860bdc9b86265))

### Refactoring

- make channel routing explicit and stabilize primary actor address ([#6](https://github.com/bos-agent/bos-ai/pull/6), [`90573da`](https://github.com/bos-agent/bos-ai/commit/90573daf492aa5f505be8fc260ee3b3188d17a23))
- **core**: code structure deep refactoring ([#4](https://github.com/bos-agent/bos-ai/pull/4), [`daea7d2`](https://github.com/bos-agent/bos-ai/commit/daea7d24c7ff0a2630c14d602b661299b23a48fd))

### Chores

- setup PSR and github flow ([#1](https://github.com/bos-agent/bos-ai/pull/1), [`1bcb8e2`](https://github.com/bos-agent/bos-ai/commit/1bcb8e2a2c68d63ad145b33aee4d6a45b9149d41))
