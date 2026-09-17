from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.schemas import ServiceOut
from shared.db import get_db
from shared.models import Service

router = APIRouter(prefix="/services", tags=["services"])


@router.get("", response_model=list[ServiceOut])
def list_services(db: Session = Depends(get_db)) -> list[Service]:
    return list(db.query(Service).order_by(Service.name).all())
