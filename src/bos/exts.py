"""Single import to load all built-in extensions and plugins.

Use ``extensions = ["bos.exts"]`` in config.toml to enable everything.
"""

# Core defaults (consolidator, litellm provider, etc.)
import bos.core.defaults  # noqa: F401

# Actor commands
import bos.extensions.actor_commands.system_cmd  # noqa: F401

# Channels
import bos.extensions.channels.http  # noqa: F401
import bos.extensions.channels.telegram  # noqa: F401

# Mailboxes
import bos.extensions.mailboxes.in_memory  # noqa: F401

# Memory stores
import bos.extensions.memory_stores.in_memory  # noqa: F401

# Message stores
import bos.extensions.message_stores.in_memory  # noqa: F401

# Providers
import bos.extensions.providers.antigravity_provider  # noqa: F401
import bos.extensions.providers.codex_provider  # noqa: F401
import bos.extensions.providers.gemini_cli_provider  # noqa: F401

# Tools
import bos.extensions.tools.filesystem  # noqa: F401
import bos.extensions.tools.knowledge  # noqa: F401
import bos.extensions.tools.orchestration  # noqa: F401
import bos.extensions.tools.system  # noqa: F401

# Plugin defaults register via their own ExtensionPoints on import
import bos.plugins.memory  # noqa: F401
import bos.plugins.memory.markdown_backend  # noqa: F401  registers MarkdownMemoryBackend as _default
import bos.plugins.skills  # noqa: F401
import bos.plugins.skills.fs_skill_loader  # noqa: F401  registers FileSystemSkillsLoader as _default
import bos.plugins.subagent  # noqa: F401

# Plugins (BEP 4)
import bos.plugins.task  # noqa: F401
