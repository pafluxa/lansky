import os

# --- Database ---
SQLITE_DB_PATH: str = os.environ.get("SQLITE_DB_PATH", "/app/data/lansky.db")

# --- Claude model ---
MODEL: str = os.environ.get("MODEL", "anthropic:claude-sonnet-4-6")

# --- Graph engine thresholds ---
TAU_P: float = 0.7   # minimum purity for a partition to earn a label
N_MIN: int = 3       # minimum labeled nodes required in a partition

# --- Gaussian kernel sigmas ---
SIGMA_DATE: float = 4.0    # days (day-of-month periodicity)
SIGMA_TIME: float = 3.0    # hours (hour-of-day periodicity)
SIGMA_AMOUNT: float = 1.0  # log-scale units

# --- MCP Code Executor ---
MCP_CODE_EXECUTOR_URL: str = os.environ.get(
    "MCP_CODE_EXECUTOR_URL", "http://code-executor:3333/mcp"
)

# --- Extended thinking (off by default) ---
ENABLE_THINKING: bool = os.environ.get("ENABLE_THINKING", "false").lower() == "true"
THINKING_BUDGET_TOKENS: int = int(os.environ.get("THINKING_BUDGET_TOKENS", "5000"))
