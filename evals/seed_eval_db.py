"""Seed evals/eval.db: the demo schema plus what the eval scenarios ask about.

On top of seed_demo_db.py it spreads order dates over 2025 (so "most recent" and
date-range questions have one answer), adds a reviews table whose foreign keys
have no index, and adds a view. Seeded from fixed random seeds, so scenario
answers are stable; the runner still reads each answer from the database.

Run: uv run python evals/seed_eval_db.py
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from seed_demo_db import create_demo_database  # noqa: E402

EVAL_DATABASE = Path(__file__).resolve().parent / "eval.db"

EXTRA_SQL = """
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    body TEXT,
    created_at TIMESTAMP
);
-- Deliberately missing: indexes on reviews.product_id and reviews.user_id

CREATE VIEW customer_order_summary AS
SELECT u.id AS user_id, u.email, COUNT(o.id) AS order_count,
       ROUND(COALESCE(SUM(o.total), 0), 2) AS lifetime_value
FROM users u LEFT JOIN orders o ON o.user_id = u.id
GROUP BY u.id, u.email
"""

# A clear best and worst category, so "which category rates highest" has one answer.
CATEGORY_RATING = {"keyboard": 4, "monitor": 5, "laptop": 3, "cable": 2,
                   "headset": 4, "webcam": 3}
REVIEW_TEXT = ["Works as described.", "Would buy again.", "Arrived late.",
               "Stopped working after a month.", "Great value.", None]


def create_eval_database(database_path: Path = EVAL_DATABASE) -> dict[str, int]:
    create_demo_database(database_path)
    rng = random.Random(7)
    start = datetime(2025, 1, 1)
    engine = create_engine(f"sqlite:///{database_path}")

    with engine.begin() as connection:
        for statement in EXTRA_SQL.split(";"):
            if statement.strip():
                connection.execute(text(statement))

        # Distinct minutes, so no two orders tie on created_at.
        minutes = rng.sample(range(365 * 24 * 60), 200)
        connection.execute(
            text("UPDATE orders SET created_at = :created_at WHERE id = :id"),
            [
                {"id": order_id, "created_at": f"{start + timedelta(minutes=m):%Y-%m-%d %H:%M:%S}"}
                for order_id, m in enumerate(minutes, start=1)
            ],
        )

        categories = dict(
            connection.execute(text("SELECT id, category FROM products")).all()
        )
        reviews = []
        for _ in range(300):
            product_id = rng.randint(1, 20)
            rating = CATEGORY_RATING[categories[product_id]] + rng.choice([-1, 0, 0, 1])
            reviews.append({
                "product_id": product_id,
                "user_id": rng.randint(1, 50),
                "rating": min(5, max(1, rating)),
                "body": rng.choice(REVIEW_TEXT),
                "created_at": f"{start + timedelta(days=rng.randint(0, 364)):%Y-%m-%d}",
            })
        connection.execute(
            text(
                "INSERT INTO reviews (product_id, user_id, rating, body, created_at) "
                "VALUES (:product_id, :user_id, :rating, :body, :created_at)"
            ),
            reviews,
        )

    with engine.connect() as connection:
        return {
            table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            for table in ("users", "products", "orders", "order_items",
                          "api_sessions", "event_log", "reviews")
        }


if __name__ == "__main__":
    counts = create_eval_database()
    print(f"{EVAL_DATABASE.name} seeded:")
    for table, count in counts.items():
        print(f"  {table:14} {count:4} rows")
