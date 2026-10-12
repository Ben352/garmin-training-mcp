"""The eval questions file is valid, and the code checks work. No API calls."""

import re

import anyio
from mcp.client import Client

from evals.checks import Question, code_checks, load_questions
from garmin_mcp.server import mcp


def server_tools():
    async def go():
        async with Client(mcp) as client:
            return {t.name for t in (await client.list_tools()).tools}

    return anyio.run(go)


def test_questions_file_is_valid():
    questions = load_questions()
    assert len(questions) == 15
    assert len({q.id for q in questions}) == len(questions)
    tools = server_tools()
    for q in questions:
        assert q.question and q.rubric, q.id
        for pattern in q.answer_matches:
            re.compile(pattern)
        for name in [*q.forbidden(), *"|".join(q.must_call).split("|")]:
            assert not name or name in tools, f"{q.id}: unknown tool {name}"


def test_code_checks():
    q = Question(
        id="x",
        question="?",
        rubric="",
        answer_matches=[r"\b20\b"],
        must_call=["query|list_activities"],
    )
    assert code_checks(q, "You did 20 runs.", ["list_activities"]) == []
    assert code_checks(q, "You did 10 runs.", ["get_run", "sync_garmin"]) == [
        r"answer lacks /\b20\b/",
        "didn't call query|list_activities",
        "called sync_garmin",
    ]
