from __future__ import annotations
import html, json, sqlite3, urllib.error, urllib.parse, urllib.request
from pathlib import Path
import pandas as pd

from .evidence_ledger import infer_chain, normalize_address, wallet_key
from .score_outcomes import tier_track_record, track_record_text
from .telegram_auth import ALERT_PLANS, authorized_chat_ids

def _truthy(v):
    return v if isinstance(v, bool) else str(v).strip().lower() in {"1","true","yes","y"}

def _num(v, digits=2, suffix=""):
    try:
        if v is None or pd.isna(v): return "n/d"
        return ("{:,.%df}{}" % digits).format(float(v), suffix)
    except Exception: return "n/d"

def _wr(v):
    try:
        if v is None or pd.isna(v): return "n/d"
        x=float(v); x=x*100 if x<=1 else x
        return "{:.1f}%".format(x)
    except Exception: return "n/d"

def _message(r, track_record=None):
    address=str(r.get("address","")).strip()
    # Mesma regra de rede do /status: linha legada sem `chain` é Robinhood, não Solana.
    chain=infer_chain(r).title()
    coverage=float(r.get("selective_evidence_coverage",0) or 0)*100
    return (
        "🐟 <b>RAULLUX ALPHA — WALLET ALPHA ENCONTRADA</b>\n\n"
        "🔵 <b>Rede:</b> {}\n🏆 <b>Tier:</b> {}\n🎯 <b>Peixão Alpha Score:</b> {}/100\n"
        "🧠 <b>Selective Score:</b> {}/100\n📈 <b>Win Rate:</b> {}\n💰 <b>PnL 30d:</b> ${}\n"
        "🗓️ <b>Novas posições/semana:</b> {}\n📊 <b>Cobertura de evidência:</b> {}\n"
        "🧬 <b>Perfil:</b> {}\n\n<code>{}</code>\n\n"
        "✅ Passou naturalmente pelo gate final Selective Alpha.{}"
    ).format(html.escape(chain),html.escape(str(r.get("selective_alpha_tier","ALPHA"))),
      _num(r.get("selective_alpha_score")),_num(r.get("selective_score")),
      _wr(r.get("selective_win_rate",r.get("gmgn_winrate_30d"))),_num(r.get("realized_profit_30d")),
      _num(r.get("selective_new_positions_per_week")),_num(coverage,0,"%"),
      html.escape(str(r.get("selective_profile","UNKNOWN"))),html.escape(address),
      ("\n" + html.escape(track_record)) if track_record else "")

def _send(token, chat_id, text, timeout):
    url="https://api.telegram.org/bot{}/sendMessage".format(token)
    payload=urllib.parse.urlencode({"chat_id":chat_id,"text":text,"parse_mode":"HTML","disable_web_page_preview":"true"}).encode()
    req=urllib.request.Request(url,data=payload,method="POST")
    try:
        with urllib.request.urlopen(req,timeout=timeout) as resp:
            body=json.loads(resp.read().decode())
            return {"ok":bool(body.get("ok")),"http_status":int(resp.status)}
    except urllib.error.HTTPError as exc:
        return {"ok":False,"http_status":int(exc.code),"error":"TELEGRAM_HTTP_ERROR"}
    except Exception as exc:
        return {"ok":False,"http_status":None,"error":type(exc).__name__}

def _recipients(conn, access_password=None, auth_ttl_days=0):
    """Só sessões válidas e de planos que recebem alertas automáticos."""
    return authorized_chat_ids(conn, access_password, ttl_days=auth_ttl_days, plans=ALERT_PLANS)

def notify_alpha_wallets(stage1_path:Path, db_path:Path, *, token, chat_id=None, enabled=True, timeout=10.0,
                         access_password=None, auth_ttl_days=0):
    if not enabled: return {"status":"DISABLED","sent":0}
    if not token: return {"status":"NOT_CONFIGURED","sent":0}
    if not stage1_path.is_file(): return {"status":"NO_STAGE1_FILE","sent":0}
    try: frame=pd.read_csv(stage1_path)
    except pd.errors.EmptyDataError: frame=pd.DataFrame()
    if frame.empty or "address" not in frame or "selective_deep_dive_candidate" not in frame or "selective_alpha_score" not in frame:
        return {"status":"DONE","eligible":0,"sent":0,"recipients":0,"duplicates_skipped":0}
    scores=pd.to_numeric(frame["selective_alpha_score"],errors="coerce")
    alpha_gate=frame["selective_deep_dive_candidate"].map(_truthy) & scores.ge(70.0)
    eligible=frame[alpha_gate].copy()
    if eligible.empty: return {"status":"DONE","eligible":0,"sent":0,"recipients":0,"duplicates_skipped":0}
    db_path.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("""CREATE TABLE IF NOT EXISTS telegram_alpha_deliveries (
          address TEXT NOT NULL, score_version TEXT NOT NULL, chat_id TEXT NOT NULL,
          sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_tier TEXT, last_score REAL,
          PRIMARY KEY(address,score_version,chat_id))""")
        recipients=_recipients(conn, access_password, auth_ttl_days)
        sent=skipped=errors=0
        for _,r in eligible.iterrows():
            raw_address=str(r.get("address","")).strip()
            if not raw_address: continue
            chain=infer_chain(r)
            address=normalize_address(chain, raw_address)
            # Chave de entrega por chain:address (a mesma wallet EVM em duas redes
            # gera dois alertas). Entregas antigas foram gravadas só com o endereço.
            delivery_key=wallet_key(chain, address)
            legacy_keys=(delivery_key, address, raw_address)
            version=str(r.get("selective_score_version","V2.2S1"))
            score=r.get("selective_alpha_score")
            tier=str(r.get("selective_alpha_tier","") or "")
            # Histórico real do tier (backtest 30d) junto do alerta.
            track=track_record_text(tier_track_record(stage1_path.parent, tier), tier) if tier else None
            for recipient in recipients:
                if conn.execute(
                    "SELECT 1 FROM telegram_alpha_deliveries WHERE address IN (?,?,?) AND score_version=? AND chat_id=?",
                    (*legacy_keys, version, recipient),
                ).fetchone():
                    skipped+=1; continue
                result=_send(token,recipient,_message(r, track),timeout)
                if result.get("ok"):
                    conn.execute("INSERT OR REPLACE INTO telegram_alpha_deliveries(address,score_version,chat_id,last_tier,last_score) VALUES(?,?,?,?,?)",
                      (delivery_key,version,recipient,str(r.get("selective_alpha_tier","")),None if pd.isna(score) else float(score)))
                    conn.commit(); sent+=1
                else: errors+=1
        return {"status":"DONE" if errors==0 else "PARTIAL","eligible":int(len(eligible)),
          "recipients":len(recipients),"sent":sent,"duplicates_skipped":skipped,"errors":errors}
    finally: conn.close()

def send_test_alert(*, token, chat_id, timeout=10.0):
    if not token or not chat_id: return {"status":"NOT_CONFIGURED","sent":0}
    text="🐟 <b>RAULLUX ALPHA BOT — TESTE OK</b>\n\n✅ Telegram conectado ao Peixão Hunter.\n🔒 Este é apenas um teste de infraestrutura.\n🎯 Alertas reais só serão enviados quando uma wallet passar o gate final Selective Alpha."
    result=_send(token,chat_id,text,timeout)
    return {"status":"DONE" if result.get("ok") else "ERROR","sent":1 if result.get("ok") else 0,**result}


def send_simulation_alert(*, token, db_path: Path, address, chain="Base", label=None,
                          pnl=None, pnl_pct=None, win_rate=None, buys=None, sells=None,
                          buy_volume=None, sell_volume=None, profile=None,
                          alpha_score=None, tier=None, positions_per_week=None,
                          timeout=10.0, access_password=None, auth_ttl_days=0):
    """Send a marked, data-rich simulation to all authorized Telegram recipients."""
    if not token:
        return {"status":"NOT_CONFIGURED","sent":0,"recipients":0}
    address=str(address or "").strip()
    if not address:
        return {"status":"INVALID_ADDRESS","sent":0,"recipients":0}
    if not db_path.is_file():
        return {"status":"NO_AUTH_DB","sent":0,"recipients":0}
    conn=sqlite3.connect(db_path, timeout=30)
    try:
        recipients=_recipients(conn, access_password, auth_ttl_days)
    finally:
        conn.close()

    def shown(v, fallback="a calcular pelo nosso pipeline"):
        return html.escape(str(v)) if v not in (None, "") else fallback

    text=(
        "🧪 <b>RAULLUX ALPHA — SIMULAÇÃO DE TRACKER</b>\n\n"
        "🔵 <b>Rede principal:</b> {}\n"
        "🐋 <b>Wallet:</b> <code>{}</code>\n"
        "🏷️ <b>Label:</b> {}\n"
        "💰 <b>PnL observado:</b> {} ({})\n"
        "🎯 <b>Win Rate observado:</b> {}\n"
        "📊 <b>Compras/Vendas:</b> {} / {}\n"
        "💵 <b>Volume comprado:</b> {}\n"
        "💵 <b>Volume vendido:</b> {}\n"
        "🔬 <b>Perfil observado:</b> {}\n\n"
        "🧠 <b>Peixão Selective Alpha</b>\n"
        "🏆 <b>Tier:</b> {}\n"
        "🎯 <b>Score:</b> {}\n"
        "🗓️ <b>Posições por semana:</b> {}\n\n"
        "🧪 <b>SIMULAÇÃO</b> — esta wallet não passou pelo gate Alpha do radar."
    ).format(
        html.escape(str(chain).title()), html.escape(address), shown(label, "n/d"),
        shown(pnl, "n/d"), shown(pnl_pct, "n/d"), shown(win_rate, "n/d"),
        shown(buys, "n/d"), shown(sells, "n/d"), shown(buy_volume, "n/d"),
        shown(sell_volume, "n/d"), shown(profile, "n/d"), shown(tier),
        shown(alpha_score), shown(positions_per_week)
    )
    sent=errors=0
    for recipient in recipients:
        result=_send(token,recipient,text,timeout)
        if result.get("ok"): sent+=1
        else: errors+=1
    return {"status":"DONE" if errors==0 else "PARTIAL","sent":sent,"recipients":len(recipients),"errors":errors}


def send_test_alert_once(state_dir: Path, *, token, chat_id, enabled=False, timeout=10.0):
    if not enabled:
        return {"status":"DISABLED","sent":0}
    marker=state_dir / "telegram_test_sent.json"
    if marker.is_file():
        return {"status":"ALREADY_SENT","sent":0}
    result=send_test_alert(token=token,chat_id=chat_id,timeout=timeout)
    if result.get("status")=="DONE":
        state_dir.mkdir(parents=True,exist_ok=True)
        marker.write_text(json.dumps({"sent":True}),encoding="utf-8")
    return result
