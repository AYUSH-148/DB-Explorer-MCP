from typing import Any

from sqlalchemy import Engine
from sqlparse import parse

from errors import ToolInputError
from inspector import get_all_tables


def get_migration_context(engine: Engine) -> dict[str, Any]:
    """Return schema and dialect context for client-side migration generation."""
    return {
        "dialect": engine.dialect.name,
        "tables": get_all_tables(engine),
        "execution_note": "Migration SQL is generated and run by the user, never by this server.",
    }


def _validate_script(script: str, name: str) -> list[str]:
    if not isinstance(script, str) or not script.strip():
        raise ToolInputError(
            code="missing_argument",
            message=f"{name} SQL is required",
            hint="Both up_sql and down_sql must be non-empty.",
        )
    if "--" in script or "/*" in script or "*/" in script:
        raise ToolInputError(
            code="comments_not_allowed",
            message=f"{name} SQL comments are not allowed",
            hint="Strip the comments from the migration script and resend it.",
        )

    statements = [statement for statement in parse(script) if statement.tokens]
    if not statements:
        raise ToolInputError(
            code="unparsable_sql",
            message=f"{name} SQL could not be parsed",
            hint="Send complete, semicolon-separated DDL statements.",
        )

    statement_types = []
    for statement in statements:
        statement_type = statement.get_type() or "UNKNOWN"
        if statement_type == "SELECT":
            raise ToolInputError(
                code="select_in_migration",
                message=f"{name} SQL must not contain SELECT statements",
                hint="Use execute_query to read data; migrations are DDL only.",
            )
        statement_types.append(statement_type)
    return statement_types


def validate_migration(
    engine: Engine,
    up_sql: str,
    down_sql: str,
) -> dict[str, Any]:
    """Validate migration scripts without executing either script."""
    return {
        "valid": True,
        "dialect": engine.dialect.name,
        "up": {"sql": up_sql.strip(), "statement_types": _validate_script(up_sql, "UP")},
        "down": {
            "sql": down_sql.strip(),
            "statement_types": _validate_script(down_sql, "DOWN"),
        },
        "execution_note": "Not executed. Review and run manually.",
    }
