from llm_memory.config import get_settings

if get_settings().storage_backend == "markdown":
    from llm_memory.markdown_cli import app
else:
    from llm_memory.database_cli import app

__all__ = ["app"]
