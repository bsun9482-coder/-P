"""题库 REST API：浏览/检索、筛选元数据、添加自定义题、CSV 导入、按用户收藏。

题库为共享资源（爬虫 + 社区补充），收藏按用户隔离。
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import app.core.db as db
import app.services.importer as importer
from app.stores import auth

router = APIRouter(prefix="/api", tags=["questions"])

#: 题库来源的显示名（数据库存英文标识，界面统一展示中文）
SOURCE_LABELS = {
    "mianshiya": "面试鸭",
    "leetcode": "LeetCode",
    "nowcoder": "牛客",
    "custom": "自定义",
}


class AddQuestionBody(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    answer: str = Field(default="", max_length=20000)
    tags: list[str] = Field(default=[])
    difficulty: str = Field(default="中等")
    company: str = Field(default="")


def _qrow_to_dict(r) -> dict:
    return {
        "id": r["id"],
        "source": r["source"],
        "source_label": SOURCE_LABELS.get(r["source"], r["source"]),
        "title": r["title"],
        "content": r["content"],
        "answer": r["answer"],
        "tags": (r["tags"] or "").split(",") if r["tags"] else [],
        "difficulty": r["difficulty"] or "未知",
        "company": r["company"] or "",
        "url": r["url"],
    }


@router.get("/questions")
def browse(
    user_row=auth.CurrentUser,
    keyword: str = "",
    source: str = "",
    difficulty: str = "",
    company: str = "",
    favorite_only: bool = False,
    tags: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict:
    """题库浏览/检索，返回题目列表与当前用户收藏 id 集合。"""
    rows = db.browse_questions(
        keyword=keyword.strip() or None,
        tags=[t for t in (tags or []) if t.strip()] or None,
        source=source or None,
        difficulty=difficulty or None,
        company=company or None,
        favorite_only=favorite_only,
        user_id=user_row["id"],
        limit=limit,
    )
    favs = db.list_favorite_ids(user_row["id"])
    return {"items": [_qrow_to_dict(r) for r in rows], "favorite_ids": sorted(favs)}


@router.get("/questions/meta")
def question_meta(user_row=auth.CurrentUser) -> dict:
    """题库筛选元数据：来源（含计数）、公司列表、标签列表。"""
    srcs = [
        {"key": r["source"], "label": SOURCE_LABELS.get(r["source"], r["source"]), "count": r["n"]}
        for r in db.count_by_source()
    ]
    companies = db.list_companies()
    tags = [{"name": t, "count": c} for t, c in db.list_tags()]
    return {"sources": srcs, "companies": companies, "tags": tags}


@router.post("/questions")
def add_question(body: AddQuestionBody, user_row=auth.CurrentUser) -> dict:
    """添加一道自定义题（进入共享题库）。"""
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="题干不能为空")
    # 标签清洗：去空白、剔除含逗号的条目（tags 列按逗号分隔，混入会污染筛选）、
    # 限制条数与单条长度（bug #25）
    tags = [t.strip() for t in (body.tags or []) if t.strip() and "," not in t][:10]
    db.upsert_question(
        source="custom",
        title=title,
        answer=body.answer.strip() or None,
        tags=tags or None,
        difficulty=body.difficulty,
        company=body.company.strip() or None,
    )
    return {"ok": True}


@router.post("/questions/import")
def import_csv(body: dict, user_row=auth.CurrentUser) -> dict:
    """批量导入自定义题（CSV 文本）。"""
    content = (body or {}).get("content") or ""
    if not content.strip():
        raise HTTPException(status_code=400, detail="请粘贴 CSV 内容")
    if len(content) > 2_000_000:
        # 超大请求直接拒绝，防止一次性占用过多内存并让 DB 膨胀（bug #33）
        raise HTTPException(status_code=413, detail="导入内容过大，请分批导入（最多 2MB）")
    stats = importer.import_questions_csv(content)
    if not stats.get("rows"):
        raise HTTPException(status_code=400, detail="没有可导入的题目，请检查 CSV 格式")
    return stats


# ---------------------------------------------------------------- 收藏


@router.get("/favorites")
def favorites(user_row=auth.CurrentUser) -> dict:
    """当前用户收藏的题目 id 列表。"""
    return {"ids": sorted(db.list_favorite_ids(user_row["id"]))}


@router.post("/favorites/{qid}")
def add_favorite(qid: int, user_row=auth.CurrentUser) -> dict:
    # 题目存在性校验：FK 约束开启前已有的防御层，防止幽灵收藏（bug #24）
    if db.get_question_by_id(qid) is None:
        raise HTTPException(status_code=404, detail="题目不存在")
    db.add_favorite(qid, user_id=user_row["id"])
    return {"ok": True}


@router.delete("/favorites/{qid}")
def remove_favorite(qid: int, user_row=auth.CurrentUser) -> dict:
    db.remove_favorite(qid, user_id=user_row["id"])
    return {"ok": True}
