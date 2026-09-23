"""爬虫适配器测试（mock 网络响应，不触网）。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
from app.crawler import leetcode, mianshiya, run

MIANSHIYA_ROW = """
<table><tbody>
<tr class="ant-table-row">
  <td class="ant-table-cell"><a href="/question/123">1. 谈谈 GIL</a></td>
  <td class="ant-table-cell">中等</td>
  <td class="ant-table-cell"><span class="ant-tag">Python</span><span class="ant-tag">VIP</span></td>
</tr>
</tbody></table>
"""

HOT_HTML = (
    '<a href="/question/999">1. 什么是 AI Agent？它和直接调用大模型 API 有什么区别？3.2k热度</a>'
)

DETAIL_HTML = """
<h1>42. 谈谈 Python 的 GIL</h1>
<span class="ant-tag">困难</span><span class="ant-tag">Python</span>
<div class="answer-content">推荐答案视频讲解隐藏答案回答重点GIL 是全局解释器锁，限制多线程并行执行……</div>
"""

LEETCODE_PAYLOAD = {
    "stat_status_pairs": [
        {
            "stat": {
                "frontend_question_id": "1",
                "question__title": "Two Sum",
                "question__title_slug": "two-sum",
            },
            "difficulty": {"level": 1},
            "paid_only": False,
        },
        {
            "stat": {
                "frontend_question_id": "2",
                "question__title": "Add Two Numbers",
                "question__title_slug": "add-two-numbers",
            },
            "difficulty": {"level": 2},
            "paid_only": False,
        },
        {
            "stat": {
                "frontend_question_id": "3",
                "question__title": "Paid Question",
                "question__title_slug": "paid",
            },
            "difficulty": {"level": 3},
            "paid_only": True,
        },
    ]
}


def _fake_get(text: str = "", json_data=None):
    resp = mock.Mock()
    resp.text = text
    resp.json.return_value = json_data
    return resp


class MianShiYaTests(unittest.TestCase):
    @mock.patch("requests.Session.get", return_value=_fake_get(MIANSHIYA_ROW))
    def test_parse_row(self, mock_get):
        ad = mianshiya.MianShiYaAdapter()
        rows = ad._fetch_category("python", 1)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["source_id"], "123")
        self.assertEqual(r["title"], "谈谈 GIL")
        self.assertEqual(r["difficulty"], "中等")
        self.assertEqual(r["tags"], ["Python"])  # VIP 标签被过滤

    @mock.patch("requests.Session.get", return_value=_fake_get(MIANSHIYA_ROW))
    def test_fetch_limit_caps_rows(self, mock_get):
        ad = mianshiya.MianShiYaAdapter()
        with (
            mock.patch.object(mianshiya.config, "CRAWL_WORKERS", 1),
            mock.patch.object(mianshiya.config, "CRAWL_REQUEST_DELAY", 0.0),
            mock.patch.object(mianshiya.config, "CRAWL_PAGES_PER_CATEGORY", 1),
        ):
            rows = ad.fetch(limit=2)
        # mock 页面全部是同一道题（qid=123），跨分类/热门去重后只剩 1 条
        self.assertEqual(len(rows), 1)

    @mock.patch("requests.Session.get", return_value=_fake_get(HOT_HTML))
    def test_fetch_hot(self, mock_get):
        ad = mianshiya.MianShiYaAdapter()
        rows = ad._fetch_hot()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_id"], "999")
        self.assertIn("AI Agent", rows[0]["title"])
        self.assertNotIn("热度", rows[0]["title"])

    @mock.patch("requests.Session.get", return_value=_fake_get(DETAIL_HTML))
    def test_fetch_detail(self, mock_get):
        ad = mianshiya.MianShiYaAdapter()
        d = ad._fetch_detail("42")
        self.assertIsNotNone(d)
        self.assertEqual(d["difficulty"], "困难")
        self.assertIn("GIL", d["title"])
        self.assertTrue(d["answer"].startswith("GIL"))

    @mock.patch("requests.Session.get", return_value=_fake_get(DETAIL_HTML))
    def test_fetch_details_for_backfills(self, mock_get):
        """按 source_id 抓详情补答案：抓取 + 回写 db + 返回统计。"""
        ad = mianshiya.MianShiYaAdapter()
        with mock.patch("app.core.db.update_question_details", return_value=1) as m_upd:
            stats = ad.fetch_details_for(["42"])
        self.assertEqual(stats, {"total": 1, "updated": 1})
        m_upd.assert_called_once()
        self.assertEqual(m_upd.call_args.args[1], "42")

    def test_fetch_details_for_empty(self):
        ad = mianshiya.MianShiYaAdapter()
        self.assertEqual(ad.fetch_details_for([]), {"total": 0, "updated": 0})


class LeetCodeTests(unittest.TestCase):
    @mock.patch("requests.Session.get", return_value=_fake_get(json_data=LEETCODE_PAYLOAD))
    def test_parse_and_skip_paid(self, mock_get):
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(leetcode, "CACHE_FILE", Path(td) / "cache.json"),
        ):
            ad = leetcode.LeetCodeAdapter()
            rows = ad.fetch()
        self.assertEqual(len(rows), 2)  # 跳过 VIP 题
        self.assertIn("[LeetCode 1]", rows[0]["title"])
        self.assertEqual(rows[0]["difficulty"], "简单")
        self.assertEqual(rows[1]["difficulty"], "中等")

    @mock.patch("requests.Session.get", return_value=_fake_get(json_data=LEETCODE_PAYLOAD))
    def test_limit(self, mock_get):
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(leetcode, "CACHE_FILE", Path(td) / "cache.json"),
        ):
            ad = leetcode.LeetCodeAdapter()
            rows = ad.fetch(limit=1)
        self.assertEqual(len(rows), 1)

    def test_cache_fallback_on_network_error(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "cache.json"
            with mock.patch.object(leetcode, "CACHE_FILE", cache):
                leetcode.LeetCodeAdapter._save_cache(LEETCODE_PAYLOAD)
                with mock.patch(
                    "requests.Session.get", side_effect=requests.RequestException("网络错误")
                ):
                    ad = leetcode.LeetCodeAdapter()
                    rows = ad.fetch()
        self.assertEqual(len(rows), 2)

    def test_no_cache_raises_on_network_error(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "missing.json"
            with (
                mock.patch.object(leetcode, "CACHE_FILE", cache),
                mock.patch(
                    "requests.Session.get", side_effect=requests.RequestException("网络错误")
                ),
            ):
                ad = leetcode.LeetCodeAdapter()
                with self.assertRaises(RuntimeError):
                    ad.fetch()


class RunAdapterTests(unittest.TestCase):
    def test_crawl_all_passes_limit(self):
        m1 = mock.MagicMock(return_value={"source": "mianshiya", "new": 1})
        m2 = mock.MagicMock(return_value={"source": "javaguide", "new": 1})
        with (
            mock.patch.object(run.mianshiya.MianShiYaAdapter, "fetch_and_store", m1),
            mock.patch.object(run.javaguide.JavaGuideAdapter, "fetch_and_store", m2),
        ):
            stats = run.crawl_all(limit_per_source=3)
        self.assertEqual(len(stats), 2)
        for m in (m1, m2):
            m.assert_called_once_with(limit=3)

    def test_default_registry_has_no_stub_sources(self):
        """默认注册的数据源必须真能抓取（不再挂空壳占位适配器）。"""
        self.assertEqual([a.name for a in run.build_adapters()], ["mianshiya", "javaguide"])

    def test_leetcode_is_opt_in(self):
        """CRAWL_LEETCODE=1 时力扣插到第 2 位（README 承诺的开关行为）。"""
        with mock.patch.object(run.config, "CRAWL_LEETCODE", True):
            names = [a.name for a in run.build_adapters()]
        self.assertEqual(names, ["mianshiya", "leetcode", "javaguide"])


if __name__ == "__main__":
    unittest.main()
