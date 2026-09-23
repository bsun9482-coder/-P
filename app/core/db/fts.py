"""FTS5 全文检索：查询串构造、检索原语与索引重建。

只做「把关键词变成 MATCH 查询串、拿回命中行」这一层，**不含回退编排** ——
trigram → unicode61 → LIKE 的三级回退放在 `questions.py`，因为它的终点是题目检索。

trigram 索引按 3 字符子串建，中文子串/组合词匹配远好于 unicode61 的逐字索引，
但**要求每个查询词都不短于 3 字符**；短词由调用方退回 unicode61。
"""

import re
import sqlite3
from contextlib import closing, suppress

from app.core.db.conn import get_conn, rows_to_dicts

#: 关键词切分：连续的 ASCII 字母数字下划线算一个词，连续的汉字算一个词
_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")


def escape_fts(text: str) -> str:
    """FTS5 字符串字面量里的双引号需成对转义。"""
    return text.replace('"', '""')


def build_query(keyword: str) -> str:
    """把用户关键词转成 FTS5 MATCH 短语查询（引号转义 + AND 连接）。"""
    tokens = [t for t in _FTS_TOKEN_RE.findall(keyword) if t.strip()]
    if not tokens:
        return f'"{escape_fts(keyword)}"'
    return " AND ".join(f'"{escape_fts(t)}"' for t in tokens)


def build_trigram_query(keyword: str) -> str | None:
    """trigram 查询串：要求所有词都 ≥3 字符（trigram 不支持短词），否则返回 None。"""
    tokens = [t for t in _FTS_TOKEN_RE.findall(keyword) if t.strip()]
    if not tokens or any(len(t) < 3 for t in tokens):
        return None
    return " AND ".join(f'"{escape_fts(t)}"' for t in tokens)


def trigram_rows(keyword: str, limit: int = 5) -> list[dict]:
    """trigram 索引检索：中文子串/组合词匹配（≥3 字符），按 bm25 排序。

    查询词不满足 ≥3 字符、或索引不可用时返回空列表（交由调用方下一级回退）。
    """
    query = build_trigram_query(keyword)
    if not query:
        return []
    sql = """SELECT q.* FROM questions q
             JOIN questions_fts_tr f ON q.id = f.rowid
             WHERE questions_fts_tr MATCH ?
             ORDER BY bm25(questions_fts_tr) LIMIT ?"""
    try:
        with closing(get_conn()) as conn:
            return rows_to_dicts(conn.execute(sql, (query, limit)).fetchall())
    except sqlite3.OperationalError:
        return []


def unicode_rows(keyword: str, limit: int = 5) -> list[dict]:
    """unicode61 索引检索（逐字分词），按 bm25 排序；索引不可用时返回空列表。"""
    sql = """SELECT q.* FROM questions q
             JOIN questions_fts f ON q.id = f.rowid
             WHERE questions_fts MATCH ?
             ORDER BY bm25(questions_fts) LIMIT ?"""
    try:
        with closing(get_conn()) as conn:
            return rows_to_dicts(conn.execute(sql, (build_query(keyword), limit)).fetchall())
    except sqlite3.OperationalError:
        return []


def rebuild() -> None:
    """重建 FTS5 索引（外部内容表的 rowid 与主表错位时用于修复）。"""
    with closing(get_conn()) as conn, conn:
        for t in ("questions_fts", "questions_fts_tr"):
            with suppress(sqlite3.OperationalError):
                conn.execute(f"INSERT INTO {t}({t}) VALUES('rebuild')")
