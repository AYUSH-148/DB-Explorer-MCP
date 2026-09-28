import pytest

from indexes import covering_column_lists, is_covered


def test_every_source_of_an_index_is_collected():
    covering = covering_column_lists(
        indexes=[{"column_names": ["created_at"]}],
        primary_key={"constrained_columns": ["id"]},
        unique_constraints=[{"column_names": ["email"]}],
    )

    assert covering == [["created_at"], ["id"], ["email"]]


def test_a_table_with_nothing_has_no_covering_lists():
    # SQLite reports a table without a primary key as constrained_columns [].
    assert covering_column_lists([], {"constrained_columns": []}, []) == []
    assert covering_column_lists([], {}, []) == []


def test_an_expression_index_covers_only_the_columns_before_the_expression():
    # How Postgres reflects CREATE INDEX ON t (tenant_id, lower(email)) and
    # CREATE INDEX ON t (lower(email), tenant_id).
    covering = covering_column_lists(
        indexes=[
            {"column_names": ["tenant_id", None]},
            {"column_names": [None, "tenant_id"]},
        ],
        primary_key={},
        unique_constraints=[],
    )

    assert covering == [["tenant_id"]]


@pytest.mark.parametrize(
    "columns, covering, expected",
    [
        (["user_id"], [["user_id"]], True),
        # A leading prefix of a wider index serves the lookup.
        (["user_id"], [["user_id", "tag"]], True),
        (["a", "b"], [["a", "b", "c"]], True),
        # A column that is not first does not.
        (["user_id"], [["team_id", "user_id"]], False),
        # Order matters: (b, a) cannot serve a lookup on (a, b).
        (["a", "b"], [["b", "a"]], False),
        # An index narrower than the key does not cover all of it.
        (["a", "b"], [["a"]], False),
        # Postgres treats a quoted "UserId" and userid as distinct columns.
        (["UserId"], [["userid"]], False),
        ([], [["user_id"]], False),
        (["user_id"], [], False),
    ],
)
def test_coverage_requires_the_columns_as_a_leading_prefix(columns, covering, expected):
    assert is_covered(columns, covering) is expected
