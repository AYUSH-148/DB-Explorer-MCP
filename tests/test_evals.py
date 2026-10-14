from evals.run import appears, score
from evals.scenarios import SCENARIOS


def test_a_value_counts_only_as_a_whole_token():
    # user4 must not be found inside user34, and 1234 must survive a separator.
    assert appears("user4@example.com", "Emails: user4@example.com.")
    assert not appears("user4@example.com", "Emails: user34@example.com")
    assert appears(1234, "a total of 1,234 orders")
    assert not appears(10, "about 100 users")


def test_a_run_fails_on_any_broken_check():
    scenario = {"expect_all": ["execute_query"], "forbid": ["validate_migration"],
                "max_calls": 2, "mention": ["pro"]}
    call = {"tool": "execute_query", "arguments": {"sql": "SELECT 1"}, "error_code": None}

    good = score(scenario, [call], "10 users are on the pro plan", [10])
    assert good["passed"] and good["repeated_calls"] == 0

    wasteful = score(scenario, [call, call, call], "10 users are on the pro plan", [10])
    assert not wasteful["passed"]
    assert wasteful["repeated_calls"] == 2
    assert wasteful["checks"]["within_budget"] is False

    wrong = score(scenario, [call], "11 users are on the pro plan", [10])
    assert wrong["missing_truth"] == [10]

    refused = {**call, "error_code": "unsafe_query"}
    assert score(scenario, [refused], "10 users are on the pro plan", [10])["passed"]
    strict = score({**scenario, "no_errors": True}, [refused], "10 on pro", [10])
    assert strict["checks"]["no_errors"] is False


def test_every_scenario_names_real_tools_and_a_budget():
    tools = {"explore_schema", "execute_query", "explain_query", "validate_schema",
             "suggest_index", "migration_context", "validate_migration"}
    for scenario in SCENARIOS:
        named = {*scenario.get("expect_all", []), *scenario.get("expect_any", []),
                 *scenario.get("forbid", [])}
        assert named <= tools, scenario["id"]
        assert scenario["max_calls"] >= 1 and scenario["goal"], scenario["id"]
