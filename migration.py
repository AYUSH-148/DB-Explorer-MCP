from typing import Any

from sqlalchemy import Engine
from sqlparse import parse
from sqlparse.tokens import Keyword, Punctuation

from errors import ToolInputError
from inspector import DEFAULT_TABLE_LIMIT, get_schema_page
from safety import comment_markers, has_comment


def get_migration_context(
    engine: Engine,
    name_pattern: str | None = None,
    limit: int | None = DEFAULT_TABLE_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Return dialect context and one page of full table detail for migration drafting."""
    return {
        "dialect": engine.dialect.name,
        **get_schema_page(
            engine,
            name_pattern=name_pattern,
            limit=limit,
            offset=offset,
            detail=True,
            include_row_counts=True,
        ),
        "execution_note": "Migration SQL is generated and run by the user, never by this server.",
    }


def _validate_script(script: str, name: str, dialect: str) -> list[str]:
    if not isinstance(script, str) or not script.strip():
        raise ToolInputError(
            code="missing_argument",
            message=f"{name} SQL is required",
            hint="Both up_sql and down_sql must be non-empty.",
        )
    # A stray ";" parses as its own statement of nothing but punctuation.
    statements = [
        statement
        for statement in parse(script)
        if any(
            token.ttype not in Punctuation and not token.is_whitespace
            for token in statement.flatten()
        )
    ]
    if not statements:
        raise ToolInputError(
            code="unparsable_sql",
            message=f"{name} SQL could not be parsed",
            hint="Send complete, semicolon-separated SQL statements.",
        )

    markers = comment_markers(dialect)
    if any(has_comment(statement, markers) for statement in statements):
        raise ToolInputError(
            code="comments_not_allowed",
            message=f"{name} SQL comments are not allowed",
            hint="Strip the comments from the migration script and resend it.",
        )

    statement_types = []
    for statement in statements:
        # Every real statement opens with a keyword. This refuses prose such as
        # "hello world" and a parenthesised "(SELECT ...)", which sqlparse types
        # UNKNOWN and would otherwise slip past the SELECT check below. UNKNOWN
        # itself is kept: COMMENT ON, RENAME TABLE, GRANT and DO all type that way.
        first = statement.token_first(skip_cm=True)
        if first is None or first.ttype not in Keyword:
            raise ToolInputError(
                code="unparsable_sql",
                message=f"{name} SQL statement does not start with a SQL keyword",
                hint="Send complete, semicolon-separated SQL statements, unparenthesised.",
            )
        statement_type = statement.get_type() or "UNKNOWN"
        if statement_type == "SELECT":
            raise ToolInputError(
                code="select_in_migration",
                message=f"{name} SQL must not contain SELECT statements",
                hint="Use execute_query to read data; migrations change schema or data.",
            )
        statement_types.append(statement_type)
    return statement_types


def validate_migration(
    engine: Engine,
    up_sql: str,
    down_sql: str,
) -> dict[str, Any]:
    """Validate migration scripts without executing either script."""
    dialect = engine.dialect.name
    up_types = _validate_script(up_sql, "UP", dialect)
    down_types = _validate_script(down_sql, "DOWN", dialect)
    return {
        "dialect": dialect,
        "up": {"sql": up_sql.strip(), "statement_types": up_types},
        "down": {"sql": down_sql.strip(), "statement_types": down_types},
        "execution_note": "Not executed. Review and run manually.",
    }
