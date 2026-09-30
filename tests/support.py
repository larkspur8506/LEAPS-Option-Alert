"""
测试公共工具 (P0 加固后的会话口径)。

背景: 管理员会话已从明文 Cookie `admin_logged_in=true` 改为服务端签名 Cookie
(`app.admin.security` + starlette SessionMiddleware)。因此测试不再伪造明文
Cookie, 统一通过 `login_client()` 注入合法会话。

注意: `SESSION_SECRET` 必须在 `app.main` 被导入之前设置好 (中间件在模块导入
时读取密钥), 所以这里在 import 时就 setdefault。
"""
import os

os.environ.setdefault("SESSION_SECRET", "unit-test-session-secret-not-for-production")

from app.admin.security import SESSION_COOKIE_NAME, create_session_token  # noqa: E402


def admin_session_token() -> str:
    """生成一个合法的管理员会话 token。"""
    return create_session_token({"admin": True})


def login_client(client) -> None:
    """给 TestClient 注入合法管理员会话 Cookie。"""
    client.cookies.set(SESSION_COOKIE_NAME, admin_session_token())


def logout_client(client) -> None:
    """清掉管理员会话 Cookie。"""
    try:
        client.cookies.delete(SESSION_COOKIE_NAME)
    except Exception:
        pass


def set_setup_token(value: str) -> None:
    """设置/清除 SETUP_TOKEN (运行时读取, 无需重启)。"""
    if value:
        os.environ["SETUP_TOKEN"] = value
    else:
        os.environ.pop("SETUP_TOKEN", None)
