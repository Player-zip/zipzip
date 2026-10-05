from __future__ import annotations

"""Compatibility layer for Solana RPC providers that reject batched jsonParsed accounts.

The discovery engine prefers getMultipleAccounts for efficiency. Some Solana RPC
providers/endpoints accept getTokenLargestAccounts but reject getMultipleAccounts
with jsonParsed. In that case we transparently fan out to getAccountInfo for each
token account and synthesize the same response shape expected by the engine.

Once a router instance proves that the batched jsonParsed method is unsupported,
we remember that capability for the rest of the cycle and skip the doomed batch
request. This removes repeated provider errors and wasted QuickNode credits.
"""

from typing import Any

from . import quicknode_solana as _core


_ORIGINAL_CALL = _core.SolanaRpcRouter.call
_PATCHED = False


def _fanout_accounts(self, params, provider=None, original_error=None):
    if not isinstance(params, list) or not params or not isinstance(params[0], list):
        return None, provider, original_error or "INVALID_MULTIPLE_ACCOUNTS_PARAMS"

    addresses = [str(x or "").strip() for x in params[0] if str(x or "").strip()]
    if not addresses:
        return {"value": []}, provider, None

    config = params[1] if len(params) > 1 and isinstance(params[1], dict) else {
        "encoding": "jsonParsed",
        "commitment": "confirmed",
    }
    values: list[Any | None] = []
    last_provider = provider
    successes = 0
    for address in addresses:
        account_result, account_provider, account_error = _ORIGINAL_CALL(
            self,
            "getAccountInfo",
            [address, config],
        )
        if account_provider:
            last_provider = account_provider
        value = account_result.get("value") if isinstance(account_result, dict) else None
        values.append(value)
        if account_error is None and isinstance(value, dict):
            successes += 1

    if successes:
        return {"value": values}, last_provider, None
    return None, last_provider, original_error or "ACCOUNT_INFO_FANOUT_FAILED"


def _compat_call(self, method: str, params: list | dict | None = None) -> tuple[Any | None, str | None, str | None]:
    if method != "getMultipleAccounts":
        return _ORIGINAL_CALL(self, method, params)

    capability = getattr(self, "_compat_get_multiple_accounts_jsonparsed", None)
    if capability is False:
        return _fanout_accounts(self, params)

    result, provider, error = _ORIGINAL_CALL(self, method, params)
    if error is None:
        setattr(self, "_compat_get_multiple_accounts_jsonparsed", True)
        return result, provider, None

    setattr(self, "_compat_get_multiple_accounts_jsonparsed", False)
    return _fanout_accounts(self, params, provider=provider, original_error=error)


def _install_patch() -> None:
    global _PATCHED
    if _PATCHED:
        return
    _core.SolanaRpcRouter.call = _compat_call
    _PATCHED = True


_install_patch()

run_quicknode_solana_discovery = _core.run_quicknode_solana_discovery
merge_quicknode_with_birdeye = _core.merge_quicknode_with_birdeye
QuickNodeCreditBudget = _core.QuickNodeCreditBudget
SolanaRpcRouter = _core.SolanaRpcRouter
