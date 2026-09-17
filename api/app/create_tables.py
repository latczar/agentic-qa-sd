from shared.db import engine
from shared.models import Base


def main() -> None:
    Base.metadata.create_all(engine)
    print("tables created (or already existed)")


if __name__ == "__main__":
    main()
