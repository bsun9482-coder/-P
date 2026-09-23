"""账号认证 REST API：注册 / 登录 / 登出 / 当前用户 / 资料更新。"""

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

import app.core.db as db
from app.core.ratelimit import rate_limit
from app.stores import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterBody(BaseModel):
    username: str = Field(min_length=3, max_length=32, description="登录名")
    password: str = Field(min_length=6, max_length=128, description="密码")
    nickname: str = Field(default="", max_length=32, description="昵称（显示名）")


class LoginBody(BaseModel):
    # 只限长不限短（老账号兼容），防超大 body 进 pbkdf2/日志
    username: str = Field(max_length=32)
    password: str = Field(max_length=128)


class ProfileBody(BaseModel):
    nickname: str = Field(default="", max_length=32)
    persona: str = Field(default="", max_length=64)


def _token_payload(user_row) -> dict:
    token = auth.issue_token(user_row["id"])
    return {"token": token, "user": auth.public_user(user_row)}


@router.post("/register")
def register(body: RegisterBody, _rate: None = Depends(rate_limit(limit=5, window=60))) -> dict:
    """注册并自动登录。"""
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="用户名不能为空")
    user = db.get_user_by_username(username)
    if user is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    user_id = db.create_user(username, auth.hash_password(body.password), body.nickname)
    if user_id is None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = db.get_user_by_id(user_id)
    return _token_payload(user)


@router.post("/login")
def login(body: LoginBody, _rate: None = Depends(rate_limit(limit=10, window=60))) -> dict:
    """登录，返回令牌与用户信息。"""
    user = db.get_user_by_username(body.username.strip())
    if user is None or not auth.verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    db.touch_user_login(user["id"])
    return _token_payload(user)


@router.post("/logout")
def logout(authorization: str | None = Header(default=None)) -> dict:
    """注销当前令牌（读取请求头 Bearer 串）。"""
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            db.revoke_token(token.strip())
    return {"ok": True}


@router.post("/ws-ticket")
def ws_ticket(
    user_row=auth.CurrentUser, _rate: None = Depends(rate_limit(limit=30, window=60))
) -> dict:
    """签发语音 WS 一次性连接票据。

    浏览器 new WebSocket() 无法携带请求头，改为前端持 Bearer 令牌先换一张
    短时（WS_TICKET_TTL_SECONDS）一次性票据，WS URL 只出现票据；
    长效令牌不再进入 URL（避免落入访问日志/代理/浏览器历史）。
    """
    return {"ticket": auth.issue_ws_ticket(user_row["id"])}


@router.get("/me")
def me(user_row=auth.CurrentUser) -> dict:
    """返回当前登录用户信息。"""
    return auth.public_user(user_row)


@router.put("/me")
def update_me(body: ProfileBody, user_row=auth.CurrentUser) -> dict:
    """更新昵称/默认人格。

    nickname 显式传入（含空串=清空，回退显示用户名）才更新；
    未传字段保持不动。
    """
    db.update_user_persona(user_row["id"], body.persona)
    if "nickname" in body.model_fields_set:
        db.update_user_nickname(user_row["id"], body.nickname.strip())
    return auth.public_user(db.get_user_by_id(user_row["id"]))
