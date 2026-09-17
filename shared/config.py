import os

# Falls back to the same credentials as .env.example so tests and local runs
# outside Docker work without extra setup. docker-compose.yml always overrides
# these with the real values.
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://sd_user:sd_password@localhost:5432/service_desk",
)

RABBITMQ_URL = os.environ.get(
    "RABBITMQ_URL",
    "amqp://sd_user:sd_password@localhost:5672/",
)
