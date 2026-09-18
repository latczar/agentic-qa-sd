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

GENERATION_MODEL = os.environ.get("GENERATION_MODEL", "qwen2.5:7b-instruct")

# Same "ollama" vs "fake" split as embeddings, same reason: tests and CI can't
# depend on a live model.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama")

# The worker reaches the MCP server over the Compose network. Streamable HTTP
# rather than stdio, because it is a separate container and not a child
# process of the worker.
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8080/mcp")

# "mcp" routes knowledge lookups through the MCP server's controlled tools;
# "direct" calls shared.retrieval in-process. Tests use "direct" so they do not
# need a running MCP server to exercise orchestration logic.
RETRIEVAL_MODE = os.environ.get("RETRIEVAL_MODE", "mcp")

# Where to POST when a ticket needs a human. Empty string disables it, which is
# what tests and CI use - there is no n8n there.
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "")
N8N_WEBHOOK_TIMEOUT_SECONDS = float(os.environ.get("N8N_WEBHOOK_TIMEOUT_SECONDS", "5"))
