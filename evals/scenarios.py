"""Tasks a model is given against evals/eval.db, and what a good run looks like.

Each scenario may set:
  expect_all  tools the run must call
  expect_any  tools of which the run must call at least one
  forbid      tools the run must not call
  max_calls   tool-call budget; more than this fails the run
  truth_sql   run against eval.db; every value it returns must appear in the answer
  mention     literal strings the answer must contain (case-insensitive)
  no_errors   true fails the run if any tool call returned an error
  goal        what a good run does, for the judge
"""

SCENARIOS = [
    {
        "id": "list_tables",
        "prompt": "What tables and views are in this database? One line on what each holds.",
        "expect_all": ["explore_schema"],
        "forbid": ["execute_query"],
        "max_calls": 3,
        "mention": ["customer_order_summary", "reviews", "event_log"],
        "goal": "Lists every table and the view from one schema listing, without querying rows.",
    },
    {
        "id": "orders_by_status",
        "prompt": "How many orders are there in each status?",
        "expect_all": ["execute_query"],
        "max_calls": 4,
        "truth_sql": "SELECT status, COUNT(*) FROM orders GROUP BY status",
        "goal": "One GROUP BY query, after at most a quick look at the orders table.",
    },
    {
        "id": "top_customers",
        "prompt": (
            "Who are the top 3 customers by total spend on delivered orders? "
            "Give each one's email and total."
        ),
        "expect_all": ["execute_query"],
        "max_calls": 5,
        "truth_sql": (
            "SELECT u.email FROM orders o JOIN users u ON u.id = o.user_id "
            "WHERE o.status = 'delivered' GROUP BY u.id "
            "ORDER BY SUM(o.total) DESC LIMIT 3"
        ),
        "goal": "Joins orders to users, filters delivered, aggregates and ranks in SQL.",
    },
    {
        "id": "revenue_by_category",
        "prompt": "Which product category has brought in the most revenue from order items?",
        "expect_all": ["execute_query"],
        "max_calls": 5,
        "truth_sql": (
            "SELECT p.category FROM order_items oi JOIN products p ON p.id = oi.product_id "
            "GROUP BY p.category ORDER BY SUM(oi.quantity * oi.unit_price) DESC LIMIT 1"
        ),
        "goal": "Computes revenue as quantity * unit_price from order_items, grouped by category.",
    },
    {
        "id": "best_rated_category",
        "prompt": "Which product category has the highest average review rating?",
        "expect_all": ["execute_query"],
        "max_calls": 5,
        "truth_sql": (
            "SELECT p.category FROM reviews r JOIN products p ON p.id = r.product_id "
            "GROUP BY p.category ORDER BY AVG(r.rating) DESC LIMIT 1"
        ),
        "goal": "Joins reviews to products and averages rating per category in one query.",
    },
    {
        "id": "users_without_orders",
        "prompt": "Which users have never placed an order? Give their emails.",
        "expect_all": ["execute_query"],
        "max_calls": 4,
        "truth_sql": "SELECT email FROM users WHERE id NOT IN (SELECT user_id FROM orders)",
        "goal": "An anti-join (NOT EXISTS / NOT IN / LEFT JOIN ... IS NULL) in one query.",
    },
    {
        "id": "recent_orders",
        "prompt": "Show the 5 most recent orders with the customer's name.",
        "expect_all": ["execute_query"],
        "max_calls": 4,
        "truth_sql": "SELECT id FROM orders ORDER BY created_at DESC LIMIT 5",
        "goal": "Orders by created_at descending with LIMIT 5, joined to users for the name.",
    },
    {
        "id": "misnamed_table",
        "prompt": "How many rows does the user table have?",
        "expect_all": ["execute_query"],
        "max_calls": 4,
        "truth_sql": "SELECT COUNT(*) FROM users",
        "goal": (
            "Finds that the table is called users (from the schema or a "
            "table_not_found hint) and counts it."
        ),
    },
    {
        "id": "slow_date_query",
        "prompt": (
            "This query feels slow: SELECT * FROM orders WHERE created_at >= '2025-12-01'. "
            "Why, and what would fix it?"
        ),
        "expect_any": ["explain_query", "suggest_index"],
        "max_calls": 4,
        "mention": ["created_at"],
        "goal": "Reads the plan, sees a full scan of orders, recommends an index on created_at.",
    },
    {
        "id": "schema_problems",
        "prompt": "Audit this database's schema for structural problems.",
        "expect_all": ["validate_schema"],
        "max_calls": 4,
        "mention": ["event_log", "product_id"],
        "goal": (
            "Runs validate_schema and reports event_log's missing primary key and "
            "the unindexed foreign keys (order_items.product_id, reviews, api_sessions)."
        ),
    },
    {
        "id": "review_fk_indexes",
        "prompt": "Which foreign keys on the reviews table need an index? Give the SQL.",
        "expect_any": ["suggest_index", "validate_schema"],
        "max_calls": 3,
        "mention": ["product_id", "user_id", "CREATE INDEX"],
        "goal": "Calls suggest_index(table_name='reviews') and passes on both CREATE INDEX statements.",
    },
    {
        "id": "add_column_migration",
        "prompt": "Write a migration (up and down) that adds a nullable phone column to users.",
        "expect_all": ["validate_migration"],
        "forbid": ["execute_query"],
        "max_calls": 4,
        "mention": ["ALTER TABLE", "phone"],
        "goal": (
            "Reads users' structure, drafts up/down SQL for SQLite, checks it with "
            "validate_migration and says the user must run it."
        ),
    },
    {
        "id": "refuse_delete",
        "prompt": "Delete all cancelled orders.",
        "forbid": ["validate_migration"],
        # Sizing what a delete would touch is fair; trying the DELETE is not, and
        # the server refuses one with unsafe_query, so any error fails the run.
        "max_calls": 5,
        "no_errors": True,
        "goal": (
            "Explains this server is read-only and cannot delete, ideally without a "
            "doomed DELETE attempt; may offer the SQL for the user to run elsewhere."
        ),
    },
    {
        "id": "pro_users",
        "prompt": "How many users are on the pro plan?",
        "expect_all": ["execute_query"],
        "max_calls": 3,
        "truth_sql": "SELECT COUNT(*) FROM users WHERE plan = 'pro'",
        "goal": "One COUNT query filtered on plan.",
    },
]
