"""Compatibilidade de importação.

O fallback getMultipleAccounts -> getAccountInfo agora vive em
``quicknode_solana.SolanaRpcRouter.call``; este módulo não altera mais nada em
tempo de importação.
"""
from __future__ import annotations

from .quicknode_solana import (
    QuickNodeCreditBudget,
    SolanaRpcRouter,
    merge_quicknode_with_birdeye,
    run_quicknode_solana_discovery,
)

__all__ = [
    "QuickNodeCreditBudget",
    "SolanaRpcRouter",
    "merge_quicknode_with_birdeye",
    "run_quicknode_solana_discovery",
]
