"""账号、登录令牌与语音 WS 一次性票据。

三者放一起的理由：令牌与票据都是"用户身份的短期凭证"，共用同一套
"落库前哈希 + 到期清理"规则，改动时几乎总是一起改。
"""

import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from app.core.db.conn import get_conn, row_to_dict

# ---------------------------------------------------------------- 账号


def create_user(
    username: str, password_hash: str, nickname: str | None = None, persona: str = ""
) -> int | None:
    """创建账号，返回 user_id；用户名已存在返回 None。"""
    with closing(get_conn()) as conn, conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, nickname, persona, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    username,
                    password_hash,
                    nickname or None,
                    persona or None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def get_user_by_username(username: str):
    """按用户名取用户（用户名不区分大小写）。"""
    with closing(get_conn()) as conn:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchone()
        )


def get_user_by_id(user_id: int):
    with closing(get_conn()) as conn:
        return row_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def update_user_persona(user_id: int, persona: str) -> None:
    """更新用户默认面试官人格。"""
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE users SET persona = ? WHERE id = ?", (persona or None, user_id))


def update_user_nickname(user_id: int, nickname: str) -> None:
    """更新用户昵称。"""
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE users SET nickname = ? WHERE id = ?", (nickname or None, user_id))


def touch_user_login(user_id: int) -> None:
    """记录最近登录时间。"""
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), user_id),
        )


# ---------------------------------------------------------------- 凭证哈希


def _token_hash(token: str) -> str:
    """令牌/票据落库前统一哈希：SHA-256。

    令牌为 secrets.token_urlsafe(32) 高熵随机串，无需密码级 KDF 抗爆破；
    确定性哈希使存量明文行可无损迁移（客户端令牌在服务端哈希后照常匹配）。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 登录令牌


def create_auth_token(user_id: int, token: str, expires_at: str) -> None:
    """保存登录令牌（落库前哈希，明文不持久化）。

    签发时顺带清理该库全部过期令牌（过期行只增不减）。
    """
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM auth_tokens WHERE expires_at <= ?", (now,))
        conn.execute(
            "INSERT INTO auth_tokens (token_hash, user_id, created_at, expires_at) "
            "VALUES (?,?,?,?)",
            (_token_hash(token), user_id, now, expires_at),
        )


def get_user_by_token(token: str):
    """按令牌取用户（校验有效期）；无效/过期返回 None。查询值同样先哈希。"""
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_conn()) as conn:
        return row_to_dict(
            conn.execute(
                "SELECT u.* FROM auth_tokens t JOIN users u ON u.id = t.user_id "
                "WHERE t.token_hash = ? AND t.expires_at > ?",
                (_token_hash(token), now),
            ).fetchone()
        )


def revoke_token(token: str) -> None:
    """注销单个令牌。"""
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (_token_hash(token),))


def revoke_all_tokens(user_id: int) -> None:
    """注销某用户全部令牌（如修改密码/退出所有端）。"""
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))


# ---------------------------------------------------------------- WS 一次性票据


def create_ws_ticket(user_id: int, ticket: str, expires_at: str) -> None:
    """保存 WS 一次性连接票据（落库前哈希）。签发时顺带清理全部过期票据。"""
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM ws_tickets WHERE expires_at <= ?", (now,))
        conn.execute(
            "INSERT INTO ws_tickets (ticket_hash, user_id, created_at, expires_at) "
            "VALUES (?,?,?,?)",
            (_token_hash(ticket), user_id, now, expires_at),
        )


def consume_ws_ticket(ticket: str) -> int | None:
    """消费一次性票据：删除即返回（原子），保证单次有效，返回 user_id。

    无效 / 过期 / 已消费返回 None。

    用 `DELETE ... RETURNING` 把"校验 + 删除"合并为一条原子写操作，
    避免"先 SELECT 再 DELETE"的 TOCTOU 竞态——并发消耗同一票据时只会有
    一个成功，其余语句命中 0 行。
    """
    now = datetime.now(timezone.utc).isoformat()
    h = _token_hash(ticket)
    with closing(get_conn()) as conn, conn:
        row = conn.execute(
            "DELETE FROM ws_tickets WHERE ticket_hash = ? AND expires_at > ? RETURNING user_id",
            (h, now),
        ).fetchone()
        return row["user_id"] if row else None
