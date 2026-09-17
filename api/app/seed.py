"""Synthetic demo data — no real people, no real company services."""

from shared.db import SessionLocal
from shared.models import Service, ServiceStatus, User, UserRole

SEED_USERS = [
    {"name": "Jamie Whitfield", "email": "jamie.whitfield@example.com", "role": UserRole.END_USER},
    {"name": "Priya Nandakumar", "email": "priya.nandakumar@example.com", "role": UserRole.AGENT},
    {"name": "Sam O'Connor", "email": "sam.oconnor@example.com", "role": UserRole.ADMIN},
]

SEED_SERVICES = [
    {"name": "Identity Service", "description": "Authentication, SSO, password reset", "status": ServiceStatus.OPERATIONAL},
    {"name": "Email Service", "description": "Corporate email and calendaring", "status": ServiceStatus.OPERATIONAL},
    {"name": "VPN Service", "description": "Remote access VPN gateway", "status": ServiceStatus.OPERATIONAL},
    {"name": "Payroll System", "description": "Payroll and expenses", "status": ServiceStatus.OPERATIONAL},
]


def main() -> None:
    db = SessionLocal()
    try:
        for row in SEED_USERS:
            if not db.query(User).filter_by(email=row["email"]).first():
                db.add(User(**row))
        for row in SEED_SERVICES:
            if not db.query(Service).filter_by(name=row["name"]).first():
                db.add(Service(**row))
        db.commit()
        print("seed data inserted (or already present)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
