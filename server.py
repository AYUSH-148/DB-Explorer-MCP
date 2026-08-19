from typing import Any

from fastmcp import FastMCP
from sqlalchemy import create_engine

from config import DATABASE_URL
from inspector import get_all_tables, get_table_detail


mcp = FastMCP("db-explorer")
engine = create_engine(DATABASE_URL)


def explore_schema_data(
    table_name: str | None = None,
    include_sample_data: bool = False,
) -> dict[str, Any]:
    if table_name:
        return get_table_detail(engine, table_name, include_sample_data)
    return {"tables": get_all_tables(engine)}


@mcp.tool
def explore_schema(
    table_name: str | None = None,
    include_sample_data: bool = False,
) -> dict[str, Any]:
    """Explore database tables, columns, keys, indexes, and sample rows."""
    return explore_schema_data(table_name, include_sample_data)


if __name__ == "__main__":
    mcp.run()
