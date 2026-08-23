"""Seed sample.db with the demo e-commerce schema from the project blueprint.

Larger and deliberately imperfect, so every tool has something to report:
missing indexes for suggest_index, a table with no primary key for
validate_schema, and a BLOB column to exercise value serialization.

Run: python seed_demo_db.py
Revert to the minimal fixture with: python tests/seed_test_db.py
"""

from __future__ import annotations

import random
from pathlib import Path

from sqlalchemy import create_engine, text


SCHEMA_SQL = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    plan TEXT DEFAULT 'free' CHECK(plan IN ('free', 'pro', 'enterprise')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price REAL NOT NULL,
    stock INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    total REAL NOT NULL,
    status TEXT DEFAULT 'pending'
        CHECK(status IN ('pending','confirmed','shipped','delivered','cancelled')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL DEFAULT 1,
    unit_price REAL NOT NULL
);

-- A BLOB column, so execute_query exercises binary serialization.
CREATE TABLE api_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token BLOB NOT NULL,
    expires_at TIMESTAMP
);

-- Deliberately has no primary key and no indexes, so validate_schema has a
-- finding to report. Realistic: append-only log tables often look like this.
CREATE TABLE event_log (
    event_type TEXT NOT NULL,
    payload TEXT,
    occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_orders_user_id ON orders(user_id);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_order_items_order_id ON order_items(order_id);
-- Deliberately missing: idx_order_items_product_id  (unindexed foreign key)
-- Deliberately missing: idx_api_sessions_user_id    (unindexed foreign key)
-- Deliberately missing: idx_orders_created_at       (so date filters scan)
"""

FIRST_NAMES = ["Alice", "Bob", "Carol", "Dan", "Erin", "Frank", "Grace", "Heidi",
               "Ivan", "Judy", "Karl", "Liam", "Mona", "Nina", "Omar", "Priya"]
LAST_NAMES = ["Shah", "Patel", "Iyer", "Roy", "Khan", "Das", "Nair", "Bose"]
CATEGORIES = ["keyboard", "monitor", "laptop", "cable", "headset", "webcam"]
STATUSES = ["pending", "confirmed", "shipped", "delivered", "cancelled"]
PLANS = ["free", "free", "free", "pro", "pro", "enterprise"]


def create_demo_database(database_path: Path) -> dict[str, int]:
    if database_path.exists():
        database_path.unlink()

    random.seed(42)  # reproducible, so the demo questions have stable answers
    engine = create_engine(f"sqlite:///{database_path}")

    with engine.begin() as connection:
        for statement in SCHEMA_SQL.split(";"):
            if statement.strip():
                connection.execute(text(statement))

        users = [
            {
                "name": f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}",
                "email": f"user{n}@example.com",
                "plan": random.choice(PLANS),
            }
            for n in range(1, 51)
        ]
        connection.execute(
            text("INSERT INTO users (name, email, plan) VALUES (:name, :email, :plan)"),
            users,
        )

        products = [
            {
                "name": f"{random.choice(CATEGORIES).title()} Model {n}",
                "category": random.choice(CATEGORIES),
                "price": round(random.uniform(9.99, 1499.99), 2),
                "stock": random.randint(0, 250),
            }
            for n in range(1, 21)
        ]
        connection.execute(
            text(
                "INSERT INTO products (name, category, price, stock) "
                "VALUES (:name, :category, :price, :stock)"
            ),
            products,
        )

        orders = [
            {
                "user_id": random.randint(1, 50),
                "total": round(random.uniform(19.99, 2999.99), 2),
                "status": random.choice(STATUSES),
            }
            for _ in range(200)
        ]
        connection.execute(
            text(
                "INSERT INTO orders (user_id, total, status) "
                "VALUES (:user_id, :total, :status)"
            ),
            orders,
        )

        items = [
            {
                "order_id": random.randint(1, 200),
                "product_id": random.randint(1, 20),
                "quantity": random.randint(1, 5),
                "unit_price": round(random.uniform(9.99, 1499.99), 2),
            }
            for _ in range(500)
        ]
        connection.execute(
            text(
                "INSERT INTO order_items (order_id, product_id, quantity, unit_price) "
                "VALUES (:order_id, :product_id, :quantity, :unit_price)"
            ),
            items,
        )

        sessions = [
            {"user_id": random.randint(1, 50), "token": random.randbytes(16)}
            for _ in range(30)
        ]
        connection.execute(
            text("INSERT INTO api_sessions (user_id, token) VALUES (:user_id, :token)"),
            sessions,
        )

        events = [
            {
                "event_type": random.choice(["login", "logout", "purchase", "refund"]),
                "payload": '{"source":"web"}',
            }
            for _ in range(120)
        ]
        connection.execute(
            text("INSERT INTO event_log (event_type, payload) VALUES (:event_type, :payload)"),
            events,
        )

    counts = {}
    with engine.connect() as connection:
        for table in ("users", "products", "orders", "order_items",
                      "api_sessions", "event_log"):
            counts[table] = connection.execute(
                text(f"SELECT COUNT(*) FROM {table}")
            ).scalar_one()
    return counts


if __name__ == "__main__":
    counts = create_demo_database(Path("sample.db"))
    print("sample.db seeded:")
    for table, count in counts.items():
        print(f"  {table:14} {count:4} rows")
