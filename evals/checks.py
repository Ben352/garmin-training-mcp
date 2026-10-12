"""Load the eval questions and run the code checks on an answer (no API calls)."""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

QUESTIONS_PATH = Path(__file__).with_name("questions.toml")
ALWAYS_FORBIDDEN = ("sync_garmin",)  # offer a sync, never run it unasked


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    rubric: str
    answer_matches: list[str] = field(default_factory=list)
    must_call: list[str] = field(default_factory=list)
    must_not_call: list[str] = field(default_factory=list)

    def forbidden(self) -> list[str]:
        return [*ALWAYS_FORBIDDEN, *self.must_not_call]


def load_questions(path: Path = QUESTIONS_PATH) -> list[Question]:
    with path.open("rb") as f:
        return [Question(**q) for q in tomllib.load(f)["question"]]


def code_checks(q: Question, answer: str, tools_called: list[str]) -> list[str]:
    """Failed checks as readable messages; empty if all passed."""
    failed = []
    for pattern in q.answer_matches:
        if not re.search(pattern, answer, re.IGNORECASE):
            failed.append(f"answer lacks /{pattern}/")
    for required in q.must_call:
        if not set(required.split("|")) & set(tools_called):
            failed.append(f"didn't call {required}")
    for tool in q.forbidden():
        if tool in tools_called:
            failed.append(f"called {tool}")
    return failed
