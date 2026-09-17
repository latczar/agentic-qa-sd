from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Service
from app.schemas import ServiceOut

router = APIRouter(prefix="/services", tags=["services"])


@router.get("", response_model=list[ServiceOut])
def list_services(db: Session = Depends(get_db)) -> list[Service]:
    return list(db.query(Service).order_by(Service.name).all())
