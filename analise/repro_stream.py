# Reprodução dos achados de ANALISE_PROBLEMAS.md.
# Rodar a partir da raiz de peixao-hunter-main, com as dependências instaladas.
import sys, tempfile, threading, time, json
from pathlib import Path
sys.path.insert(0, "src")
import peixao.robinhood_stream as rs
import peixao.robinhood_chainstack as rc

tmp = Path(tempfile.mkdtemp()); state = tmp / "s.json"; out = tmp / "c.csv"
T1, T2 = "0x" + "1"*40, "0x" + "2"*40
W = "0x" + "a"*40
def ev(tok, i): return {"token": tok, "from": W, "to": "0x" + f"{i:040x}", "transaction_hash": f"0x{i}", "log_index": i, "block_number": i}
rs.ingest_stream_payload({"events": [ev(T1, 1), ev(T2, 2)]}, state)

print("== A) Erro de RPC (429 persistente) no eth_getCode => wallet marcada como NÃO-EOA e cacheada 24h")
class R:
    status_code = 429
    def json(self): return {}
    def raise_for_status(self): pass
_real_sleep = rc.time.sleep; rc.time.sleep = lambda s: None  # acelera backoff
rc.requests.post = lambda *a, **k: R()
res = rs.materialize_stream_candidates(state, out, rpc_url="http://rpc", min_cross_token_hits=2)
w = json.loads(state.read_text())["wallets"][W]
print("  resultado:", {k: res[k] for k in ("wallets", "errors", "rpc_calls")}, "| is_eoa cacheado:", w.get("is_eoa"))

print("== B) materialize segura _STATE_LOCK durante chamadas de rede; webhook fica bloqueado")
import time as _t; _t.sleep = _real_sleep
w_state = json.loads(state.read_text()); w_state["wallets"][W].pop("eoa_checked_epoch", None); state.write_text(json.dumps(w_state))
rs._is_eoa_paced = lambda *a, **k: (_t.sleep(3), (True, 1))[1]   # RPC lento (3s)
th = threading.Thread(target=rs.materialize_stream_candidates, args=(state, out), kwargs=dict(rpc_url="http://rpc", min_cross_token_hits=2))
th.start(); _t.sleep(0.2)
t0 = _t.time(); rs.ingest_stream_payload({"events": [ev(T1, 3)]}, state); dt = _t.time() - t0
th.join()
print(f"  ingest (handler do webhook) esperou {dt:.1f}s pelo lock")
