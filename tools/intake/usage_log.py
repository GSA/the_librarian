"""Body-free record of direct model calls as an AgentsView "Direct API" session file."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import uuid


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Recorder:
    """Appends one assistant message of token usage per call to ``<session-id>.jsonl``.

    ``$AGENTSVIEW_DIRECT_API_DIR`` (default ``~/.agentsview/direct-api``) is read at the
    first record, which also creates the file. Prompts, documents and replies are never
    written, and a write failure is swallowed so it cannot affect the caller.
    """

    def __init__(self, name, provider, *, directory=None, cwd=None):
        self.name, self.provider = name, provider
        self.directory, self.cwd = directory, cwd or os.getcwd()
        self.session_id, self.path, self.last_id, self.lock = str(uuid.uuid4()), None, None, threading.Lock()

    def record(self, model, usage):
        usage = usage if isinstance(usage, dict) else {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        fresh = max((usage.get("prompt_tokens") or 0) - cached, 0)
        output = usage.get("completion_tokens") or 0
        now = _now()
        with self.lock:
            try:
                lines, parent, path = [], self.last_id, self.path
                if path is None:
                    parent = uuid.uuid4().hex[:8]
                    lines = [{"type": "session", "version": 3, "id": self.session_id,
                              "timestamp": now, "cwd": self.cwd},
                             {"type": "session_info", "id": parent, "parentId": None,
                              "timestamp": now, "name": self.name}]
                    directory = Path(self.directory or os.environ.get("AGENTSVIEW_DIRECT_API_DIR")
                                     or Path.home() / ".agentsview" / "direct-api")
                    directory.mkdir(parents=True, exist_ok=True)
                    path = directory / (self.session_id + ".jsonl")
                message_id = uuid.uuid4().hex[:8]
                lines.append({"type": "message", "id": message_id, "parentId": parent,
                              "timestamp": now, "message": {
                                  "role": "assistant", "provider": self.provider, "model": model,
                                  "content": [],
                                  "usage": {"input": fresh, "output": output, "cacheRead": cached,
                                            "cacheWrite": 0, "totalTokens": fresh + output + cached},
                                  "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000)}})
                with path.open("a") as out:
                    out.write("".join(json.dumps(line, separators=(",", ":")) + "\n"
                                      for line in lines))
                self.path, self.last_id = path, message_id
            except (OSError, ValueError):
                pass
