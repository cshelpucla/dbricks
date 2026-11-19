"""Persistent chat history, one row per message, scoped to the signed-in user.

Live: a Delta table in Unity Catalog, written through the SQL warehouse (the app's data lives there
and the app container's disk does not persist). Demo/local: a SQLite file. Both keep the same shape:
(conversation_id, seq, user_email, role, content, artifacts JSON, created_at).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .. import config
from . import sqlexec

ARTIFACT_CAP = 400_000  # characters of artifact JSON kept per message
log = logging.getLogger(__name__)


def new_id() -> str:
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(artifacts: list) -> str:
    text = json.dumps(artifacts, default=str)
    return text if len(text) <= ARTIFACT_CAP else "[]"


class SqliteChatStore:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS chat_messages (conversation_id TEXT, seq INTEGER, user_email TEXT, "
                             "role TEXT, content TEXT, artifacts TEXT, created_at TEXT, PRIMARY KEY (conversation_id, seq))")

    def _q(self, sql, args=()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.fetchall()

    def list(self, user: str, limit: int = 50) -> list[dict]:
        rows = self._q("SELECT conversation_id, MIN(created_at), MAX(created_at), COUNT(*), "
                       "(SELECT content FROM chat_messages m WHERE m.conversation_id = c.conversation_id AND role = 'user' ORDER BY seq LIMIT 1) "
                       "FROM chat_messages c WHERE user_email = ? GROUP BY conversation_id ORDER BY MAX(created_at) DESC LIMIT ?", (user, limit))
        return [{"id": r[0], "created_at": r[1], "updated_at": r[2], "messages": r[3], "title": (r[4] or "")[:80]} for r in rows]

    def get(self, user: str, cid: str) -> list[dict]:
        rows = self._q("SELECT role, content, artifacts, created_at FROM chat_messages WHERE conversation_id = ? AND user_email = ? ORDER BY seq", (cid, user))
        return [{"role": r[0], "content": r[1], "artifacts": json.loads(r[2] or "[]"), "created_at": r[3]} for r in rows]

    def save_turn(self, user: str, cid: str, question: str, reply: str, artifacts: list) -> None:
        """The question and its answer, appended together."""
        now = _now()
        with self._lock:
            (n,) = self._db.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM chat_messages WHERE conversation_id = ?", (cid,)).fetchone()
            self._db.executemany("INSERT INTO chat_messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 [(cid, n, user, "user", question, "[]", now), (cid, n + 1, user, "assistant", reply, _clip(artifacts), now)])
            self._db.commit()

    def delete(self, user: str, cid: str) -> None:
        self._q("DELETE FROM chat_messages WHERE conversation_id = ? AND user_email = ?", (cid, user))


class DeltaChatStore:
    """Each warehouse statement takes about a second, so writes run on one background thread, in order, and a
    reply is not held up by its Delta commit. Conversations read or written here stay in memory, so a follow-up
    question does not wait for the warehouse either; this process is the table's only writer."""
    CACHED = 200  # conversations kept in memory

    def __init__(self):
        self.table = f"{config.CATALOG}.{config.SCHEMA}.{config.CHAT_TABLE}"
        self._ready = False
        self._recent: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
        self._lock = threading.Lock()
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chat-history")

    def _ensure(self):
        if not self._ready:
            sqlexec.run(f"CREATE TABLE IF NOT EXISTS {self.table} (conversation_id STRING, seq INT, user_email STRING, role STRING, "
                        "content STRING, artifacts STRING, created_at TIMESTAMP)")
            self._ready = True

    def _flush(self):
        """Waits for queued writes, so a read sees them."""
        self._writer.submit(lambda: None).result(timeout=60)

    def _remember(self, key: tuple[str, str], messages: list[dict]) -> None:
        with self._lock:
            self._recent[key] = messages
            self._recent.move_to_end(key)
            while len(self._recent) > self.CACHED:
                self._recent.popitem(last=False)

    def list(self, user: str, limit: int = 50) -> list[dict]:
        self._ensure()
        self._flush()
        rows = sqlexec.run(
            f"SELECT conversation_id, CAST(MIN(created_at) AS STRING), CAST(MAX(created_at) AS STRING), COUNT(*), "
            f"MIN_BY(content, CASE WHEN role = 'user' THEN seq END) FROM {self.table} WHERE user_email = :u "
            f"GROUP BY conversation_id ORDER BY MAX(created_at) DESC LIMIT {int(limit)}", {"u": ("STRING", user)})
        return [{"id": r[0], "created_at": r[1], "updated_at": r[2], "messages": int(r[3]), "title": (r[4] or "")[:80]} for r in rows]

    def get(self, user: str, cid: str) -> list[dict]:
        with self._lock:
            hit = self._recent.get((user, cid))
        if hit is not None:
            return list(hit)
        self._ensure()
        self._flush()
        rows = sqlexec.run(f"SELECT role, content, artifacts, CAST(created_at AS STRING) FROM {self.table} "
                           "WHERE conversation_id = :c AND user_email = :u ORDER BY seq",
                           {"c": ("STRING", cid), "u": ("STRING", user)})
        msgs = [{"role": r[0], "content": r[1], "artifacts": json.loads(r[2] or "[]"), "created_at": r[3]} for r in rows]
        if msgs:
            self._remember((user, cid), msgs)
        return list(msgs)

    def save_turn(self, user: str, cid: str, question: str, reply: str, artifacts: list) -> None:
        """Updates memory now and queues one INSERT of both messages. A conversation not in memory is a new one:
        the chat route reads an existing conversation (which keeps it here) just before answering."""
        now, art = _now(), _clip(artifacts)
        new = [{"role": "user", "content": question, "artifacts": [], "created_at": now},
               {"role": "assistant", "content": reply, "artifacts": json.loads(art), "created_at": now}]
        with self._lock:
            before = self._recent.get((user, cid)) or []
        self._remember((user, cid), before + new)
        self._writer.submit(self._insert_turn, user, cid, question, reply, art)

    def _insert_turn(self, user: str, cid: str, question: str, reply: str, art: str) -> None:
        try:
            self._ensure()
            sqlexec.run(
                f"INSERT INTO {self.table} WITH n AS (SELECT COALESCE(MAX(seq), -1) + 1 AS seq FROM {self.table} WHERE conversation_id = :c) "
                "SELECT :c, seq, :u, 'user', :q, '[]', current_timestamp() FROM n "
                "UNION ALL SELECT :c, seq + 1, :u, 'assistant', :t, :a, current_timestamp() FROM n",
                {"c": ("STRING", cid), "u": ("STRING", user), "q": ("STRING", question), "t": ("STRING", reply),
                 "a": ("STRING", art)})
        except Exception:  # noqa: BLE001 - the turn stays in memory; only the saved copy is lost
            log.exception("could not save chat turn %s", cid)

    def delete(self, user: str, cid: str) -> None:
        with self._lock:
            self._recent.pop((user, cid), None)
        self._writer.submit(self._delete, user, cid).result(timeout=60)  # after any queued write of that conversation

    def _delete(self, user: str, cid: str) -> None:
        self._ensure()
        sqlexec.run(f"DELETE FROM {self.table} WHERE conversation_id = :c AND user_email = :u",
                    {"c": ("STRING", cid), "u": ("STRING", user)})
