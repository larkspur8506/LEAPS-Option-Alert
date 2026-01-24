import hashlib
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

security = HTTPBasic()


def get_password_hash(password: str) -> str:
    if len(password) > 72:
        password = password[:72]
    sha256_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
    return sha256_hash


def verify_password(plain_password: str, hashed_password: str) -> bool:
    sha256_hash = hashlib.sha256(plain_password.encode('utf-8')).hexdigest()
    return sha256_hash == hashed_password


def get_admin_password_hash(db: Session) -> str:
    from app.database.models import Configuration

    config = db.query(Configuration).first()
    if not config:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Configuration not found"
        )

    return config.admin_password_hash


def is_first_time_setup(db: Session) -> bool:
    from app.database.models import Configuration

    config = db.query(Configuration).first()
    if not config:
        return True

    return not config.admin_password_hash


def authenticate_admin(credentials: HTTPBasicCredentials, db: Session) -> bool:
    try:
        hashed_password = get_admin_password_hash(db)
        return verify_password(credentials.password, hashed_password)
    except HTTPException:
        return False


def verify_admin_password(password: str, db: Session) -> bool:
    try:
        hashed_password = get_admin_password_hash(db)
        return verify_password(password, hashed_password)
    except HTTPException:
        return False
