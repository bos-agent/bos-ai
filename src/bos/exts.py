"""Single import to load all built-in extensions and plugins.

Use ``extensions = ["bos.exts"]`` in config.toml to enable everything.

Third-party packages can register additional extensions by declaring the
``bos.exts`` entry-point group in their ``pyproject.toml``.  The entry point
target should be a module whose import triggers registration via decorators
such as ``@ep_tool``::

    # Third-party pyproject.toml
    [project.entry-points."bos.exts"]
    weather = "bos_weather_tools.bos_exts"

    # bos_weather_tools/bos_exts.py
    from bos.core import ep_tool, AgentRegistry

    @ep_tool(name="GetWeather", description="...", parameters={...})
    async def tool_get_weather(city: str) -> str:
        ...

    AgentRegistry.register(
        name="weather_agent",
        description="Weather agent",
        model="gemini-2.5-flash",
        tools=["GetWeather"],
    )
"""

# Core defaults (consolidator, litellm provider, etc.)
import bos.core.defaults  # noqa: F401

# Actor commands
import bos.extensions.actor_commands.system_cmd  # noqa: F401

# Channels
import bos.extensions.channels.telegram  # noqa: F401

# Chat stores
import bos.extensions.chat_stores.in_memory  # noqa: F401

# Mailboxes
import bos.extensions.mailboxes.in_memory  # noqa: F401

# Memory stores
import bos.extensions.memory_stores.in_memory  # noqa: F401

# Providers
import bos.extensions.providers.antigravity_provider  # noqa: F401
import bos.extensions.providers.codex_provider  # noqa: F401
import bos.extensions.providers.gemini_cli_provider  # noqa: F401

# Tools
import bos.extensions.tools.filesystem  # noqa: F401
import bos.extensions.tools.knowledge  # noqa: F401
import bos.extensions.tools.system  # noqa: F401

# Plugin defaults register via their own ExtensionPoints on import
import bos.plugins.memory  # noqa: F401
import bos.plugins.plan  # noqa: F401
import bos.plugins.skills  # noqa: F401
import bos.plugins.subagent  # noqa: F401
import bos.plugins.task  # noqa: F401


# Entry point extensions
def _discover_entry_point_extensions():
    from importlib.metadata import entry_points
    from logging import getLogger

    logger = getLogger(__name__)

    eps = entry_points(group="bos.exts")
    for ep in eps:
        try:
            ep.load()
        except Exception:
            logger.warning("Failed to load entry point extension %s", ep.name, exc_info=True)


_discover_entry_point_extensions()
del _discover_entry_point_extensions
