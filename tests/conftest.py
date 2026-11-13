"""Tests never touch the live bot's state.

`process_pr` clears state/current.json when it returns. A test that drives it without
redirecting that path erased the marker of a review that was running at the time, and
the app lost track of what the bot was looking at.
"""
import pytest

from lib import outcomes, store


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    # The run history is the same kind of file: a test that drives a review to its end
    # appended rows for made-up PRs to the real runs.jsonl, and the app listed them.
    for name, file in (('CURRENT', 'current.json'), ('LOG', 'runs.jsonl'), ('VERIFY_LOG', 'verify.jsonl')):
        monkeypatch.setattr(store, name, str(tmp_path / file))
    # A tick driven by a test would otherwise collect outcomes into the live state.
    monkeypatch.setattr(outcomes, 'path', lambda: str(tmp_path / 'outcomes.json'))
