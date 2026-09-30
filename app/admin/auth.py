"""
Admin 认证: 密码散列与校验。

密码散列
--------
新密码使用 **scrypt**(stdlib `hashlib.scrypt`, 加盐、可调成本) 存储:

    scrypt$<n>$<r>$<p>$<base64(salt)>$<base64(dk)>

历史数据兼容: 早期版本使用无盐 `sha256(hex)`(64 字符)。`verify_password()`
仍可校验该格式, 一旦通过 `/setup` 或 `set_admin_password()` 重新设置密码即
自动升级为 scrypt。所有比较使用 `hmac.compare_digest`(常量时间)。
"""
import base64
import hashlib
import hmac
import secrets
from typing import Optional

from fastapi import HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

security = HTTPBasic()

# scrypt 参数: N=2^14, r=8, p=1 -> 单次校验约 16MB 内存
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_PREFIX = "scrypt"
_LEGACY_SHA256_HEX_LEN = 64


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def get_password_hash(password: str) -> str:
    """生成 scrypt 密码散列 (每个密码独立随机盐)。"""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"{_SCRYPT_PREFIX}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"


def _verify_scrypt(plain_password: str, hashed_password: str) -> bool:
    try:
        prefix, n, r, p, salt_b64, dk_b64 = hashed_password.split("$")
        if prefix != _SCRYPT_PREFIX:
            return False
        expected = _b64d(dk_b64)
        dk = hashlib.scrypt(
            plain_password.encode("utf-8"),
            salt=_b64d(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def _verify_legacy_sha256(plain_password: str, hashed_password: str) -> bool:
    """历史格式: 无盐 sha256 hex (原实现会截断到 72 字符, 保留同样行为)。"""
    legacy = plain_password[:72] if len(plain_password) > 72 else plain_password
    digest = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, hashed_password.lower())


def is_legacy_hash(hashed_password: Optional[str]) -> bool:
    if not hashed_password:
        return False
    if hashed_password.startswith(f"{_SCRYPT_PREFIX}$"):
        return False
    return len(hashed_password) == _LEGACY_SHA256_HEX_LEN


def verify_password(plain_password: str, hashed_password: Optional[str]) -> bool:
    """校验密码; 同时支持 scrypt(新) 与 sha256(历史) 两种格式。"""
    if not plain_password or not hashed_password:
        return False
    if hashed_password.startswith(f"{_SCRYPT_PREFIX}$"):
        return _verify_scrypt(plain_password, hashed_password)
    if is_legacy_hash(hashed_password):
        return _verify_legacy_sha256(plain_password, hashed_password)
    return False


def get_admin_password_hash(db: Session) -> str:
    """读取管理员密码散列; 配置行缺失时返回空串 (视为未初始化, 不抛错)。"""
    from app.database.models import Configuration

    config = db.query(Configuration).first()
    if not config:
        return ""

    return config.admin_password_hash or ""


def is_first_time_setup(db: Session) -> bool:
    """管理员密码尚未设置 (空散列) => 需要初始化。"""
    from app.database.models import Configuration

    config = db.query(Configuration).first()
    if not config:
        return True

    return not config.admin_password_hash


def set_admin_password(db: Session, password: str) -> None:
    """
    写入/更新管理员密码散列 (scrypt)。

    配置行缺失时自动创建: 历史实现要求 Configuration 行必须已存在 (由 init_db 建立),
    否则 /setup 会静默无效甚至 500, 首次部署路径因此依赖启动顺序。此处自愈。
    """
    from app.database.models import Configuration

    config = db.query(Configuration).first()
    if not config:
        config = Configuration()
        db.add(config)
    config.admin_password_hash = get_password_hash(password)
    db.commit()


def authenticate_admin(credentials: HTTPBasicCredentials, db: Session) -> bool:
    try:
        hashed_password = get_admin_password_hash(db)
    except HTTPException:
        return False
    return verify_password(credentials.password, hashed_password)


def verify_admin_password(password: str, db: Session) -> bool:
    try:
        hashed_password = get_admin_password_hash(db)
    except HTTPException:
        return False
    return verify_password(password, hashed_password)
