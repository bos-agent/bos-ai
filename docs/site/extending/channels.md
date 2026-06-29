# Writing Channels

A **channel** bridges an external client — a messaging bot, a webhook endpoint, a custom terminal — to an actor's mailbox inside the gateway.  Channels are long-lived: each one runs for the lifetime of the gateway process, continuously reading from the outside world and delivering `Envelope` messages to the bound actor.

---

## The `Channel` protocol

```python
class Channel(Protocol):
    @property
    def channel_id(self) -> str: ...        # unique id, e.g. "telegram+main"

    @property
    def display_name(self) -> str | None: ...

    @property
    def target_actor(self) -> str: ...      # must exist in [runtime.actors]

    @property
    def identity_key(self) -> str | None: ... # used for identity/routing

    async def run(self, mailbox: MailBox) -> None: ...
```

`run(mailbox)` is the channel's entire life.  It must:

1. Wait for an incoming message from the external client.
2. Post an `Envelope` into `mailbox` (the actor's mailbox).
3. Await the actor's reply.
4. Push the reply back to the client.
5. Go back to step 1.

The loop runs until the gateway shuts down or the channel itself raises.

---

## `BaseChannel[SettingsT]` helper

Most channels use `BaseChannel` to avoid boilerplate.  It stores `channel_id`, `target_actor`, `settings`, `display_name`, and `runtime`, and implements the property stubs.  Subclass it and implement `run`:

```python
from dataclasses import dataclass
from bos.core.contract import BaseChannel, ep_channel
from bos.core.actor import MailBox


@dataclass
class PrintBotSettings:
    prefix: str = "Bot"


@ep_channel(name="PrintBotChannel")
class PrintBotChannel(BaseChannel[PrintBotSettings]):
    SettingsType = PrintBotSettings          # triggers typed settings parsing

    async def run(self, mailbox: MailBox) -> None:
        import asyncio
        while True:
            user_input = await asyncio.to_thread(input, f"{self._settings.prefix}> ")
            if not user_input.strip():
                continue

            from bos.core.actor import Envelope, MessageType
            env = Envelope(
                sender=f"channel@{self.channel_id}",
                recipient=f"agent@{self.target_actor}",
                payload={"role": "user", "content": user_input},
                message_type=MessageType.CHAT,
            )
            await mailbox.put(env)
            reply = await mailbox.get_reply(env.id)
            print(f"\n{reply.payload.get('content', '')}\n")
```

### `SettingsType`

Set the class variable `SettingsType` to a dataclass or Pydantic model.  The gateway parses the `[[runtime.channels]].settings` TOML table through it and hands a typed instance to `__init__` as `settings`.  If `SettingsType` is `None` (the default), `settings` is a raw `dict`.

---

## Settings flow

In config, each channel entry is an element of `[[runtime.channels]]`:

```toml
[[runtime.channels]]
type         = "PrintBotChannel"         # registered ep_channel name
channel_id   = "printbot+main"           # unique id
display_name = "Print Bot"
target_actor = "main"

[runtime.channels.settings]              # becomes PrintBotSettings(prefix="Agent")
prefix = "Agent"
```

!!! tip "Keep secrets in environment variables"
    Reference secrets by env-var name (`token_env = "TELEGRAM_BOT_TOKEN"`) rather than inlining them.  The channel reads `os.environ[settings.token_env]` at startup.  See `TelegramChannel` for a real example.

---

## How the gateway instantiates a channel

1. `workspace.resolve_gateway_channels` reads each `[[runtime.channels]]` block.
2. It looks up the `ep_channel` registered under `type`.
3. It constructs the channel, passing `channel_id`, `target_actor`, `display_name`, and parsed `settings`.
4. It binds the channel to the named actor's mailbox.
5. It calls `channel.run(mailbox)` as a long-running async task.

Each channel is bound to exactly one actor (`target_actor`).  The actor's mailbox address is `agent@<actor_name>`.  The channel's own address is `channel@<channel_id>`.

---

## Validation rules

- `channel_id` must be unique across all configured channels.
- `target_actor` must exist in `[runtime.actors]`.
- `type` must be a registered `ep_channel` implementation.
- `HttpChannel` is reserved as gateway infrastructure (the control-plane API) and may **not** be used as a user channel.

---

## Built-in channels

| Name | Requires | Notes |
|------|---------|-------|
| `TelegramChannel` | `pip install bos-ai` | `settings.token_env` names the env var holding the bot token |
| `LarkChannel` | `pip install 'bos-ai[lark]'` | Lark/Feishu; settings include `app_id_env`, `app_secret_env` |

---

## See also

- [Concepts: runtime](../concepts/runtime.md) — actors, mailboxes, gateway lifecycle
- [Configuration](../configuration/index.md) — `[[runtime.channels]]` and `[runtime.actors]` reference
- [Packaging & entry points](entry-points.md) — ship a channel in an installable package
