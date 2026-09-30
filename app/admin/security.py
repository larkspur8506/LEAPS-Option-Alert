"""
Admin 会话与登录防护 (P0 安全加固)。

设计要点
--------
1. **签名会话 Cookie**: 用 `itsdangerous.URLSafeTimedSerializer` 对会话载荷签名,
   管理员登录状态存放于服务端签名的 token 中, 不再使用可伪造的静态
   `admin_logged_in=true` 明文 Cookie。token 带时间戳, 超过 `SESSION_MAX_AGE_HOURS`
   自动失效。
2. **会话密钥来源**: 环境变量 `SESSION_SECRET` > 数据目录下的
   `data/.session_secret`(自动生成, 0600) 。密钥落盘保证容器重启后已登录
   的管理员不会全部被踢出, 同时不把密钥写进代码或镜像。
3. **登录限速**: 进程内滑动窗口计数 (按客户端 IP + 全局兜底), 超过阈值返回
   429 并冷却, 阻止口令爆破。
"""
import logging
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "ndx_admin_session"
SESSION_SALT = "ndx-grid-alert.session"
DEFAULT_SESSION_MAX_AGE_HOURS = 168  # 7 天
_SECRET_FILE_NAME = ".session_secret"
_SECRET_FILE_MIN_BYTES = 32

# 登录限速默认值: 10 分钟内最多 5 次失败 (同一来源), 超过则冷却 10 分钟;
# 全局兜底阈值更高 (防止攻击者用大量 IP 触发全局锁死管理员)
LOGIN_MAX_FAILURES = 5
LOGIN_GLOBAL_MAX_FAILURES = 30
LOGIN_WINDOW_SECONDS = 600
LOGIN_LOCKOUT_SECONDS = 600


def _data_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")


def _secret_file_path() -> str:
    override = os.getenv("SESSION_SECRET_FILE", "").strip()
    if override:
        return override
    return os.path.join(_data_dir(), _SECRET_FILE_NAME)


def _load_or_create_secret_file() -> Optional[str]:
    path = _secret_file_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                value = fh.read().strip()
            if len(value) >= _SECRET_FILE_MIN_BYTES:
                return value
            logger.warning("[Session] Existing secret file is too short, regenerating: %s", path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        value = secrets.token_hex(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
        logger.info("[Session] Generated new session secret at %s", path)
        return value
    except Exception as exc:  # 只读文件系统等异常场景
        logger.error("[Session] Failed to load/create session secret file: %s", exc)
        return None


_SECRET_CACHE: Optional[str] = None
_SECRET_LOCK = threading.Lock()


def get_session_secret() -> str:
    """返回会话签名密钥 (env > 文件 > 内存随机值)。"""
    global _SECRET_CACHE
    env_value = os.getenv("SESSION_SECRET", "").strip()
    if env_value:
        return env_value
    with _SECRET_LOCK:
        if _SECRET_CACHE:
            return _SECRET_CACHE
        file_value = _load_or_create_secret_file()
        if file_value:
            _SECRET_CACHE = file_value
            return _SECRET_CACHE
        # 落盘失败时的最后兜底: 进程内随机密钥 (重启后会话失效, 但不会退化成静态 cookie)
        _SECRET_CACHE = secrets.token_hex(32)
        logger.warning("[Session] Falling back to in-process session secret (sessions reset on restart).")
        return _SECRET_CACHE


def get_session_max_age_seconds() -> int:
    raw = os.getenv("SESSION_MAX_AGE_HOURS", "").strip()
    try:
        hours = float(raw) if raw else float(DEFAULT_SESSION_MAX_AGE_HOURS)
    except ValueError:
        hours = float(DEFAULT_SESSION_MAX_AGE_HOURS)
    if hours <= 0:
        hours = float(DEFAULT_SESSION_MAX_AGE_HOURS)
    return int(hours * 3600)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_session_secret(), salt=SESSION_SALT)


def create_session_token(payload: Dict[str, Any]) -> str:
    return _serializer().dumps(payload)


def load_session_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """校验签名与时效; 非法/过期/被篡改一律返回 None。"""
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=get_session_max_age_seconds())
    except SignatureExpired:
        return None
    except BadSignature:
        return None
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def is_admin_session(token: Optional[str]) -> bool:
    data = load_session_token(token)
    return bool(data and data.get("admin") is True)


def cookie_secure_enabled() -> bool:
    """HTTPS 部署时置 true (默认 false, 兼容内网 http 访问)。"""
    return os.getenv("COOKIE_SECURE", "false").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# 登录限速 (进程内滑动窗口)
# ---------------------------------------------------------------------------

_ATTEMPTS_LOCK = threading.Lock()
_FAILED_ATTEMPTS: Dict[str, List[float]] = {}
_LOCKED_UNTIL: Dict[str, float] = {}


def _prune(now: float) -> None:
    for key in list(_FAILED_ATTEMPTS.keys()):
        _FAILED_ATTEMPTS[key] = [t for t in _FAILED_ATTEMPTS[key] if now - t < LOGIN_WINDOW_SECONDS]
        if not _FAILED_ATTEMPTS[key]:
            del _FAILED_ATTEMPTS[key]
    for key in list(_LOCKED_UNTIL.keys()):
        if _LOCKED_UNTIL[key] <= now:
            del _LOCKED_UNTIL[key]


def login_locked_seconds(client_key: str) -> int:
    now = time.time()
    with _ATTEMPTS_LOCK:
        _prune(now)
        until = _LOCKED_UNTIL.get(client_key)
        if until and until > now:
            return int(until - now) + 1
    return 0


def register_login_failure(client_key: str, threshold: int = LOGIN_MAX_FAILURES) -> None:
    now = time.time()
    with _ATTEMPTS_LOCK:
        _prune(now)
        bucket = _FAILED_ATTEMPTS.setdefault(client_key, [])
        bucket.append(now)
        if len(bucket) >= threshold:
            _LOCKED_UNTIL[client_key] = now + LOGIN_LOCKOUT_SECONDS
            _FAILED_ATTEMPTS[client_key] = []


def register_login_success(client_key: str) -> None:
    with _ATTEMPTS_LOCK:
        _FAILED_ATTEMPTS.pop(client_key, None)
        _LOCKED_UNTIL.pop(client_key, None)


def reset_login_limits() -> None:
    """仅测试使用: 清空限速状态。"""
    with _ATTEMPTS_LOCK:
        _FAILED_ATTEMPTS.clear()
        _LOCKED_UNTIL.clear()


def client_key(request) -> str:
    """限速维度: 优先 X-Forwarded-For 首个地址 (反向代理场景), 否则 socket 地址。

    注意: XFF 可被伪造, 因此仅用于限速维度 (不会授予任何权限), 另设全局兜底键。
    """
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return f"xff:{xff}"
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) or "unknown"
    return f"sock:{host}"


def attempt_keys(request) -> Tuple[str, str]:
    return client_key(request), "__global__"
