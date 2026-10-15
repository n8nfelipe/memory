"""Select the configured persistence backend without opening a database in Markdown mode."""

from llm_memory.config import get_settings

if get_settings().storage_backend == "markdown":
    from llm_memory.markdown_api import app
else:
    from llm_memory.database_api import app

__all__ = ["app"]
