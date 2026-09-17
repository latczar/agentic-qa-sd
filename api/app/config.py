import os

# Falls back to the same credentials as .env.example so tests and local runs
# outside Docker work without extra setup. docker-compose.yml always overrides
# this with the real value.
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://sd_user:sd_password@localhost:5432/service_desk",
)
