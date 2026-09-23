"""接口响应契约守卫。

背景：此前所有路由的返回注解都是裸 `dict`，OpenAPI 只能生成空对象，字段靠翻源码猜。
补上 `app/routers/schemas.py` 的响应模型后，**风险变了**：FastAPI 会按模型过滤响应，
模型与真实返回脱节时会**静默丢掉字段**（不报错，前端直接少数据）。

本文件守三件事：
1. 每个 JSON 端点都必须声明 Pydantic 响应模型（SSE 端点除外，它们不是 JSON）；
2. 声明的模型必须真的出现在 OpenAPI components 里（文档可见）；
3. 模型的字段集必须与**真正生产这些 dict 的代码**一致 —— 即
   `auth.public_user` / `_qrow_to_dict` / `session_store.list_history` / `parse_report`，
   任一侧漂移都会失败。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.agent.coach import InterviewSession, _extract_dimensions, parse_report
from app.core import config, db
from app.core.ratelimit import reset_rate_limits
from app.main import app
from app.routers import questions as questions_api
from app.routers import schemas
from app.stores import auth, session_store
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel

#: 流式（SSE / text-event-stream）端点：响应体不是 JSON，故不声明 response_model
SSE_ROUTES: set[tuple[str, str]] = {
    ("POST", "/api/chat"),
    ("POST", "/api/custom/generate"),
}


def _iter_api_routes():
    """遍历 REST 路由，产出 (方法, 路径, 路由对象)。

    跳过 `include_in_schema=False` 的路由（如 SPA 静态回退 `/{full_path:path}`）：
    它们不进 OpenAPI，不属于公开接口契约。
    """
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.include_in_schema is False:
            continue
        for method in sorted(route.methods):
            if method in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                yield method, route.path, route


class ResponseModelDeclaredTests(unittest.TestCase):
    """1 / 2：声明完整性与文档可见性。"""

    def test_every_json_route_declares_response_model(self):
        """每个非 SSE 端点都必须声明 Pydantic 响应模型。"""
        missing: list[str] = []
        for method, path, route in _iter_api_routes():
            is_sse = (method, path) in SSE_ROUTES
            model = route.response_model
            if is_sse:
                self.assertIsNone(model, f"{method} {path} 是 SSE 端点，不应声明响应模型")
                media = getattr(route.response_class, "media_type", None)
                self.assertEqual(
                    media,
                    "text/event-stream",
                    f"{method} {path} 应在 OpenAPI 标注 text/event-stream，实际为 {media}",
                )
                continue
            if model is None or not (isinstance(model, type) and issubclass(model, BaseModel)):
                missing.append(f"{method} {path}")
        self.assertEqual(missing, [], f"以下端点缺少响应模型：{missing}")

    def test_response_models_are_in_openapi(self):
        """声明的模型必须真的出现在 OpenAPI 组件里（否则文档看不到）。"""
        components = app.openapi().get("components", {}).get("schemas", {})
        declared = {
            route.response_model.__name__
            for _, _, route in _iter_api_routes()
            if route.response_model is not None
        }
        self.assertTrue(declared, "没有解析到任何声明了响应模型的路由")
        absent = sorted(n for n in declared if n not in components)
        self.assertEqual(absent, [], f"以下模型未出现在 OpenAPI components：{absent}")


class ResponseModelMatchesProducerTests(unittest.TestCase):
    """3：模型字段集必须与真正生产响应 dict 的代码一致。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmp.name) / "contract.db")
        self._patch.start()
        db.init_db()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _assert_keys_equal(self, model: type[BaseModel], produced: dict, what: str):
        want = list(model.model_fields)
        got = list(produced)
        self.assertEqual(got, want, f"{what} 的键与 {model.__name__} 字段不一致：{got} != {want}")

    def test_public_user_matches_model(self):
        """`auth.public_user` 的键 == PublicUserOut 字段（顺序也要一致）。"""
        row = {"id": 1, "username": "u", "nickname": None, "persona": None}
        self._assert_keys_equal(schemas.PublicUserOut, auth.public_user(row), "public_user")

    def test_qrow_to_dict_matches_model(self):
        """`_qrow_to_dict` 的键 == QuestionOut 字段。"""
        row = {
            "id": 1,
            "source": "custom",
            "title": "t",
            "content": None,
            "answer": None,
            "tags": "a,b",
            "difficulty": None,
            "company": None,
            "url": None,
        }
        self._assert_keys_equal(
            schemas.QuestionOut, questions_api._qrow_to_dict(row), "_qrow_to_dict"
        )

    def test_list_history_item_matches_model(self):
        """`session_store.list_history` 单条的键 == HistoryItemOut 字段。"""
        uid = db.create_user("contractuser", auth.hash_password("secret123"), "契约用户")
        session_store.start_session(uid, InterviewSession("coach", user_id=uid))
        items = session_store.list_history(uid)
        self.assertEqual(len(items), 1, "应当有 1 条历史")
        self._assert_keys_equal(schemas.HistoryItemOut, items[0], "list_history item")

    def test_parse_report_matches_model(self):
        """`parse_report` 的键 == ReportDataOut 字段；维度元素 == DimensionOut 字段。"""
        sample = (
            "【总分】85\n\n- 技术正确性：40\n- 表达清晰度：45\n\n"
            "【知识薄弱点】\n- 需要改进算法理解\n\n【改进建议】\n- 阅读 Flask 源码\n"
        )
        report_data = parse_report(sample)
        self._assert_keys_equal(schemas.ReportDataOut, report_data, "parse_report")
        self.assertTrue(report_data["dimensions"], "样例报告应解析出维度分（否则本用例失去意义）")
        self._assert_keys_equal(
            schemas.DimensionOut, _extract_dimensions(sample)[0], "_extract_dimensions item"
        )


class SessionStateShapeTests(unittest.TestCase):
    """4：`GET /api/session` 两种分支形状必须与 SessionStateOut 对齐。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmp.name) / "state.db")
        self._patch.start()
        db.init_db()
        reset_rate_limits()
        self._client = TestClient(app)
        self._client.__enter__()
        auth_resp = self._client.post(
            "/api/auth/register",
            json={"username": "stateuser", "password": "secret123", "nickname": "状态用户"},
        )
        self.assertEqual(auth_resp.status_code, 200, auth_resp.text)
        self._headers = {"Authorization": f"Bearer {auth_resp.json()['token']}"}

    def tearDown(self):
        self._client.__exit__(None, None, None)
        self._patch.stop()
        self._tmp.cleanup()

    def test_inactive_shape_is_subset(self):
        """无活跃会话时只返回 5 个键，不得被模型补成 null。"""
        resp = self._client.get("/api/session", headers=self._headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            sorted(resp.json()),
            sorted(["active", "mode", "history", "finished", "report"]),
            "无活跃会话的返回形状发生了变化",
        )

    def test_active_shape_is_full(self):
        """有活跃会话时返回全部字段。"""
        start = self._client.post(
            "/api/session/start",
            headers=self._headers,
            json={"mode": "coach"},
        )
        self.assertEqual(start.status_code, 200, start.text)
        resp = self._client.get("/api/session", headers=self._headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(sorted(resp.json()), sorted(schemas.SessionStateOut.model_fields))


if __name__ == "__main__":
    unittest.main()
