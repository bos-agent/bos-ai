# Tutorial 5 — Connect a channel

A **channel** bridges an external client to one of your gateway's actor
mailboxes. Once wired up, messages arrive from the external service, get routed
to your agent, and replies are sent back — all without touching the TUI. This
tutorial walks through adding a Telegram bot.

This tutorial assumes you have the `my-agent/` project from
[Tutorial 2](create-a-project.md) with the gateway running.

---

## How channels work

```
Telegram servers ──► TelegramChannel.run(mailbox)
                           │
                    posts Envelope into
                           │
                     actor's mailbox ──► agent turn ──► reply
                           │
                    channel pushes reply back to Telegram
```

The gateway starts each configured channel as a long-lived task alongside the
actors. The channel's `run(mailbox)` loop listens for incoming messages,
delivers them to the actor, waits for the reply, and pushes it back to the
client. Channels do not share history with each other — each conversation thread
is isolated by its Telegram chat ID.

---

## Telegram walkthrough

### Step 1 — Create a bot with @BotFather

1. Open Telegram and start a chat with
   [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts (name → username ending in `bot`).
3. Copy the **token** BotFather gives you — it looks like `7123456789:AAF...`.

### Step 2 — Add the token to `.env`

Open `.bos/.env` and add:

```bash
TELEGRAM_BOT_TOKEN=7123456789:AAF...
```

Never put the token directly in `config.toml` — that file is typically checked
into version control.

### Step 3 — Declare the channel in `config.toml`

Add a `[[runtime.channels]]` entry:

```toml
[[runtime.channels]]
type = "TelegramChannel"
channel_id = "telegram+main"
display_name = "Telegram"
target_actor = "main"
settings = { token_env = "TELEGRAM_BOT_TOKEN" }
```

| Key | Meaning |
|-----|---------|
| `type` | The registered `ep_channel` implementation name. |
| `channel_id` | A unique identifier for this channel. Combine type and actor for clarity (`telegram+main`). |
| `display_name` | Human-readable name shown in logs and the TUI. |
| `target_actor` | Which actor receives messages from this channel. Must exist in `[runtime.actors]`. |
| `settings.token_env` | The name of the env var that holds the bot token. BOS reads the token from the environment at startup. |

!!! warning "Each channel must have a unique `channel_id`"
    If you add a second Telegram bot (for a different actor, for example), give
    it a different `channel_id` such as `telegram+researcher`.

### Step 4 — Restart the gateway

```bash
boscli gateway stop
boscli gateway start
```

Check the status:

```bash
boscli gateway status
```

If the channel started successfully you will see `TelegramChannel` listed.

### Step 5 — Talk to your agent

Open Telegram, find your bot by its username, and send it a message. Your
agent will reply within a few seconds.

---

## Lark / Feishu

The `LarkChannel` connects to a Lark (Feishu) workspace via a WebSocket long
connection — no public URL or webhook configuration needed.

First, install the optional dependency:

```bash
pip install 'bos-ai[lark]'
# or: uv add 'bos-ai[lark]'
```

Then add a channel entry:

```toml
[[runtime.channels]]
type = "LarkChannel"
channel_id = "lark:main"
display_name = "Lark"
target_actor = "main"
settings = { app_id_env = "LARK_APP_ID", app_secret_env = "LARK_APP_SECRET" }
```

Put `LARK_APP_ID` and `LARK_APP_SECRET` in `.bos/.env`. In the Lark developer
console enable the `im.message.receive_v1` event permission and switch the
receive mode to **"Use long connection to receive events"**.

!!! note "Why `bos-ai[lark]`?"
    The Lark SDK (`lark-oapi`) is an optional dependency to keep the default
    install small. If `LarkChannel` is configured but the package is not
    installed, the gateway will fail to start with a clear error.

---

## Multiple channels

You can attach as many channels as you need — each `[[runtime.channels]]`
block is one channel. They can target the same actor or different actors:

```toml
[[runtime.channels]]
type = "TelegramChannel"
channel_id = "telegram+main"
display_name = "Telegram → Main"
target_actor = "main"
settings = { token_env = "TELEGRAM_BOT_TOKEN" }

[[runtime.channels]]
type = "TelegramChannel"
channel_id = "telegram+researcher"
display_name = "Telegram → Researcher"
target_actor = "researcher"
settings = { token_env = "RESEARCHER_BOT_TOKEN" }
```

---

## What you learned

- Channels bridge external clients to an actor's mailbox; each channel runs as
  a long-lived task in the gateway process.
- The Telegram walkthrough: create a bot with @BotFather, store the token in
  `.env` as `TELEGRAM_BOT_TOKEN`, and declare a `[[runtime.channels]]` block
  with `type = "TelegramChannel"` and `settings = { token_env = "..." }`.
- `LarkChannel` requires `bos-ai[lark]` to be installed.
- You can attach multiple channels to the same or different actors; each needs
  a unique `channel_id`.

**Next:** [Package & share extensions](package-extension.md) — turn your
custom tools into an installable Python package that registers itself in any
BOS project.
