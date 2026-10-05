"""Peixão Hunter."""

# Install the Solana RPC compatibility shim before runner modules are imported.
# This keeps QuickNode as the primary provider while transparently falling back
# from getMultipleAccounts(jsonParsed) to per-account getAccountInfo when needed.
from . import quicknode_solana_compat as _quicknode_solana_compat  # noqa: F401

# Extend /status with provider efficiency, adaptive queue and replay metrics
# without replacing the proven Telegram authentication/command handler.
from . import telegram_status_patch as _telegram_status_patch  # noqa: F401

from .multichain_runner import run, run_v6

__all__ = ["run", "run_v6"]
