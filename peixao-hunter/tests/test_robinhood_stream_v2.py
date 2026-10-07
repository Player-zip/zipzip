from peixao import robinhood_stream as legacy
from peixao import robinhood_stream_v2 as rs2


def test_robinhood_stream_uses_block_with_receipts_dataset():
    assert rs2.STREAM_DATASET == "block_with_receipts"
    assert legacy.STREAM_DATASET == "block_with_receipts"


def test_filter_reads_transfer_logs_from_receipts():
    token = "0x1111111111111111111111111111111111111111"
    code = rs2._filter_code([token])
    assert token in code
    assert rs2.TRANSFER_TOPIC in code
    assert "row.receipts" in code
    assert "receipt.logs" in code
    assert "return null" in code
