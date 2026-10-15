from typer.testing import CliRunner

from llm_memory.cli import app


def test_token_create_rejects_empty_owner_id() -> None:
    result = CliRunner().invoke(app, ["token", "create", "--owner-id", "   "])

    assert result.exit_code != 0
    assert "Owner identity cannot be empty" in result.output
