from llm_memory.config import get_settings

if get_settings().storage_backend == "markdown":
    from llm_memory.markdown_mcp import build_server, run
else:
    from llm_memory.database_mcp_server import build_server, run

__all__ = ["build_server", "run"]
