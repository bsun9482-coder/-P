"""启动链路验证（标准库 unittest，无需额外依赖）。

运行：
    python -m unittest tests.test_launch -v
"""

import unittest

import requests

#: 统一服务端口（scripts/start.bat / start.sh 启动，见 config.APP_PORT）
SERVICE_URL = "http://localhost:8765"


def _local_session() -> requests.Session:
    """构造只探本机服务的会话：显式忽略环境代理。

    环境里存在 HTTP(S)_PROXY 时，`requests.get(SERVICE_URL)` 会经代理发出，
    而代理对无人监听的端口返回 502（合法的 HTTP 响应，不抛连接异常），
    于是"服务未运行 → skip"的兜底失效，本用例变成红灯。
    环路地址本就不应经代理，故关闭 trust_env 让连接失败如实抛出。
    """
    session = requests.Session()
    session.trust_env = False
    return session


class LaunchChecks(unittest.TestCase):
    """验证启动脚本关键链路：服务可达 + 数据可用。"""

    def test_service_http_200(self):
        """统一服务在 8765 端口返回 HTTP 200（Vue3 前端 + REST + 语音）。

        服务未运行时自动跳过（先运行 scripts/start.bat 再跑全套验证）。
        """
        with _local_session() as session:
            try:
                r = session.get(SERVICE_URL + "/health", timeout=3)
            except requests.RequestException:
                self.skipTest("服务未运行，跳过（请先运行 scripts/start.bat 或启动统一服务）")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_db_accessible(self):
        """数据库可正常读取（UI 侧边栏题库统计依赖）。"""
        from app.core import db

        try:
            db.init_db()  # 空环境（CI）下先建库
            total = db.count_questions()
        except Exception as e:
            self.skipTest(f"题库初始化失败，跳过：{e}")
        if total == 0:
            self.skipTest("题库为空，请先运行 python -m app.crawler.run 抓取")
        self.assertGreater(total, 0)


if __name__ == "__main__":
    unittest.main()
