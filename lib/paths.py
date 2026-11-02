"""Immutable release code and persistent installation data have separate roots."""
import os
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
HOME = Path(os.environ.get('REVIEW_BOT_HOME', str(CODE))).expanduser().resolve()


def version(root=None):
    try:
        return (Path(root or CODE) / 'VERSION').read_text().strip()
    except OSError:
        return 'unversioned'
