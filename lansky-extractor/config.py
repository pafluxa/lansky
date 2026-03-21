import os

IMAP_HOST: str = os.environ.get("IMAP_HOST", "imap.mail.me.com")
IMAP_USER: str = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD: str = os.environ.get("IMAP_PASSWORD", "")
IMAP_FOLDER: str = os.environ.get("IMAP_FOLDER", "INBOX")
BANK_SENDERS: list[str] = [
    s.strip() for s in
    os.environ.get("BANK_SENDERS", "").split(",")
    if s.strip()
]

LLM_BASE_URL: str = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8081/v1")
LLM_MODEL: str = os.environ.get("LLM_MODEL", "qwen3-8b")

LANSKY_API_URL: str = os.environ.get("LANSKY_API_URL", "http://127.0.0.1:8000")

POLL_INTERVAL_SECONDS: int = int(os.environ.get("POLL_INTERVAL_SECONDS", "900"))

LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
