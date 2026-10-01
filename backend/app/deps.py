from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .database import get_db
from .i18n import get_lang, msg
from .models import User
from .security import decode_access_token

bearer = HTTPBearer(auto_error=False)


def get_optional_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User | None:
    if not creds:
        return None
    decoded = decode_access_token(creds.credentials)
    if not decoded:
        return None
    user_id, version = decoded
    user = db.get(User, user_id)
    # Depois de "sair de todos os dispositivos" ou de trocar a senha, os tokens antigos deixam de valer.
    if not user or user.token_version != version:
        return None
    return user


def get_current_user(user: User | None = Depends(get_optional_user), lang: str = Depends(get_lang)) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=msg("session_expired", lang),
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
