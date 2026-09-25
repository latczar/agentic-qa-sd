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


# --- Telegram -------------------------------------------------------------
#
# The bot long-polls Telegram rather than receiving webhooks, so none of this
# needs a public URL. See shared/telegram.py for why.

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
TELEGRAM_TIMEOUT_SECONDS = float(os.environ.get("TELEGRAM_TIMEOUT_SECONDS", "10"))

# "http" talks to real Telegram, "fake" records messages in memory. Tests and
# CI use "fake" - same split as LLM_PROVIDER and EMBEDDING_PROVIDER.
TELEGRAM_PROVIDER = os.environ.get("TELEGRAM_PROVIDER", "http" if TELEGRAM_BOT_TOKEN else "fake")

# How long each getUpdates call waits for something to happen. Telegram holds
# the connection open for this long rather than returning an empty list
# immediately, which is what makes long polling cheap instead of a busy loop.
TELEGRAM_POLL_SECONDS = int(os.environ.get("TELEGRAM_POLL_SECONDS", "25"))

# Where the bot sends approval requests and finds tickets to act on. The bot
# goes through the API exactly like n8n does, never straight to Postgres, so
# the status guard, the approvals record and the audit trail all apply to a
# decision made from a phone as much as one made in the browser.
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")


def _parse_chat_ids(raw: str) -> frozenset[int]:
    """Parse the allowlist, refusing to start on anything malformed.

    This is the only thing standing between "a stranger found my bot" and
    "a stranger can approve tickets", so a typo must not silently shrink it.
    Dropping an unparseable entry and carrying on would turn a fat-fingered
    chat id into a quietly reduced allowlist, which is the failure mode worth
    avoiding here.
    """
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise ValueError(
                f"TELEGRAM_ALLOWED_CHAT_IDS contains {part!r}, which is not a chat id. "
                "Expected a comma-separated list of numbers."
            ) from exc
    return frozenset(ids)


# Empty means nobody, not everybody. Anyone can find a bot by its username and
# start messaging it, so an empty allowlist has to fail closed - the
# alternative is a bot that resolves tickets for whoever types at it first.
TELEGRAM_ALLOWED_CHAT_IDS = _parse_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
