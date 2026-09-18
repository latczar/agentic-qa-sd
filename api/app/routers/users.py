from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.schemas import UserOut
from shared.db import get_db
from shared.models import User

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)) -> list[User]:
    return list(db.query(User).order_by(User.name).all())


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: Session = Depends(get_db)) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user
