"""JavaGuide 面试指南适配器：抓取面试题专题页（VuePress 静态渲染 HTML，无需登录）。

页面结构（main.vp-page 内）：
    <h2>分类标题</h2>
    <h3>⟪⟫ 问题标题</h3>
    <ol>答案要点…</ol> / <blockquote>…</blockquote> / <p>…</p> / <pre>代码…</pre>
问题以 h3 标题呈现（带 ⟪⟫ 装饰前缀，需清理）；h3 与下一个 h2/h3 之间的兄弟节点为答案。
网络不佳时整页超时/失败只告警，不影响其他源。
"""

import hashlib
import logging
import re

from bs4 import BeautifulSoup

from app.crawler.base import SourceAdapter, make_session

logger = logging.getLogger("interview_coach.crawler.javaguide")

BASE = "https://javaguide.cn"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

#: 面试题专题页（主题标识, URL, 标签）；单页失败会被跳过，不影响其他页
TOPICS = [
    ("java-basic-01", f"{BASE}/java/basis/java-basic-questions-01.html", ["Java"]),
    ("java-basic-02", f"{BASE}/java/basis/java-basic-questions-02.html", ["Java"]),
    (
        "java-collection-01",
        f"{BASE}/java/collection/java-collection-questions-01.html",
        ["Java", "数据结构"],
    ),
    (
        "java-collection-02",
        f"{BASE}/java/collection/java-collection-questions-02.html",
        ["Java", "数据结构"],
    ),
    (
        "java-concurrent-01",
        f"{BASE}/java/concurrent/java-concurrent-questions-01.html",
        ["Java", "并发"],
    ),
    (
        "java-concurrent-02",
        f"{BASE}/java/concurrent/java-concurrent-questions-02.html",
        ["Java", "并发"],
    ),
    (
        "java-concurrent-03",
        f"{BASE}/java/concurrent/java-concurrent-questions-03.html",
        ["Java", "并发"],
    ),
    ("jvm", f"{BASE}/java/jvm/jvm-interview-questions.html", ["Java"]),
    ("redis-01", f"{BASE}/database/redis/redis-questions-01.html", ["Redis", "数据库"]),
    ("mysql-01", f"{BASE}/database/mysql/mysql-questions-01.html", ["MySQL", "数据库"]),
    (
        "os-basic-01",
        f"{BASE}/cs-basics/operating-system/operating-system-basic-questions-01.html",
        ["操作系统"],
    ),
]

#: 问题标题的装饰前缀（VuePress 装饰）与序号噪音
_TITLE_NOISE = re.compile(r"^[\s⟪⟫#]+|^\d+[\.、]\s*")
#: 非问题的标题（目录/总结/参考等）
_SKIP_TITLES = ("参考资料", "总结", "参考", "目录", "结语", "写在前面", "扩展阅读")
#: 答案最大长度（截断）
_ANSWER_MAX = 4000


def _stable_source_id(topic: str, title: str) -> str:
    """按标题哈希生成稳定 source_id：页内序号在源页中部插题后会集体偏移，故不能用序号。"""
    return f"{topic}:{hashlib.sha1(title.encode('utf-8')).hexdigest()[:12]}"


class JavaGuideAdapter(SourceAdapter):
    """JavaGuide 面试指南（中文，Java/后端通用面试题，量大、稳定、服务端渲染）。"""

    name = "javaguide"

    def __init__(self) -> None:
        self._session = make_session()

    def fetch(self, limit: int | None = None) -> list[dict]:
        """抓取全部专题页，返回题目字典列表；单页失败不影响其他。"""
        out: list[dict] = []
        for topic, url, tags in TOPICS:
            try:
                out.extend(self._fetch_topic(topic, url, tags))
            except Exception as e:
                logger.warning("javaguide 专题 %s 抓取失败: %s", topic, e)
        if limit:
            out = out[:limit]
        return out

    def _fetch_topic(self, topic: str, url: str, tags: list[str]) -> list[dict]:
        """抓单个专题页：解析 h3 问题 + 到下一个标题之间的兄弟节点作为答案。"""
        resp = self._session.get(url, headers=HEADERS, timeout=40)
        resp.raise_for_status()
        # 源站响应头不含 charset，requests 会按 HTTP 默认 ISO-8859-1 解码，
        # 导致中文全部变成 mojibake 入库（历史 BUG：题库 88% 题目乱码）。
        # javaguide.cn 全站为 UTF-8，显式指定后再取 resp.text。
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        main = soup.select_one("main.vp-page") or soup.select_one("article") or soup
        rows: list[dict] = []
        for h3 in main.find_all("h3"):
            title = _TITLE_NOISE.sub("", h3.get_text(" ", strip=True)).strip()
            if not title or any(s in title for s in _SKIP_TITLES):
                continue
            answer = self._collect_answer(h3)
            if not answer or len(answer) < 15:
                continue
            rows.append(
                {
                    # 稳定标识见 _stable_source_id；去重由 content_hash(title) 保证
                    "source_id": _stable_source_id(topic, title),
                    "title": title,
                    "content": None,
                    "answer": answer,
                    "tags": tags,
                    "difficulty": "中等",
                    "url": url,
                    "source": self.name,
                }
            )
        return rows

    @staticmethod
    def _collect_answer(h3) -> str | None:
        """收集 h3 之后直到下一个 h2/h3 的兄弟节点文本作为答案。"""
        parts: list[str] = []
        node = h3.find_next_sibling()
        while node is not None and node.name not in ("h2", "h3"):
            if node.name in ("script", "style", "ins", "iframe"):
                node = node.find_next_sibling()
                continue
            text = node.get_text(" ", strip=True)
            if text:
                parts.append(text)
            node = node.find_next_sibling()
        text = "\n".join(parts).strip()
        return text[:_ANSWER_MAX] or None
