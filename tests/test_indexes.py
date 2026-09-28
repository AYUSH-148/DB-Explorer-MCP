import pytest

from indexes import IndexCoverage, covering_column_lists


def _coverage(covering, case_sensitive=False):
    return IndexCoverage(
        primary_key=[],
        covering=covering,
        has_any_index=bool(covering),
        case_sensitive=case_sensitive,
    )


def test_the_primary_key_counts_as_an_index():
    covering = covering_column_lists(
        indexes=[{"column_names": ["created_at"]}],
        primary_key=["id"],
    )

    assert covering == [["created_at"], ["id"]]


def test_a_table_with_nothing_has_no_covering_lists():
    assert covering_column_lists([], []) == []


def test_an_expression_index_covers_only_the_columns_before_the_expression():
    # How Postgres reflects CREATE INDEX ON t (tenant_id, lower(email)) and
    # CREATE INDEX ON t (lower(email), tenant_id).
    covering = covering_column_lists(
        indexes=[
            {"column_names": ["tenant_id", None]},
            {"column_names": [None, "tenant_id"]},
        ],
        primary_key=[],
    )

    assert covering == [["tenant_id"]]


@pytest.mark.parametrize(
    "dialect_options",
    [
        # Partial indexes, as each dialect reflects the predicate.
        {"postgresql_where": "deleted_at IS NULL"},
        {"sqlite_where": "deleted = 0"},
        {"mssql_where": "deleted = 0"},
        # Access methods that cannot answer fk_column = $1.
        {"postgresql_using": "gin"},
        {"postgresql_using": "brin"},
        {"postgresql_using": "gist"},
        {"mysql_prefix": "FULLTEXT"},
        {"mariadb_prefix": "SPATIAL"},
    ],
)
def test_an_index_that_cannot_find_rows_by_value_does_not_cover(dialect_options):
    covering = covering_column_lists(
        indexes=[{"column_names": ["user_id"], "dialect_options": dialect_options}],
        primary_key=[],
    )

    assert covering == []


@pytest.mark.parametrize(
    "dialect_options",
    [{}, {"postgresql_using": "hash"}, {"postgresql_where": None}],
)
def test_an_equality_index_covers(dialect_options):
    covering = covering_column_lists(
        indexes=[{"column_names": ["user_id"], "dialect_options": dialect_options}],
        primary_key=[],
    )

    assert covering == [["user_id"]]


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
        ([], [["user_id"]], False),
        (["user_id"], [], False),
    ],
)
def test_coverage_requires_the_columns_as_a_leading_prefix(columns, covering, expected):
    assert _coverage(covering).covers(columns) is expected


def test_names_match_case_insensitively_outside_postgres():
    assert _coverage([["userid"]]).covers(["UserId"]) is True


def test_names_match_exactly_on_postgres():
    # A quoted "UserId" and userid are two different Postgres columns.
    assert _coverage([["userid"]], case_sensitive=True).covers(["UserId"]) is False
    assert _coverage([["UserId"]], case_sensitive=True).covers(["UserId"]) is True
