"""Short, human-friendly single-use identifiers (word pairs like ``amber-fox``).

Why short is safe: a proposal token (and an undo handle) is a **workflow
binding**, not a security boundary. Any client that can reach the gateway can
call ``propose_tuning`` itself and receive its own token, so token entropy
defends against nothing; what the token provides is (a) binding an operator's
approval to exactly the change that was previewed and (b) single-use replay
protection. The gateway's actual security boundary is network reachability
plus the callers' tool allowlists (the autonomous cycle cannot reach the write
tools at all). Given that, a word pair the operator can read aloud, retype on
a phone, or match at a glance beats 32 hex characters.

Collisions are prevented against the caller-supplied ``taken`` collection; the
word space (32 x 32 = 1024 pairs) is comfortably larger than the handful of
proposals/tunings a deployment holds at once, with a numeric-suffix fallback
if the space is ever crowded.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Container

ADJECTIVES = (
    "amber", "bold", "brisk", "calm", "cedar", "civic", "coral", "crisp",
    "dapper", "deft", "dusky", "eager", "fabled", "fleet", "frank", "gilded",
    "hardy", "humble", "ivory", "jade", "keen", "limber", "lucid", "mellow",
    "noble", "olive", "plucky", "quiet", "rustic", "sage", "tidy", "vivid",
)

NOUNS = (
    "badger", "bison", "crane", "dingo", "egret", "falcon", "ferret", "finch",
    "gecko", "heron", "ibis", "jackal", "koala", "lemur", "lynx", "marmot",
    "marten", "moose", "newt", "ocelot", "orca", "otter", "panda", "pika",
    "quail", "raven", "seal", "shrew", "stork", "tapir", "vole", "wren",
)

_SEPARATORS = re.compile(r"[\s_\-]+")


def normalize(value: str) -> str:
    """Canonicalize operator input: ``'Amber Fox'`` -> ``'amber-fox'``.

    Case-insensitive; any run of spaces/underscores/hyphens collapses to a
    single hyphen. Leaves other content untouched so legacy hex tokens (or
    anything else) still round-trip through comparison unchanged.
    """
    return _SEPARATORS.sub("-", value.strip().lower())


def new_token(taken: Container[str] = ()) -> str:
    """A fresh word-pair identifier not present in *taken* (normalized form)."""
    for _ in range(64):
        candidate = f"{secrets.choice(ADJECTIVES)}-{secrets.choice(NOUNS)}"
        if candidate not in taken:
            return candidate
    # The pair space is crowded (or pathological luck): disambiguate numerically.
    while True:
        candidate = (
            f"{secrets.choice(ADJECTIVES)}-{secrets.choice(NOUNS)}"
            f"-{secrets.randbelow(90) + 10}"
        )
        if candidate not in taken:
            return candidate
