"""Best-effort redaction of secrets/PII from crash-dump-derived text.

Crash dumps routinely surface things nobody meant to persist or forward to a
third-party LLM: home-directory usernames baked into build paths, API keys
sitting in environment-variable dumps, private key material, etc. This module
scrubs the common, high-confidence cases before text is written to the
on-disk memory store (``memory/tools.py``) or sent to an LLM provider
(``graph/nodes.py``).

This is defense-in-depth, not a guarantee: it catches recognizable patterns,
not every possible secret shape. Treat crash dumps as sensitive regardless.
"""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

_KNOWN_TOKEN_PATTERNS = (
    r"\bAKIA[0-9A-Z]{16}\b",  # AWS access key ID
    r"\bsk-[A-Za-z0-9]{20,}\b",  # OpenAI-style secret key
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",  # GitHub personal/app tokens
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",  # Slack tokens
    r"\bBearer\s+[A-Za-z0-9\-_.=]{10,}",  # Bearer auth headers
)
_KNOWN_TOKEN_RE = re.compile("|".join(_KNOWN_TOKEN_PATTERNS))

# KEY=VALUE / KEY: VALUE style assignments whose key name suggests a secret
# (env var dumps, config output). Only the value is masked.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?im)^(?P<prefix>[ \t]*[\w.-]*(?:password|passwd|secret|token|api[_-]?key"
    r"|access[_-]?key|credential|private[_-]?key)[\w.-]*[ \t]*[:=][ \t]*)"
    r"(?P<value>\S+)"
)

# Home-directory usernames embedded in build/source paths.
_HOME_DIR_RE = re.compile(r"(?i)(/Users/|/home/|C:\\+Users\\+)([^/\\\s]+)")


def redact_sensitive(text: str) -> str:
    """Return *text* with recognizable secrets and usernames masked."""
    if not text:
        return text

    text = _PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
    text = _KNOWN_TOKEN_RE.sub("[REDACTED_TOKEN]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group('prefix')}[REDACTED]", text)
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _HOME_DIR_RE.sub(lambda m: f"{m.group(1)}[USER]", text)
    return text
