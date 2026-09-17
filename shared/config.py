import os

# Falls back to the same credentials as .env.example so tests and local runs
# outside Docker work without extra setup. docker-compose.yml always overrides
# these with the real values.
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://sd_user:sd_password@localhost:5433/service_desk",
)

RABBITMQ_URL = os.environ.get(
    "RABBITMQ_URL",
    "amqp://sd_user:sd_password@localhost:5672/",
)

# Ollama runs on the host, not in Compose - it needs GPU access and is shared
# with other projects on this machine. Containers reach it through the special
# host.docker.internal name, set in docker-compose.yml.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "60"))

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")

# "ollama" for real embeddings, "fake" for deterministic ones that need no model
# running. Tests set this to "fake"; CI has no Ollama at all.
EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "ollama")
