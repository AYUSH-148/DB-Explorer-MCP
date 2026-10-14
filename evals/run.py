"""Run the eval scenarios: Claude Code works each task through this server's tools.

Each scenario runs headless (claude -p) with this server as its only MCP server
and no built-in tools, from an empty directory and with no settings sources, so
your hooks, CLAUDE.md and other MCP servers stay out of the run. Runs on the
Claude subscription Claude Code is logged in with and count toward its usage
limits; no API key is involved.

Every tool call, its result and the run's token usage are recorded from the
stream-json output, each run is scored against its scenario, and a second
claude -p call, with no tools, judges the whole trajectory.

    uv run python evals/run.py                         # every scenario
    uv run python evals/run.py --only top_customers    # one scenario
    uv run python evals/run.py --model opus
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

EVALS = Path(__file__).resolve().parent
REPO = EVALS.parent
sys.path.insert(0, str(EVALS))

from scenarios import SCENARIOS  # noqa: E402
from seed_eval_db import EVAL_DATABASE, create_eval_database  # noqa: E402

DEFAULT_MODEL = "sonnet"
SERVER_NAME = "dbeval"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
# claude -p has no turn limit, so a run that loops is cut off by time instead.
RUN_TIMEOUT_SECONDS = 300
# Enough for every result the scenarios produce, so the judge can check the
# answer's facts against what the tools actually returned.
JUDGE_RESULT_CHARS = 5000

# Flags that keep the user's own setup out of a run: no settings sources (so no
# hooks or permissions from them), no MCP server but this one, no session saved.
ISOLATION = ["--setting-sources", "", "--strict-mcp-config", "--no-session-persistence"]

JUDGE_PROMPT = """\
You grade an AI agent that answered a user's question using read-only database
tools. Judge whether it met the user's goal and how efficiently it got there:
right tools, sensible arguments, no wasted or repeated calls, an answer that is
correct given the tool results and says what it could not do.

User's request: {prompt}
What a good run does: {goal}

Tool calls, in order:
{trajectory}

Final answer:
{answer}

Reply with only a JSON object: {{"score": <1-5>, "reason": "<one sentence>"}}
5 = correct and efficient, 3 = correct but wasteful or partly wrong, 1 = failed."""


def appears(value: Any, answer: str) -> bool:
    """Whether a value appears in the answer as a whole token, ignoring case and
    thousands separators, so 4 does not match inside user34 or 1,234."""
    needle = re.escape(str(value).lower())
    return re.search(rf"(?<![\w.]){needle}(?![\w])", answer.lower().replace(",", "")) is not None


def score(scenario: dict[str, Any], calls: list[dict[str, Any]], answer: str,
          truth: list[Any]) -> dict[str, Any]:
    """Score one run against its scenario. Every check must hold for passed."""
    called = [call["tool"] for call in calls]
    seen: set[str] = set()
    repeats = 0
    for call in calls:
        key = call["tool"] + json.dumps(call["arguments"], sort_keys=True)
        repeats += key in seen
        seen.add(key)
    checks = {
        "expected_tools": all(tool in called for tool in scenario.get("expect_all", []))
        and (not scenario.get("expect_any")
             or any(tool in called for tool in scenario["expect_any"])),
        "no_forbidden_tools": not set(called) & set(scenario.get("forbid", [])),
        "within_budget": len(calls) <= scenario["max_calls"],
        "answer_has_truth": all(appears(value, answer) for value in truth),
        "answer_mentions": all(
            text.lower() in answer.lower() for text in scenario.get("mention", [])
        ),
        "no_errors": not scenario.get("no_errors")
        or not any(call["error_code"] for call in calls),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "tool_calls": len(calls),
        "repeated_calls": repeats,
        "error_codes": [call["error_code"] for call in calls if call["error_code"]],
        "missing_truth": [value for value in truth if not appears(value, answer)],
    }


def ground_truth(scenario: dict[str, Any]) -> list[Any]:
    if "truth_sql" not in scenario:
        return []
    uri = f"file:{EVAL_DATABASE.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return [value for row in connection.execute(scenario["truth_sql"]) for value in row]


def mcp_config() -> str:
    # The real environment of the server process wins over .env, so the run can
    # only ever see eval.db, whatever DATABASE_URL .env holds.
    return json.dumps({"mcpServers": {SERVER_NAME: {
        "command": sys.executable,
        "args": [str(REPO / "server.py")],
        "env": {
            "DATABASE_URL": f"sqlite:///file:{EVAL_DATABASE.as_posix()}?mode=ro&uri=true",
            "MCP_TRANSPORT": "stdio",
        },
    }}})


def claude(arguments: list[str], prompt: str, workdir: Path):
    """Start claude -p with the prompt on stdin, which avoids quoting it."""
    process = subprocess.Popen(
        [shutil.which("claude") or "claude", "-p", *arguments],
        cwd=workdir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    process.stdin.write(prompt)
    process.stdin.close()
    return process


def tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(item.get("text", "") for item in content or [])


def run_scenario(model: str, scenario: dict[str, Any], workdir: Path) -> dict[str, Any]:
    process = claude(
        [*ISOLATION, "--model", model, "--output-format", "stream-json", "--verbose",
         "--tools", "", "--mcp-config", mcp_config(), "--allowedTools", f"mcp__{SERVER_NAME}"],
        scenario["prompt"],
        workdir,
    )
    timer = threading.Timer(RUN_TIMEOUT_SECONDS, process.kill)
    timer.start()
    started = time.perf_counter()
    calls: list[dict[str, Any]] = []
    pending: dict[str, tuple[dict[str, Any], float]] = {}
    final: dict[str, Any] = {}
    try:
        for line in process.stdout:
            arrived = time.perf_counter()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                final = event
                continue
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    call = {"tool": block["name"].removeprefix(TOOL_PREFIX),
                            "arguments": block.get("input") or {}, "error_code": None}
                    calls.append(call)
                    pending[block["id"]] = (call, arrived)
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    call, asked = pending.pop(block["tool_use_id"])
                    text = tool_result_text(block.get("content"))
                    if block.get("is_error"):
                        code = re.match(r"\[(\w+)\]", text)
                        call["error_code"] = code.group(1) if code else "error"
                    # Measured from when the call was streamed to when its result
                    # was, so it includes Claude Code's own overhead.
                    call["seconds"] = round(arrived - asked, 3)
                    call["result_chars"] = len(text)
                    call["result"] = text
        process.wait()
    finally:
        timer.cancel()

    usage = final.get("usage") or {}
    answer = final.get("result") or ""
    if not final:
        answer = f"[no result: claude exited {process.returncode}] {process.stderr.read()[:500]}"
    return {
        "answer": answer,
        "calls": calls,
        "usage": {
            "input_tokens": sum(usage.get(key, 0) for key in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
            "output_tokens": usage.get("output_tokens", 0),
            "turns": final.get("num_turns"),
            # What the run would cost on the API; a subscription is not charged it.
            "api_equivalent_usd": final.get("total_cost_usd"),
        },
        "seconds": round(time.perf_counter() - started, 2),
    }


def judge(model: str, scenario: dict[str, Any], run: dict[str, Any], workdir: Path) -> dict[str, Any]:
    trajectory = "\n".join(
        f"{n}. {call['tool']}({json.dumps(call['arguments'])}) -> "
        f"{'ERROR ' if call['error_code'] else ''}{call.get('result', '')[:JUDGE_RESULT_CHARS]}"
        for n, call in enumerate(run["calls"], start=1)
    ) or "(none)"
    process = claude(
        [*ISOLATION, "--model", model, "--output-format", "json", "--tools", ""],
        JUDGE_PROMPT.format(prompt=scenario["prompt"], goal=scenario["goal"],
                            trajectory=trajectory, answer=run["answer"]),
        workdir,
    )
    output, _ = process.communicate(timeout=RUN_TIMEOUT_SECONDS)
    try:
        text = json.loads(output).get("result") or ""
    except ValueError:
        text = output
    found = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        verdict = json.loads(found.group(0))
        return {"score": int(verdict["score"]), "reason": str(verdict["reason"])}
    except (AttributeError, KeyError, ValueError, TypeError):
        return {"score": None, "reason": f"unparsable judge reply: {text[:200]}"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=DEFAULT_MODEL, help="sonnet, opus, or a full model id")
    parser.add_argument("--judge-model", default=None, help="defaults to --model")
    parser.add_argument("--only", nargs="*", help="scenario ids to run")
    args = parser.parse_args()

    if not shutil.which("claude"):
        sys.exit("The claude CLI is not on PATH; install Claude Code and log in first.")
    scenarios = [s for s in SCENARIOS if not args.only or s["id"] in args.only]
    if not scenarios:
        sys.exit(f"No scenario matches {args.only}. Ids: {[s['id'] for s in SCENARIOS]}")
    if not EVAL_DATABASE.exists():
        create_eval_database()

    report = {"model": args.model, "judge_model": args.judge_model or args.model,
              "started": datetime.now().isoformat(timespec="seconds"), "runs": []}
    results = EVALS / "results"
    results.mkdir(exist_ok=True)
    path = results / f"{datetime.now():%Y%m%d-%H%M%S}-{args.model}.json"
    # An empty directory, so no project CLAUDE.md or .mcp.json is picked up.
    with tempfile.TemporaryDirectory(prefix="dbeval-") as workdir:
        for scenario in scenarios:
            print(f"running {scenario['id']}...", file=sys.stderr)
            run = run_scenario(args.model, scenario, Path(workdir))
            run.update(score(scenario, run["calls"], run["answer"], ground_truth(scenario)))
            run["judge"] = judge(report["judge_model"], scenario, run, Path(workdir))
            report["runs"].append({"id": scenario["id"], **run})
            # Saved after every scenario, so a crash or Ctrl+C keeps the runs done.
            path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(f"\n{'scenario':22} {'pass':5} {'calls':6} {'rep':4} {'errors':22} {'judge':6} "
          f"{'tokens in/out':14} secs")
    for run in report["runs"]:
        budget = next(s["max_calls"] for s in SCENARIOS if s["id"] == run["id"])
        failed = [name for name, ok in run["checks"].items() if not ok]
        print(f"{run['id']:22} {'yes' if run['passed'] else 'NO':5} "
              f"{run['tool_calls']}/{budget:<4} {run['repeated_calls']:<4} "
              f"{','.join(run['error_codes']) or '-':22} {run['judge']['score'] or '?':<6} "
              f"{run['usage']['input_tokens']}/{run['usage']['output_tokens']:<8} "
              f"{run['seconds']}" + (f"  failed: {', '.join(failed)}" if failed else ""))
    runs = report["runs"]
    scores = [run["judge"]["score"] for run in runs if run["judge"]["score"]]
    cost = sum(run["usage"]["api_equivalent_usd"] or 0 for run in runs)
    print(f"\npassed {sum(run['passed'] for run in runs)}/{len(runs)}, "
          f"mean judge {sum(scores) / len(scores) if scores else 0:.2f}, "
          f"tokens in {sum(run['usage']['input_tokens'] for run in runs)} "
          f"out {sum(run['usage']['output_tokens'] for run in runs)}, "
          f"API-equivalent ${cost:.2f} (agent runs only)")
    print(f"report: {path}")


if __name__ == "__main__":
    main()
