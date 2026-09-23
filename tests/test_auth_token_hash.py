"""令牌安全改造回归测试。

覆盖 BUG 检测报告问题：
- #25 登录令牌改存 SHA-256 哈希（明文不落库）+ 存量 v7 明文库迁移 v8
- #23 语音 WS 一次性票据（URL 不再携带长效令牌）
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.core import config, db
from app.core.ratelimit import reset_rate_limits
from app.main import app
from app.stores import auth
from fastapi.testclient import TestClient


def _future(days: float = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


class TokenHashTests(unittest.TestCase):
    """令牌落库前哈希，明文不持久化；上层 API 仍用明文（对调用方透明）。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmpdir.name) / "hash.db")
        self._patch.start()
        db.init_db()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def _stored_hashes(self) -> set[str]:
        conn = db.get_conn()
        try:
            return {r["token_hash"] for r in conn.execute("SELECT token_hash FROM auth_tokens")}
        finally:
            conn.close()

    def test_plaintext_never_persisted(self):
        uid = db.create_user("u1", auth.hash_password("pass123456"))
        token = auth.issue_token(uid)
        self.assertNotIn(token, self._stored_hashes(), "明文令牌不得落库")
        self.assertTrue(all(len(t) == 64 for t in self._stored_hashes()), "应为 64 位 hex 哈希")

    def test_lookup_with_plaintext_token(self):
        uid = db.create_user("u2", auth.hash_password("pass123456"))
        token = auth.issue_token(uid)
        user = db.get_user_by_token(token)
        self.assertIsNotNone(user)
        self.assertEqual(user["id"], uid)
        # 确定性哈希：同一令牌重复查询稳定命中
        self.assertEqual(db.get_user_by_token(token)["id"], uid)

    def test_revoke_with_plaintext_token(self):
        uid = db.create_user("u3", auth.hash_password("pass123456"))
        token = auth.issue_token(uid)
        db.revoke_token(token)
        self.assertIsNone(db.get_user_by_token(token))

    def test_expired_tokens_purged_on_issue(self):
        uid = db.create_user("u4", auth.hash_password("pass123456"))
        db.create_auth_token(uid, "expired-tok", _past())
        db.create_auth_token(uid, "live-tok", _future())
        db.create_auth_token(uid, "new-tok", _future())
        self.assertEqual(len(self._stored_hashes()), 2)


class MigrationV8Tests(unittest.TestCase):
    """存量迁移：v7 明文库升级 v8 后旧令牌仍有效（用户不掉线）、幂等可重跑。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmpdir.name) / "migrate.db")
        self._patch.start()
        # 手工搭建 v7 旧库：users + auth_tokens（token 明文列），user_version=7
        conn = db.get_conn()
        with conn:
            conn.executescript(
                """
                CREATE TABLE users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    nickname      TEXT,
                    persona       TEXT,
                    created_at    TEXT NOT NULL,
                    last_login_at TEXT
                );
                CREATE TABLE auth_tokens (
                    token      TEXT PRIMARY KEY,
                    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                PRAGMA user_version = 7;
                """
            )
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                ("legacy", "pbkdf2$x$s$h", "2026-01-01T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO auth_tokens (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                ("legacy-plaintext-token", 1, "2026-01-01T00:00:00+00:00", _future()),
            )
            conn.execute(
                "INSERT INTO auth_tokens (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                ("expired-legacy-token", 1, "2026-01-01T00:00:00+00:00", _past()),
            )
            # 64 位 hex 视为已哈希行（幂等保护路径）：迁移后原值必须原样保留
            prehashed = db._token_hash("already-hashed-token")
            conn.execute(
                "INSERT INTO auth_tokens (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                (prehashed, 1, "2026-01-01T00:00:00+00:00", _future()),
            )
        conn.close()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def _token_hashes(self) -> set[str]:
        conn = db.get_conn()
        try:
            return {r["token_hash"] for r in conn.execute("SELECT token_hash FROM auth_tokens")}
        finally:
            conn.close()

    def test_legacy_plaintext_token_survives_migration(self):
        db.init_db()  # 触发 v7 -> v8 迁移
        # 存量明文令牌迁移后仍能通过明文查询命中（用户不掉线）
        user = db.get_user_by_token("legacy-plaintext-token")
        self.assertIsNotNone(user)
        self.assertEqual(user["username"], "legacy")
        # 过期行迁移时顺带清理；已哈希行原样保留（不二次哈希）
        self.assertEqual(
            self._token_hashes(),
            {db._token_hash("legacy-plaintext-token"), db._token_hash("already-hashed-token")},
        )
        # 迁移后 schema 为新结构（token_hash 主键，无 token 列）
        conn = db.get_conn()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(auth_tokens)")}
            self.assertEqual(cols, {"token_hash", "user_id", "created_at", "expires_at"})
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 8)
        finally:
            conn.close()

    def test_migration_idempotent(self):
        db.init_db()
        hashes = self._token_hashes()
        db.init_db()  # 重复执行不再改动数据
        self.assertEqual(self._token_hashes(), hashes)
        self.assertIsNotNone(db.get_user_by_token("legacy-plaintext-token"))


class WsTicketTests(unittest.TestCase):
    """一次性票据签发（落库哈希）/ 单次消费 / 过期与无效拒绝。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmpdir.name) / "ticket.db")
        self._patch.start()
        db.init_db()
        self.uid = db.create_user("tu", auth.hash_password("pass123456"))

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_issue_and_consume_once(self):
        ticket = auth.issue_ws_ticket(self.uid)
        conn = db.get_conn()
        try:
            stored = {r["ticket_hash"] for r in conn.execute("SELECT ticket_hash FROM ws_tickets")}
        finally:
            conn.close()
        self.assertNotIn(ticket, stored)
        user = auth.resolve_ws_ticket(ticket)
        self.assertIsNotNone(user)
        self.assertEqual(user["id"], self.uid)
        # 单次消费：第二次必须拒绝
        self.assertIsNone(auth.resolve_ws_ticket(ticket))

    def test_expired_ticket_rejected(self):
        db.create_ws_ticket(self.uid, "stale-ticket", _past())
        self.assertIsNone(auth.resolve_ws_ticket("stale-ticket"))

    def test_invalid_ticket_rejected(self):
        self.assertIsNone(auth.resolve_ws_ticket("no-such-ticket"))
        self.assertIsNone(auth.resolve_ws_ticket(""))
        self.assertIsNone(auth.resolve_ws_ticket(None))

    def test_issuing_purges_expired_rows(self):
        db.create_ws_ticket(self.uid, "stale-ticket", _past())
        auth.issue_ws_ticket(self.uid)
        conn = db.get_conn()
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM ws_tickets").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, 1, "签发时应顺带清理过期票据")


class WsTicketRestTests(unittest.TestCase):
    """REST 签发端点鉴权与返回契约。"""

    def setUp(self):
        reset_rate_limits()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmpdir.name) / "rest.db")
        self._patch.start()
        db.init_db()
        self.client = TestClient(app)

    def tearDown(self):
        self._patch.stop()
        reset_rate_limits()
        self._tmpdir.cleanup()

    def test_requires_auth(self):
        r = self.client.post("/api/auth/ws-ticket")
        self.assertEqual(r.status_code, 401)

    def test_issue_returns_consumable_ticket(self):
        r = self.client.post(
            "/api/auth/register", json={"username": "wsuser", "password": "pass123456"}
        )
        token = r.json()["token"]
        r = self.client.post("/api/auth/ws-ticket", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 200)
        ticket = r.json()["ticket"]
        user = auth.resolve_ws_ticket(ticket)
        self.assertIsNotNone(user)
        self.assertEqual(user["username"], "wsuser")


if __name__ == "__main__":
    unittest.main()
