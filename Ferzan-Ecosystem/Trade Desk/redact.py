"""Strip API keys, RPC secrets and bot tokens from text before it reaches a user or a log line."""
import os
import re

_QS = re.compile(r"(?i)((?:api[-_]?key|apikey|access[-_]?token|token|secret|key|auth)=)[^&\s'\"<>)]+")
_BOT = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")
_PATHKEY = re.compile(r"(?i)(https?://[^\s/'\"]+/(?:v\d/)?)[A-Za-z0-9_-]{24,}(?=[/\s'\"?]|$)")
_ENVHINT = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PRIVATE", "MNEMONIC", "RPC_URL", "RPC")


def scrub(text) -> str:
    s = str(text)
    s = _QS.sub(r"\1***", s)
    s = _BOT.sub("bot***", s)
    s = _PATHKEY.sub(r"\1***", s)
    for k, v in os.environ.items():
        if len(v) >= 12 and any(h in k.upper() for h in _ENVHINT) and v in s:
            s = s.replace(v, "***")
    return s
