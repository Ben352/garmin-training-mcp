"""Eval: ask Claude the questions in questions.toml, with this MCP server's tools, on
synthetic data; check and grade the answers.

Usage:
    export ANTHROPIC_API_KEY=...        # never stored in the repo
    uv run --group eval python -m evals.run
    uv run --group eval python -m evals.run --only race_time,runs_in_may
    uv run --group eval python -m evals.run --model claude-haiku-4-5-20251001
    uv run --group eval python -m evals.run --label "shorter query description"

Per question: a fresh copy of the synthetic database and a fresh server process (the
real `garmin-mcp` over stdio, as Claude Code runs it), so questions can't affect each
other. Claude gets the server's instructions as its system prompt and its tools,
and loops until it answers (at most --max-turns API calls). Then:
  1. code checks: expected values in the answer, required/forbidden tool calls;
  2. a grader model judges the answer against the question's rubric.
A question passes only if both do. Full results go to evals/results/<time>-<model>.json
and a summary row to evals/HISTORY.md; both are committed, to track changes.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic
import anyio
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from evals.checks import Question, code_checks, load_questions

DEFAULT_MODEL = "claude-sonnet-5-5"
RESULTS_DIR = Path(__file__).with_name("results")
HISTORY = Path(__file__).with_name("HISTORY.md")
HISTORY_HEADER = """# Eval history

One row per run of `python -m evals.run`, newest last. Commit is the code the run
tested ("+dirty": with uncommitted changes). Full answers and tool calls are in the
linked JSON.

| ran | commit | label | model | passed | turns | cost | failed | details |
|---|---|---|---|---|---|---|---|---|
"""
MAX_TOKENS = 4096
EPHEMERAL = {"type": "ephemeral"}
RESOURCE_TOOL = "read_mcp_resource"

# USD per million tokens: (input, output). Cache writes cost 1.25x input, cache
# reads 0.1x. Check https://www.anthropic.com/pricing; unknown models show no cost.
PRICES = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}

GRADER_SYSTEM = (
    "You grade answers from an assistant that answers questions about a runner's "
    "own Garmin data. Judge the answer only against the expected answer. Rounding "
    "differences, other wording and extra correct detail are fine. Fail it if a "
    "stated fact contradicts the expected answer, or if it misses or breaks "
    "something the expected answer requires. Reply with only a JSON object: "
    '{"passed": true or false, "reason": "<one sentence>"}'
)


@dataclass
class Usage:
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0

    def add(self, u: Any) -> None:
        self.input += u.input_tokens
        self.cache_write += u.cache_creation_input_tokens or 0
        self.cache_read += u.cache_read_input_tokens or 0
        self.output += u.output_tokens

    def cost(self, model: str) -> float | None:
        if model not in PRICES:
            return None
        inp, out = PRICES[model]
        return (
            self.input * inp
            + self.cache_write * inp * 1.25
            + self.cache_read * inp * 0.1
            + self.output * out
        ) / 1e6


@dataclass
class Result:
    id: str
    question: str
    passed: bool = False
    failed_checks: list[str] = field(default_factory=list)
    grade: dict[str, Any] | None = None
    answer: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    usage: Usage = field(default_factory=Usage)
    grader_usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = None
    duration_s: float = 0.0
    error: str | None = None


def build_synthetic_db(base: Path) -> Path:
    """The demo database, built by the normal fetch commands (about 15 s)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"GARMIN_DB_PATH", "GARMIN_AUDIT_PATH"}
    }
    env |= {"GARMIN_SYNTHETIC": "1", "GARMIN_DATA_DIR": str(base)}
    subprocess.run(
        [sys.executable, "-m", "garmin_mcp.demo"],
        env=env,
        check=True,
        capture_output=True,
    )
    return base / "garmin.duckdb"


def server_params(workdir: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "garmin_mcp.server"],
        env={
            "GARMIN_SYNTHETIC": "1",
            "GARMIN_DATA_DIR": str(workdir),
            "GARMIN_AUDIT_PATH": str(workdir / "audit.duckdb"),
            "GARMINTOKENS": str(workdir / "no-tokens"),
        },
    )


def resource_tool(resources: list[Any]) -> dict[str, Any]:
    """Claude Code lets the model read MCP resources (e.g. schema://tables, which the
    query tool points to) through a tool of its own; this is the eval's equivalent."""
    listing = "; ".join(f"{r.uri}: {r.description}" for r in resources)
    return {
        "name": RESOURCE_TOOL,
        "description": f"Read a resource from the garmin MCP server. Available: {listing}",
        "input_schema": {
            "type": "object",
            "properties": {"uri": {"type": "string"}},
            "required": ["uri"],
        },
    }


async def call(mcp: Client, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """(result text, is_error) for a tool call, including the resource tool."""
    if name != RESOURCE_TOOL:
        out = await mcp.call_tool(name, args)
        return tool_result_text(out), bool(out.is_error)
    try:
        out = await mcp.read_resource(args["uri"])
    except Exception as e:  # noqa: BLE001 -- returned to the model, like Claude Code does
        return f"Could not read {args.get('uri')!r}: {e}", True
    return "\n".join(getattr(c, "text", "") for c in out.contents), False


def tool_result_text(result: Any) -> str:
    texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    if texts:
        return "\n".join(texts)
    return json.dumps(result.structured_content, default=str)


def with_cache_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark only the newest block, so each call reuses the previous call's prefix."""
    marked = []
    for i, m in enumerate(messages):
        content = m["content"]
        if i == len(messages) - 1 and isinstance(content, list):
            content = [*content[:-1], {**content[-1], "cache_control": EPHEMERAL}]
        marked.append({**m, "content": content})
    return marked


async def ask(
    llm: anthropic.AsyncAnthropic,
    model: str,
    q: Question,
    workdir: Path,
    max_turns: int,
    r: Result,
) -> None:
    """Run the agent loop for one question, filling in r."""
    async with Client(server_params(workdir)) as mcp:
        tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.input_schema,
            }
            for t in (await mcp.list_tools()).tools
        ]
        tools.append(resource_tool((await mcp.list_resources()).resources))
        system = [
            {"type": "text", "text": mcp.instructions or "", "cache_control": EPHEMERAL}
        ]
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [{"type": "text", "text": q.question}]}
        ]
        while r.turns < max_turns:
            r.turns += 1
            response = await llm.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tools,
                messages=with_cache_breakpoint(messages),
            )
            r.usage.add(response.usage)
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                r.answer = "\n".join(
                    b.text for b in response.content if b.type == "text"
                ).strip()
                return
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                text, is_error = await call(mcp, block.name, block.input)
                r.tool_calls.append(
                    {
                        "name": block.name,
                        "input": block.input,
                        "is_error": is_error,
                        "result_chars": len(text),
                    }
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": text,
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": results})
        r.error = f"no answer within {max_turns} turns"


async def grade(
    llm: anthropic.AsyncAnthropic, model: str, q: Question, r: Result
) -> None:
    response = await llm.messages.create(
        model=model,
        max_tokens=512,
        system=GRADER_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": f"Question: {q.question}\n\nExpected answer: {q.rubric}"
                f"\n\nAnswer to grade:\n{r.answer}",
            }
        ],
    )
    r.grader_usage.add(response.usage)
    text = "".join(b.text for b in response.content if b.type == "text")
    found = re.search(r"\{.*\}", text, re.DOTALL)
    if not found:
        raise ValueError(f"grader didn't return JSON: {text[:200]!r}")
    r.grade = json.loads(found.group())


async def run_one(
    llm: anthropic.AsyncAnthropic,
    args: argparse.Namespace,
    q: Question,
    db: Path,
    tmp: Path,
) -> Result:
    r = Result(id=q.id, question=q.question)
    workdir = tmp / q.id
    workdir.mkdir()
    shutil.copy(db, workdir / "garmin.duckdb")
    began = time.monotonic()
    try:
        await ask(llm, args.model, q, workdir, args.max_turns, r)
        if r.error is None:
            r.failed_checks = code_checks(
                q, r.answer, [c["name"] for c in r.tool_calls]
            )
            await grade(llm, args.grader_model, q, r)
    except Exception as e:  # noqa: BLE001 -- one failing question mustn't stop the run
        r.error = f"{type(e).__name__}: {e}"
    r.duration_s = round(time.monotonic() - began, 1)
    r.passed = (
        r.error is None
        and not r.failed_checks
        and r.grade is not None
        and r.grade["passed"]
    )
    costs = [r.usage.cost(args.model), r.grader_usage.cost(args.grader_model)]
    r.cost_usd = None if None in costs else round(sum(costs), 4)
    mark = "PASS" if r.passed else "FAIL"
    print(f"  {mark}  {q.id}  ({r.turns} turns, {r.duration_s} s)", flush=True)
    return r


def report(results: list[Result], args: argparse.Namespace) -> None:
    print(f"\n{'question':<24} {'result':<6} {'turns':>5} {'tools':<40} why")
    for r in results:
        tools = ",".join(c["name"] for c in r.tool_calls)
        why = "; ".join(
            [*([r.error] if r.error else []), *r.failed_checks]
            + ([r.grade["reason"]] if r.grade and not r.grade["passed"] else [])
        )
        print(
            f"{r.id:<24} {'pass' if r.passed else 'FAIL':<6} {r.turns:>5} "
            f"{tools[:40]:<40} {why}"
        )
    passed = sum(r.passed for r in results)
    total = Usage()
    for r in results:
        for u in (r.usage, r.grader_usage):
            for k in asdict(total):
                setattr(total, k, getattr(total, k) + getattr(u, k))
    costs = [r.cost_usd for r in results]
    cost = (
        f"${sum(costs):.2f}" if None not in costs else "unknown (model not in PRICES)"
    )
    print(
        f"\n{passed}/{len(results)} passed with {args.model}. Tokens: "
        f"{total.input:,} input, {total.cache_write:,} cache write, "
        f"{total.cache_read:,} cache read, {total.output:,} output. Cost: {cost}."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--grader-model", default=DEFAULT_MODEL)
    parser.add_argument("--only", help="comma-separated question ids")
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--label", default="", help="what changed, for HISTORY.md (e.g. 'baseline')"
    )
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> None:
    questions = load_questions()
    if args.only:
        wanted = set(args.only.split(","))
        unknown = wanted - {q.id for q in questions}
        if unknown:
            sys.exit(f"Unknown question ids: {', '.join(sorted(unknown))}")
        questions = [q for q in questions if q.id in wanted]

    llm = anthropic.AsyncAnthropic()
    limiter = anyio.CapacityLimiter(args.concurrency)
    results: dict[str, Result] = {}
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        print("Building the synthetic database...", flush=True)
        db = build_synthetic_db(tmp / "base")
        print(f"Asking {len(questions)} questions with {args.model}:", flush=True)

        async def worker(q: Question) -> None:
            async with limiter:
                results[q.id] = await run_one(llm, args, q, db, tmp)

        async with anyio.create_task_group() as tg:
            for q in questions:
                tg.start_soon(worker, q)

    ordered = [results[q.id] for q in questions]
    report(ordered, args)
    save(ordered, args)


def git_commit() -> str:
    """Short HEAD hash, with "+dirty" if the working tree has uncommitted changes."""

    def git(*a: str) -> str:
        return subprocess.run(
            ["git", *a], capture_output=True, text=True, check=False
        ).stdout.strip()

    dirty = git(
        "status", "--porcelain", "--", ".", ":!evals/results", ":!evals/HISTORY.md"
    )
    return git("rev-parse", "--short", "HEAD") + ("+dirty" if dirty else "")


def save(results: list[Result], args: argparse.Namespace) -> None:
    """Full transcripts to evals/results/, one summary row to evals/HISTORY.md."""
    now = datetime.now()
    passed = sum(r.passed for r in results)
    costs = [r.cost_usd for r in results]
    cost = round(sum(costs), 2) if None not in costs else None
    summary = {
        "ran_at": now.isoformat(timespec="seconds"),
        "commit": git_commit(),
        "label": args.label,
        "model": args.model,
        "grader_model": args.grader_model,
        "passed": passed,
        "total": len(results),
        "turns": sum(r.turns for r in results),
        "cost_usd": cost,
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{now:%Y%m%d-%H%M%S}-{args.model}.json"
    out.write_text(
        json.dumps(
            {**summary, "results": [asdict(r) for r in results]}, indent=2, default=str
        )
        + "\n"
    )
    failed = ", ".join(r.id for r in results if not r.passed) or "-"
    row = (
        f"| {now:%Y-%m-%d %H:%M} | {summary['commit']} | {args.label or ''} | "
        f"{args.model} | {passed}/{len(results)} | {summary['turns']} | "
        f"{'' if cost is None else f'${cost:.2f}'} | {failed} | [json](results/{out.name}) |\n"
    )
    if not HISTORY.exists():
        HISTORY.write_text(HISTORY_HEADER)
    with HISTORY.open("a") as f:
        f.write(row)
    print(f"Saved {out} and added a row to {HISTORY}")


def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first (export it in your shell, not a file).")
    anyio.run(main_async, parse_args())


if __name__ == "__main__":
    main()
