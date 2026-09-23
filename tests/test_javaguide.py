"""JavaGuide 适配器测试（mock HTML，不触网）。"""

import unittest
from unittest import mock

from app.crawler import javaguide, run

JG_HTML = """
<main class="vp-page">
  <h2>Java 基础常见面试题总结(上)</h2>
  <h3>⟪⟫ JVM vs JDK vs JRE</h3>
  <p>JVM 是 Java 虚拟机，JDK 是 Java 开发工具包，JRE 是 Java 运行环境，三者层层包含。</p>
  <h3>⟪⟫ 为什么说 Java 语言编译与解释并存？</h3>
  <ol>
    <li>Java 源代码先编译成字节码</li>
    <li>再由 JVM 解释执行字节码</li>
  </ol>
  <h3>参考资料</h3>
  <p>一些参考链接，不应被当作题目。</p>
</main>
"""


def _fake_get(text: str):
    resp = mock.Mock()
    resp.text = text
    resp.raise_for_status.return_value = None
    return resp


class JavaGuideTests(unittest.TestCase):
    def test_parse_topic(self):
        ad = javaguide.JavaGuideAdapter()
        with mock.patch("requests.Session.get", return_value=_fake_get(JG_HTML)):
            rows = ad._fetch_topic("java-basic-01", "https://x/", ["Java"])
        # 两个问题被解析；「参考资料」被过滤
        self.assertEqual(len(rows), 2)
        r = rows[0]
        # source_id 用标题哈希（稳定标识，不随页内位置偏移）
        self.assertEqual(
            r["source_id"], javaguide._stable_source_id("java-basic-01", "JVM vs JDK vs JRE")
        )
        self.assertEqual(r["title"], "JVM vs JDK vs JRE")  # ⟪⟫ 前缀被清理
        self.assertIn("JVM 是 Java 虚拟机", r["answer"])
        self.assertEqual(r["tags"], ["Java"])
        self.assertEqual(r["difficulty"], "中等")

    def test_answer_collects_siblings_until_next_h(self):
        ad = javaguide.JavaGuideAdapter()
        with mock.patch("requests.Session.get", return_value=_fake_get(JG_HTML)):
            rows = ad._fetch_topic("java-basic-01", "https://x/", ["Java"])
        r2 = rows[1]
        self.assertIn("编译成字节码", r2["answer"])
        self.assertIn("解释执行字节码", r2["answer"])

    def test_fetch_limit(self):
        ad = javaguide.JavaGuideAdapter()
        with mock.patch("requests.Session.get", return_value=_fake_get(JG_HTML)):
            rows = ad.fetch(limit=1)
        self.assertEqual(len(rows), 1)

    def test_fetch_topic_failure_skipped(self):
        ad = javaguide.JavaGuideAdapter()
        with mock.patch("requests.Session.get", side_effect=RuntimeError("boom")):
            rows = ad.fetch()
        self.assertEqual(rows, [], "专题页抓取失败应返回空而非抛异常")


class RegistrationTests(unittest.TestCase):
    def test_javaguide_registered(self):
        names = [a.name for a in run.ADAPTERS]
        self.assertIn("javaguide", names)
        self.assertIn("mianshiya", names)


if __name__ == "__main__":
    unittest.main()
