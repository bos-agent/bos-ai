# CHANGELOG

<!-- version list -->

## v1.12.0 (2026-07-26)

### Features

- **agent**: Hand off turn context when max iterations are reached
  ([#75](https://github.com/bos-agent/bos-ai/pull/75),
  [`a54fdc5`](https://github.com/bos-agent/bos-ai/commit/a54fdc52ad1ab1a9fc4e95b290bc83bf735d30c7))

- **gateway**: Close in-flight turns with a handoff on shutdown
  ([#79](https://github.com/bos-agent/bos-ai/pull/79),
  [`44697ae`](https://github.com/bos-agent/bos-ai/commit/44697ae4c2eeac0d641db8ca012a383f2cefaed2))


## v1.11.0 (2026-07-09)

### Bug Fixes

- **cli**: Stop boscli ask from wiping the agent's registry defaults
  ([#72](https://github.com/bos-agent/bos-ai/pull/72),
  [`eadbf0d`](https://github.com/bos-agent/bos-ai/commit/eadbf0db1451c71be4525340fd2e198fc53aa359))

### Features

- **agents**: Add built-in bos_config agent for safe project configuration (BEP 15)
  ([#70](https://github.com/bos-agent/bos-ai/pull/70),
  [`6a9da4e`](https://github.com/bos-agent/bos-ai/commit/6a9da4e8634782aab9f351613c76fe96df4dc747))

- **cli**: Add --no-steps option to boscli ask ([#71](https://github.com/bos-agent/bos-ai/pull/71),
  [`982412d`](https://github.com/bos-agent/bos-ai/commit/982412d0c9b6028b59257d2e695323217fa916af))


## v1.10.1 (2026-07-02)

### Bug Fixes

- **config**: Allow _parent to inherit from ep_agent factory agents
  ([#69](https://github.com/bos-agent/bos-ai/pull/69),
  [`113997e`](https://github.com/bos-agent/bos-ai/commit/113997e6ed535d9ffe4d0eaf37874fe69a2203fc))


## v1.10.0 (2026-06-30)

### Features

- **scaffold**: Ship llm-full.md reference in scaffolded projects
  ([#68](https://github.com/bos-agent/bos-ai/pull/68),
  [`84d1e81`](https://github.com/bos-agent/bos-ai/commit/84d1e81f9212d758c28f55d62d291ac654ebc45b))


## v1.9.0 (2026-06-30)

### Features

- **config**: Agent inheritance, BOS-as-extension, and .bos/.gitignore scaffolding
  ([#67](https://github.com/bos-agent/bos-ai/pull/67),
  [`c4cb0f6`](https://github.com/bos-agent/bos-ai/commit/c4cb0f6425786523c3a950dc3e8075c73981697e))


## v1.8.0 (2026-06-29)

### Documentation

- Flesh out docsite and add full AI-agent reference
  ([#66](https://github.com/bos-agent/bos-ai/pull/66),
  [`cc92d73`](https://github.com/bos-agent/bos-ai/commit/cc92d7342b82da48cbd087aabb981c372bb3f3f1))

### Features

- **cli**: Run boscli ask in-process; remove Docker runtime feature
  ([#65](https://github.com/bos-agent/bos-ai/pull/65),
  [`b0c53aa`](https://github.com/bos-agent/bos-ai/commit/b0c53aa0e05b903dddb4934bc82861fb516e5dbd))


## v1.7.0 (2026-06-28)

### Documentation

- **site**: MKDocs doc site published on release
  ([#61](https://github.com/bos-agent/bos-ai/pull/61),
  [`1951761`](https://github.com/bos-agent/bos-ai/commit/1951761b7d7bd94ceadedcedc89e8982f292cb53))

### Features

- **channels**: Accept all attachment MIME types across input routes
  ([#64](https://github.com/bos-agent/bos-ai/pull/64),
  [`86997ef`](https://github.com/bos-agent/bos-ai/commit/86997ef88aa694fdf2a7e326c723172cd1cd8776))

- **channels**: Accept inbound images on Telegram and Lark
  ([#60](https://github.com/bos-agent/bos-ai/pull/60),
  [`e332976`](https://github.com/bos-agent/bos-ai/commit/e332976f46e9a6f5c5696ada24bed4ef624b847b))

- **cli**: Add `boscli inspect`; config + extension naming cleanups
  ([#57](https://github.com/bos-agent/bos-ai/pull/57),
  [`d855516`](https://github.com/bos-agent/bos-ai/commit/d855516119b96df2d751a006ef4613647b7eddd7))

- **memory**: Make memory consolidation model configurable
  ([#58](https://github.com/bos-agent/bos-ai/pull/58),
  [`57821be`](https://github.com/bos-agent/bos-ai/commit/57821beb6e892d274f0f8499b7cfd2b434f6ef25))

- **tui**: Attach image files from an in-terminal file browser
  ([#62](https://github.com/bos-agent/bos-ai/pull/62),
  [`b0283aa`](https://github.com/bos-agent/bos-ai/commit/b0283aa9ac31f82885c63233fe122e44bcac6f3e))

### Refactoring

- **cli**: Consolidate scaffolding to workspace + package archetypes
  ([#59](https://github.com/bos-agent/bos-ai/pull/59),
  [`9baca1e`](https://github.com/bos-agent/bos-ai/commit/9baca1ee380c343fd445a4c4374f3630c8497fff))

- **config**: Only discover .bos/config.toml; unify search ignore filtering
  ([#63](https://github.com/bos-agent/bos-ai/pull/63),
  [`6ec6493`](https://github.com/bos-agent/bos-ai/commit/6ec6493b929f718426441fdad8ffbba713b1690d))


## v1.6.0 (2026-06-26)

### Features

- **agent**: Unify subagent + background LLM into AgentRunner (BEP 12)
  ([#56](https://github.com/bos-agent/bos-ai/pull/56),
  [`552b6f7`](https://github.com/bos-agent/bos-ai/commit/552b6f736e59431d6d6c3e1ffa7b10e4abcc8536))


## v1.5.0 (2026-06-24)

### Bug Fixes

- **cli**: Wait for singleton lock release on gateway restart
  ([#53](https://github.com/bos-agent/bos-ai/pull/53),
  [`b8ef93a`](https://github.com/bos-agent/bos-ai/commit/b8ef93af5919533e25360bfdf23578e7ab4f8ac0))

- **config**: Remove incorrect ._default suffix from ep_tool config keys
  ([#46](https://github.com/bos-agent/bos-ai/pull/46),
  [`652d058`](https://github.com/bos-agent/bos-ai/commit/652d058e0e5b004c2e17890431ec9de4740e23d1))

### Documentation

- **bep**: BEP 14 — Multi-Agent Project Collaboration
  ([#55](https://github.com/bos-agent/bos-ai/pull/55),
  [`8ef0d89`](https://github.com/bos-agent/bos-ai/commit/8ef0d8900e98be9e83f250aa32fbfde51a1526ef))

- **bep**: Platform-managed memory (BEP 10) + async tasks (BEP 11)
  ([#50](https://github.com/bos-agent/bos-ai/pull/50),
  [`2bef7ae`](https://github.com/bos-agent/bos-ai/commit/2bef7ae461161233088fa00fbf3ea20549a60a76))

### Features

- **cli**: Interactive boscli init wizard with key detection and live model listing
  ([#47](https://github.com/bos-agent/bos-ai/pull/47),
  [`68a79b5`](https://github.com/bos-agent/bos-ai/commit/68a79b50208e3ee1ceb74a441dfadc6e1d4566c8))

- **harness, memory**: BEP 10 (ready-now + off-turn) + BEP 11 v1
  ([#51](https://github.com/bos-agent/bos-ai/pull/51),
  [`7083ef7`](https://github.com/bos-agent/bos-ai/commit/7083ef7c76e18fa569ffcf7172a76d232e2ea2f7))

- **lark**: Add Lark/Feishu channel over lark-oapi WebSocket long connection
  ([#49](https://github.com/bos-agent/bos-ai/pull/49),
  [`9f597e5`](https://github.com/bos-agent/bos-ai/commit/9f597e5d4b056ae71ca987e602ff5545fae9847d))

- **telegram**: Rich status replies + gateway/mailbox hardening
  ([#48](https://github.com/bos-agent/bos-ai/pull/48),
  [`ba431de`](https://github.com/bos-agent/bos-ai/commit/ba431de1b2fc01c67a21b70026d46d45da2faeaf))

### Refactoring

- BEP 13 — Clean Architecture concentric dependency rings
  ([#52](https://github.com/bos-agent/bos-ai/pull/52),
  [`941657a`](https://github.com/bos-agent/bos-ai/commit/941657a16cc4499662c0018ae2be1ca60088b059))

- **cli**: Flatten project group and small fixes
  ([#45](https://github.com/bos-agent/bos-ai/pull/45),
  [`c6648e7`](https://github.com/bos-agent/bos-ai/commit/c6648e73fe1df7a5838cf45bcb545ed556f2210f))

- **providers**: Remove OAuth provider extensions and `boscli auth`
  ([#54](https://github.com/bos-agent/bos-ai/pull/54),
  [`90cbb0e`](https://github.com/bos-agent/bos-ai/commit/90cbb0e2fd6c30a234be13dc11a2920394b93d04))


## v1.4.0 (2026-06-12)

### Chores

- **deps**: Pin dependency versions and configure uv
  ([#31](https://github.com/bos-agent/bos-ai/pull/31),
  [`88c6d75`](https://github.com/bos-agent/bos-ai/commit/88c6d75123e62a8f45884c984c87c7a8127f04f4))

### Documentation

- Simplify README and switch license to MIT ([#29](https://github.com/bos-agent/bos-ai/pull/29),
  [`d6ba67a`](https://github.com/bos-agent/bos-ai/commit/d6ba67a2ade41656647d4fdc33e42d34cad0aded))

- **bep**: BEP 4 - micro-kernel and plugin architecture
  ([#28](https://github.com/bos-agent/bos-ai/pull/28),
  [`eeacd7e`](https://github.com/bos-agent/bos-ai/commit/eeacd7ec9758b067d998c2898bcbd5bafae41200))

- **bep**: BEP 4 - micro-kernel and plugin architecture
  ([#27](https://github.com/bos-agent/bos-ai/pull/27),
  [`f499dc5`](https://github.com/bos-agent/bos-ai/commit/f499dc57a82e5acf13cb93f4d21796ede7904808))

### Features

- **cli**: Add entry-point plugin discovery for 3rd-party CLI extensions
  ([#40](https://github.com/bos-agent/bos-ai/pull/40),
  [`fe8cc15`](https://github.com/bos-agent/bos-ai/commit/fe8cc1583d329c01821c9530d447580c708aa447))

- **cli**: BEP 9 — project scaffolding, generators, and doctor
  ([#43](https://github.com/bos-agent/bos-ai/pull/43),
  [`da80655`](https://github.com/bos-agent/bos-ai/commit/da80655c4ac63c2f23e27f3329d9355bc85fce5f))

- **config**: BEP 6 — Configuration Architecture Redesign
  ([#38](https://github.com/bos-agent/bos-ai/pull/38),
  [`98a76e3`](https://github.com/bos-agent/bos-ai/commit/98a76e39aff66246d6599897de8a30145901ebd6))

- **core**: BEP 4 — micro-kernel and plugin architecture
  ([#30](https://github.com/bos-agent/bos-ai/pull/30),
  [`ae0f882`](https://github.com/bos-agent/bos-ai/commit/ae0f88278785b6102f4b92a13285d59b7ed79d3f))

- **core**: BEP 5 — ChatStore unified chat persistence and context assembly
  ([#32](https://github.com/bos-agent/bos-ai/pull/32),
  [`9df16d4`](https://github.com/bos-agent/bos-ai/commit/9df16d418e71df8070e63ab8660ceaae1d75ff1b))

- **core**: Bos agent parity with top agents — Part 1
  ([#35](https://github.com/bos-agent/bos-ai/pull/35),
  [`e8350f4`](https://github.com/bos-agent/bos-ai/commit/e8350f4736238147c6e106a513808ea59c1ed192))

- **exts**: Add entry-point extension discovery for third-party packages
  ([#33](https://github.com/bos-agent/bos-ai/pull/33),
  [`5f9fdf0`](https://github.com/bos-agent/bos-ai/commit/5f9fdf08763158b66b834c893d59b0a37acca580))

- **gateway**: Implement channel runtime architecture
  ([#39](https://github.com/bos-agent/bos-ai/pull/39),
  [`ff0b767`](https://github.com/bos-agent/bos-ai/commit/ff0b76793488300104561363bc5de3b464f14e03))

- **plugins**: Plugins, tools, and gateway improvements
  ([#42](https://github.com/bos-agent/bos-ai/pull/42),
  [`f4625ef`](https://github.com/bos-agent/bos-ai/commit/f4625ef8ecd67f5c027d51fbf3bfd76cc9bd8e16))

- **skills**: Extension improvements — skills, EPs, and CLI init
  ([#44](https://github.com/bos-agent/bos-ai/pull/44),
  [`0d3175f`](https://github.com/bos-agent/bos-ai/commit/0d3175ffea43963f533824e070eda39f4e756a98))

- **tui**: Production-grade TUI and CLI revamp (BEP 8)
  ([#41](https://github.com/bos-agent/bos-ai/pull/41),
  [`1289165`](https://github.com/bos-agent/bos-ai/commit/128916553d7836d4733d645d85e7f75a5af7544c))

### Refactoring

- **core**: Separate agent kind from name, consolidate NamedActor identity
  ([#36](https://github.com/bos-agent/bos-ai/pull/36),
  [`0a5d802`](https://github.com/bos-agent/bos-ai/commit/0a5d802c55a71b63813cfec906005c4050d7d4c1))


## v1.3.0 (2026-05-20)

### Features

- **agent**: Abort-safe history persistence, hardened actor cleanup, and process-group termination
  ([`057bea5`](https://github.com/bos-agent/bos-ai/commit/057bea506e3003fda1615d3fb25b78da193e96be))

- **agent**: Task state emission, fixed task panel, dynamic iteration budget, ReActAgent rename
  ([#24](https://github.com/bos-agent/bos-ai/pull/24),
  [`03f8286`](https://github.com/bos-agent/bos-ai/commit/03f8286fd1eb9192cacd032747fcaa43834171cb))

- **defaults**: Tighten agent prompt and harden filesystem tools
  ([#26](https://github.com/bos-agent/bos-ai/pull/26),
  [`d01a1ef`](https://github.com/bos-agent/bos-ai/commit/d01a1ef6be7460438f9e88a5a8c2a59b0ec938b4))

### Refactoring

- **cli**: Rename to boscli, group gateway commands, harden workspace config
  ([#23](https://github.com/bos-agent/bos-ai/pull/23),
  [`eb1bcbb`](https://github.com/bos-agent/bos-ai/commit/eb1bcbb75fadcd6ea3e737d2fd65c81e311dd7ce))


## v1.2.0 (2026-05-13)

### Bug Fixes

- **named-actors**: Preserve metadata through actor hooks
  ([#17](https://github.com/bos-agent/bos-ai/pull/17),
  [`3d5565c`](https://github.com/bos-agent/bos-ai/commit/3d5565c263b619e6d882f70d9d80127b2d378c06))

### Chores

- Change log maintainance
  ([`a259cc7`](https://github.com/bos-agent/bos-ai/commit/a259cc77e7df711b47517e1aa5b20c8643eb3010))

### Features

- **cli**: Add -i/--interactive and --whom options to bos ask
  ([#20](https://github.com/bos-agent/bos-ai/pull/20),
  [`dff4a70`](https://github.com/bos-agent/bos-ai/commit/dff4a70cf660a02234d33f498efed17d43e5d78e))

- **tui**: Ctrl+enter interrupt message and slash-command autocomplete
  ([#18](https://github.com/bos-agent/bos-ai/pull/18),
  [`70c951d`](https://github.com/bos-agent/bos-ai/commit/70c951d077e7737b4c1d14487a4f8487e932d29c))

### Refactoring

- **cli**: Replace --workspace/--whom with global --config option
  ([#22](https://github.com/bos-agent/bos-ai/pull/22),
  [`4b609ef`](https://github.com/bos-agent/bos-ai/commit/4b609eff84316a0240d5002edbb0b01dc83eaa91))

- **defaults**: Tune default agent prompts ([#21](https://github.com/bos-agent/bos-ai/pull/21),
  [`2432eab`](https://github.com/bos-agent/bos-ai/commit/2432eab6e3a8899d15b32919da14a3f375abf635))

- **extensions**: Move in-memory extensions and add builtin skills support
  ([#19](https://github.com/bos-agent/bos-ai/pull/19),
  [`5ad33b1`](https://github.com/bos-agent/bos-ai/commit/5ad33b14a39397494ac46daf2df4e0a83ed23615))


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
