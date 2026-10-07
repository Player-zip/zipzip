"""Isola os testes do ambiente real.

``Settings`` lê o ambiente na importação, então tudo aqui roda antes de
qualquer ``import peixao``:

- diretório de dados temporário;
- nenhuma chave/URL de provedor pago (um dev com chave exportada não pode
  disparar chamadas reais rodando os testes);
- perfil de custo e redes nos padrões.
"""
import os
import tempfile

os.environ["PEIXAO_DATA_DIR"] = tempfile.mkdtemp(prefix="peixao-tests-")
# Nenhum teste de ciclo pode sair para a rede (explorer/preço); os testes on-chain injetam um cliente falso.
os.environ["PEIXAO_ONCHAIN_EVIDENCE"] = "0"
for _name in (
    "BIRDEYE_API_KEY", "NANSEN_API_KEY", "ZERION_API_KEY", "ALCHEMY_API_KEY", "COINSTATS_API_KEY",
    "DUNE_API_KEY", "JUPITER_API_KEY", "MOBULA_API_KEY", "SOLANA_TRACKER_API_KEY", "GMGN_API_KEY",
    "HELIUS_API_KEY", "HELIUS_RPC_URL", "SHYFT_API_KEY", "SHYFT_RPC_URL", "QUICKNODE_RPC_URL",
    "QUICKNODE_STREAMS_API_KEY", "QUICKNODE_STREAM_SECURITY_TOKEN", "ROBINHOOD_RPC_URL",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_ACCESS_PASSWORD", "TELEGRAM_CHAT_ID", "TELEGRAM_ADMIN_CHAT_IDS",
    "PEIXAO_COST_MODE", "PEIXAO_CHAINS",
):
    os.environ.pop(_name, None)
