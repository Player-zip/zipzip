"""Compatibilidade de importação.

O comportamento do Stream (dataset block_with_receipts, filtro de receipts,
opções do plano gratuito e diagnóstico de erro da API) foi incorporado em
``robinhood_stream``; este módulo não altera mais nada em tempo de importação.
"""
from __future__ import annotations

from .robinhood_stream import (
    STREAM_DATASET,
    STREAM_NETWORK,
    TRANSFER_TOPIC,
    _filter_code,
    extract_stream_events,
    ingest_stream_payload,
    materialize_stream_candidates,
    run_robinhood_stream_cycle,
    serve_stream_receiver,
    stream_needs_materialization,
    sync_robinhood_stream,
    verify_stream_signature,
)

__all__ = [
    "STREAM_DATASET",
    "STREAM_NETWORK",
    "TRANSFER_TOPIC",
    "_filter_code",
    "extract_stream_events",
    "ingest_stream_payload",
    "materialize_stream_candidates",
    "run_robinhood_stream_cycle",
    "serve_stream_receiver",
    "stream_needs_materialization",
    "sync_robinhood_stream",
    "verify_stream_signature",
]
