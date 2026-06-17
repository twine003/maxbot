# Authoring a Connector

A **Connector** is the bot's adapter to a single chat platform (Telegram, Discord, Slack, WhatsApp, web UI, CLI). The Telegram connector is the v0.1 reference implementation.

> **Status (v0.1):** only the Telegram connector is implemented. The `Connector` ABC defines the surface, but runners and workers currently call the Telegram connector directly. A second connector will land in v0.2 with a proper abstraction over streaming. See "Migration plan" at the bottom.

## 1. When you need a new connector

- Bringing the bot to a new chat platform.
- Wrapping an external transport (e.g. an HTTP webhook endpoint instead of polling).

You do NOT need a connector to:

- Add a new command — write a [plugin](AUTHORING_PLUGINS.md).
- Add a new AI engine — write a [runner](AUTHORING_RUNNERS.md).

## 2. The Connector ABC

`maxbot/connectors/base.py` defines:

```python
class Connector(ABC):
    name: str = "connector"
    capabilities: ConnectorCapabilities

    # I/O
    async def send_text(chat_id, text) -> int | None
    async def edit_text(chat_id, message_id, text) -> None
    async def send_document(chat_id, path, filename=None) -> None
    async def send_chat_action(chat_id, action) -> None
    async def download_file(file_id, dest_path) -> None

    # handler wiring
    def register_text_handler(handler) -> None
    def register_command(name, handler) -> None

    # lifecycle
    async def run_forever() -> None
    async def shutdown() -> None
```

`IncomingMessage` is the platform-agnostic event:

```python
@dataclass
class IncomingMessage:
    chat_id: int
    text: str
    user_id: int | None
    user_handle: str | None
    raw: Any   # platform-native event (Update, discord.Message, …)
```

## 3. Skeleton: a Discord connector

```python
# maxbot/connectors/discord/connector.py
import discord
from maxbot.connectors.base import Connector, ConnectorCapabilities, IncomingMessage


class DiscordConnector(Connector):
    name = "discord"
    capabilities = ConnectorCapabilities(
        supports_edit=True,
        supports_documents=True,
        supports_voice_input=False,
        rate_limit_edits_per_min=5,  # Discord is stricter
    )

    def __init__(self, config):
        self.config = config
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        self._text_handlers = []
        self._commands = {}

    # I/O
    async def send_text(self, chat_id, text):
        channel = self.client.get_channel(chat_id)
        msg = await channel.send(text)
        return msg.id

    async def edit_text(self, chat_id, message_id, text):
        channel = self.client.get_channel(chat_id)
        msg = await channel.fetch_message(message_id)
        await msg.edit(content=text)

    # … rest of the methods

    # handlers
    def register_text_handler(self, handler):
        self._text_handlers.append(handler)

    def register_command(self, name, handler):
        self._commands[name] = handler

    async def run_forever(self):
        @self.client.event
        async def on_message(msg):
            if msg.author.bot: return
            text = msg.content
            if text.startswith("/"):
                cmd, _, args = text[1:].partition(" ")
                if cmd in self._commands:
                    await self._commands[cmd](msg, args)
                    return
            for h in self._text_handlers:
                await h(IncomingMessage(
                    chat_id=msg.channel.id, text=text,
                    user_id=msg.author.id, user_handle=str(msg.author),
                    raw=msg,
                ))
        await self.client.start(self.config.token)

    async def shutdown(self):
        await self.client.close()
```

## 4. Wiring it into the bootstrap

For v0.1 the bootstrap (`maxbot/app.py`) hardcodes `TelegramConnector`. To add a second connector, modify `build_app` to read `[bot].connector` from `bot.toml`:

```toml
[bot]
connector = "discord"   # default "telegram"
token = "..."
```

```python
# in build_app
connector_name = config.extras.get("bot", {}).get("connector", "telegram")
if connector_name == "telegram":
    connector = TelegramConnector(config)
elif connector_name == "discord":
    from .connectors.discord.connector import DiscordConnector
    connector = DiscordConnector(config)
else:
    raise RuntimeError(f"Unknown connector: {connector_name}")
```

A future v0.2 will move this into a registry pattern (`ConnectorRegistry`) similar to `RunnerRegistry`.

## 5. What plugins / handlers depend on the connector having

| Surface | Required for |
|---|---|
| `send_text` + `edit_text` | Worker streaming (the live message that grows) |
| `send_document` | Attachment support (`[ADJUNTO:...]` sentinel) |
| `download_file` | Photo / voice / document handlers |
| `register_command` | All plugins that expose `/commands` |
| `register_text_handler` | Free-form text → worker spawn |
| `job_queue`-like scheduler | The `heartbeat` plugin |

If your platform doesn't have one of these (e.g. no `edit_text`), set the corresponding `ConnectorCapabilities` flag to `False` and write a fallback in `WorkerManager._send_result` that handles the missing capability gracefully.

## 6. Migration plan (v0.2)

In v0.2 the runner streaming code will route through a `StreamContext` that the connector provides, so a runner can update the live message without importing Telegram-specific helpers. Today's runners (`ClaudeRunner`, `CodexRunner`) import from `maxbot.connectors.telegram.ui.streaming` — that coupling will be removed once a second connector exists.

If you add a second connector now, expect to copy/adapt the streaming primitives. The ABC freezes the public surface; internals will evolve.

## 7. Checklist

- [ ] Class extends `maxbot.connectors.base.Connector`
- [ ] All abstract methods implemented (or no-op + capability flag set)
- [ ] `capabilities` reflects what the platform actually supports
- [ ] `name` is unique
- [ ] Lifecycle works: `run_forever()` blocks, `shutdown()` cleans up
- [ ] `register_command` and `register_text_handler` handle the platform's quirks (mentions, prefixes, threads…)
- [ ] Update `build_app` to instantiate your connector based on config
