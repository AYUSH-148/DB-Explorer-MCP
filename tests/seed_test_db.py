from pathlib import Path

from sqlalchemy import create_engine, text


SCHEMA_SQL = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    total REAL NOT NULL
);

CREATE INDEX idx_orders_user_id ON orders(user_id);
"""


def create_sample_database(database_path: Path) -> None:
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        for statement in SCHEMA_SQL.split(";"):
            if statement.strip():
                connection.execute(text(statement))
        connection.execute(
            text("INSERT INTO users (name, email) VALUES (:name, :email)"),
            {"name": "Alice", "email": "alice@example.com"},
        )
        connection.execute(
            text("INSERT INTO orders (user_id, total) VALUES (:user_id, :total)"),
            {"user_id": 1, "total": 42.50},
        )


if __name__ == "__main__":
    create_sample_database(Path("sample.db"))
