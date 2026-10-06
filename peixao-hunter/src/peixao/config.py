from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
import os


def _path_env(name: str, default: str | None = None) -> Path | None:
    value = os.getenv(name, default)
    return Path(value).expanduser() if value else None


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _warn_invalid(name: str, value: str, default) -> None:
    # Config é carregada no import; um valor inválido não pode derrubar o
    # pacote inteiro. Avisa e usa o padrão.
    logging.getLogger("peixao.config").warning(
        "valor inválido para %s=%r; usando o padrão %r", name, value, default
    )


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return int(default)
    try:
        return int(float(value.strip()))
    except ValueError:
        _warn_invalid(name, value, default)
        return int(default)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return float(default)
    try:
        return float(value.strip())
    except ValueError:
        _warn_invalid(name, value, default)
        return float(default)


# ---------------------------------------------------------------------------
# Perfis de custo. "economy" (padrão) minimiza chamadas a APIs/RPCs pagos;
# "balanced" reproduz os volumes anteriores. Qualquer variável explícita no
# ambiente vence o perfil.
# ---------------------------------------------------------------------------
COST_PROFILES: dict[str, dict[str, float]] = {
    "economy": {
        "PEIXAO_NANSEN_WALLETS_PER_CHAIN": 5,
        "PEIXAO_NANSEN_TTL_SECONDS": 3 * 86400,
        "PEIXAO_NANSEN_RETRY_SECONDS": 3 * 86400,
        "PEIXAO_MONITOR_REFRESH_SECONDS": 7 * 86400,
        "PEIXAO_LEGACY_NANSEN_BATCH": 5,
        "PEIXAO_LEGACY_ZERION_BATCH": 10,
        "PEIXAO_COINSTATS_BATCH": 4,
        "PEIXAO_BIRDEYE_ALPHA_MAX_TOKENS": 3,
        "PEIXAO_BIRDEYE_TOP_TRADERS_PER_TOKEN": 20,
        "PEIXAO_BIRDEYE_MAX_PNL_WALLETS": 10,
        "PEIXAO_BIRDEYE_TOP_TRADER_TTL": 86400,
        "PEIXAO_BIRDEYE_PNL_TTL": 3 * 86400,
        "PEIXAO_QUICKNODE_DAILY_CREDITS": 60_000,
        "PEIXAO_QUICKNODE_RUN_CREDITS": 20_000,
        "PEIXAO_QUICKNODE_CACHE_TTL": 6 * 3600,
        "PEIXAO_QUICKNODE_FAST_CYCLE": 0,
        "PEIXAO_DUNE_SELECTIVITY_TTL": 3 * 86400,
        "PEIXAO_DUNE_SELECTIVITY_MAX_WALLETS": 10,
        "PEIXAO_BASE_WALLET_TTL": 6 * 3600,
        "PEIXAO_ROBINHOOD_PREFER_FREE_RPC": 1,
        "PEIXAO_PAID_MIN_PRIORITY": 45,
        "PEIXAO_DAILY_CAP_NANSEN": 250,
        "PEIXAO_DAILY_CAP_BIRDEYE": 200,
        "PEIXAO_DAILY_CAP_ZERION": 100,
        "PEIXAO_DAILY_CAP_COINSTATS": 60,
        "PEIXAO_DAILY_CAP_DUNE": 1,
        "PEIXAO_RPC_DAILY_LIMIT_ALCHEMY": 3000,
        "PEIXAO_RPC_DAILY_LIMIT_QUICKNODE_EVM": 5000,
        "PEIXAO_RPC_DAILY_LIMIT_DRPC": 20000,
        "PEIXAO_RPC_DAILY_LIMIT_ROBINHOOD_RPC": 50000,
    },
    "balanced": {
        "PEIXAO_NANSEN_WALLETS_PER_CHAIN": 20,
        "PEIXAO_NANSEN_TTL_SECONDS": 86400,
        "PEIXAO_NANSEN_RETRY_SECONDS": 6 * 3600,
        "PEIXAO_MONITOR_REFRESH_SECONDS": 86400,
        "PEIXAO_LEGACY_NANSEN_BATCH": 20,
        "PEIXAO_LEGACY_ZERION_BATCH": 40,
        "PEIXAO_COINSTATS_BATCH": 8,
        "PEIXAO_BIRDEYE_ALPHA_MAX_TOKENS": 5,
        "PEIXAO_BIRDEYE_TOP_TRADERS_PER_TOKEN": 30,
        "PEIXAO_BIRDEYE_MAX_PNL_WALLETS": 20,
        "PEIXAO_BIRDEYE_TOP_TRADER_TTL": 43200,
        "PEIXAO_BIRDEYE_PNL_TTL": 86400,
        "PEIXAO_QUICKNODE_DAILY_CREDITS": 330_000,
        "PEIXAO_QUICKNODE_RUN_CREDITS": 75_000,
        "PEIXAO_QUICKNODE_CACHE_TTL": 3600,
        "PEIXAO_QUICKNODE_FAST_CYCLE": 1,
        "PEIXAO_DUNE_SELECTIVITY_TTL": 86400,
        "PEIXAO_DUNE_SELECTIVITY_MAX_WALLETS": 20,
        "PEIXAO_BASE_WALLET_TTL": 1800,
        "PEIXAO_ROBINHOOD_PREFER_FREE_RPC": 0,
        "PEIXAO_PAID_MIN_PRIORITY": 0,
        "PEIXAO_DAILY_CAP_NANSEN": 0,
        "PEIXAO_DAILY_CAP_BIRDEYE": 0,
        "PEIXAO_DAILY_CAP_ZERION": 0,
        "PEIXAO_DAILY_CAP_COINSTATS": 0,
        "PEIXAO_DAILY_CAP_DUNE": 0,
        "PEIXAO_RPC_DAILY_LIMIT_ALCHEMY": 50000,
        "PEIXAO_RPC_DAILY_LIMIT_QUICKNODE_EVM": 50000,
        "PEIXAO_RPC_DAILY_LIMIT_DRPC": 50000,
        "PEIXAO_RPC_DAILY_LIMIT_ROBINHOOD_RPC": 50000,
    },
}


def cost_mode() -> str:
    mode = str(os.getenv("PEIXAO_COST_MODE", "economy") or "economy").strip().lower()
    return mode if mode in COST_PROFILES else "economy"


def cost_default(name: str, fallback: float = 0):
    """Padrão do perfil de custo ativo para ``name``."""
    return COST_PROFILES[cost_mode()].get(name, fallback)


def cost_int(name: str) -> int:
    """Valor efetivo: ambiente, senão o padrão do perfil de custo."""
    return _int_env(name, int(cost_default(name)))


def env_int(name: str, default: int) -> int:
    """Leitura tolerante de inteiros fora do ``Settings`` (mesma regra)."""
    return _int_env(name, default)


def env_float(name: str, default: float) -> float:
    return _float_env(name, default)


def env_bool(name: str, default: bool) -> bool:
    return _bool_env(name, default)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = _path_env("PEIXAO_DATA_DIR", "./data") or Path("./data")
    project_root: Path | None = _path_env("PEIXAO_PROJECT_ROOT")
    output_dir_override: Path | None = _path_env("PEIXAO_OUTPUT_DIR")
    v3_cache_dir_override: Path | None = _path_env("PEIXAO_V3_CACHE_DIR")
    v5_reference_dir_override: Path | None = _path_env("PEIXAO_V5_REFERENCE_DIR")
    gmgn_cache_override: Path | None = _path_env("PEIXAO_GMGN_CACHE")
    log_level: str = os.getenv("PEIXAO_LOG_LEVEL", "INFO")
    start_checkpoint: str = os.getenv("PEIXAO_START_CHECKPOINT", "entry")
    end_checkpoint: str = os.getenv("PEIXAO_END_CHECKPOINT", "p1h")
    min_tokens: int = _int_env("PEIXAO_MIN_TOKENS", 2)
    tx_per_token: int = _int_env("PEIXAO_TX_PER_TOKEN", 40)
    max_rpc_tx: int = _int_env("PEIXAO_MAX_RPC_TX", 1200)
    rpc_delay: float = _float_env("PEIXAO_RPC_DELAY", 0.08)
    rpc_timeout: float = _float_env("PEIXAO_RPC_TIMEOUT", 20)
    rpc_retries: int = _int_env("PEIXAO_RPC_RETRIES", 4)
    run_rpc: bool = _bool_env("PEIXAO_RUN_RPC", True)
    rpc_url: str = os.getenv("PEIXAO_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")

    rpc_budget_enabled: bool = _bool_env("PEIXAO_RPC_BUDGET_ENABLED", True)
    rpc_budget_run_total: int = _int_env("PEIXAO_RPC_BUDGET_RUN", 60)
    rpc_budget_hourly_total: int = _int_env("PEIXAO_RPC_BUDGET_HOURLY", 60)
    rpc_budget_daily_total: int = _int_env("PEIXAO_RPC_BUDGET_DAILY", 240)
    rpc_budget_radar: int = _int_env("PEIXAO_RPC_BUDGET_RADAR", 0)
    rpc_budget_tx_origin: int = _int_env("PEIXAO_RPC_BUDGET_TX_ORIGIN", 40)
    rpc_budget_classification: int = _int_env("PEIXAO_RPC_BUDGET_CLASSIFICATION", 15)
    rpc_budget_wallet_validation: int = _int_env("PEIXAO_RPC_BUDGET_WALLET_VALIDATION", 5)
    rpc_budget_deep_dive: int = _int_env("PEIXAO_RPC_BUDGET_DEEP_DIVE", 20)

    birdeye_api_key: str | None = os.getenv("BIRDEYE_API_KEY")
    nansen_api_key: str | None = os.getenv("NANSEN_API_KEY")
    zerion_api_key: str | None = os.getenv("ZERION_API_KEY")
    alchemy_api_key: str | None = os.getenv("ALCHEMY_API_KEY")
    blockscout_api_key: str | None = os.getenv("BLOCKSCOUT_API_KEY")
    etherscan_api_key: str | None = os.getenv("ETHERSCAN_API_KEY")
    helius_api_key: str | None = os.getenv("HELIUS_API_KEY")
    helius_rpc_url: str | None = os.getenv("HELIUS_RPC_URL")
    shyft_api_key: str | None = os.getenv("SHYFT_API_KEY")
    shyft_rpc_url: str | None = os.getenv("SHYFT_RPC_URL")
    mobula_api_key: str | None = os.getenv("MOBULA_API_KEY")
    jupiter_api_key: str | None = os.getenv("JUPITER_API_KEY")
    dune_api_key: str | None = os.getenv("DUNE_API_KEY")
    solana_tracker_api_key: str | None = os.getenv("SOLANA_TRACKER_API_KEY")
    telegram_bot_token: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_access_password: str | None = os.getenv("TELEGRAM_ACCESS_PASSWORD")
    telegram_chat_id: str | None = os.getenv("TELEGRAM_CHAT_ID")
    telegram_alerts_enabled: bool = _bool_env("PEIXAO_TELEGRAM_ALERTS", True)
    telegram_test_once: bool = _bool_env("PEIXAO_TELEGRAM_TEST_ONCE", False)
    telegram_timeout: float = _float_env("PEIXAO_TELEGRAM_TIMEOUT", 10)

    solana_tracker_base_url: str = os.getenv("PEIXAO_SOLANA_TRACKER_BASE_URL", "https://data.solanatracker.io")
    solana_tracker_probe_enabled: bool = _bool_env("PEIXAO_SOLANA_TRACKER_PROBE", True)
    solana_tracker_probe_ttl_seconds: int = _int_env("PEIXAO_SOLANA_TRACKER_PROBE_TTL", 86400)
    solana_tracker_timeout: float = _float_env("PEIXAO_SOLANA_TRACKER_TIMEOUT", 10)

    mobula_base_url: str = os.getenv("PEIXAO_MOBULA_BASE_URL", "https://api.mobula.io/api")
    mobula_probe_enabled: bool = _bool_env("PEIXAO_MOBULA_PROBE", False)
    mobula_probe_ttl_seconds: int = _int_env("PEIXAO_MOBULA_PROBE_TTL", 86400)
    mobula_timeout: float = _float_env("PEIXAO_MOBULA_TIMEOUT", 10)

    jupiter_base_url: str = os.getenv("PEIXAO_JUPITER_BASE_URL", "https://api.jup.ag")
    jupiter_probe_enabled: bool = _bool_env("PEIXAO_JUPITER_PROBE", True)
    jupiter_probe_ttl_seconds: int = _int_env("PEIXAO_JUPITER_PROBE_TTL", 86400)
    jupiter_timeout: float = _float_env("PEIXAO_JUPITER_TIMEOUT", 10)

    token_radar_enabled: bool = _bool_env("PEIXAO_TOKEN_RADAR", True)
    token_radar_ttl_seconds: int = _int_env("PEIXAO_TOKEN_RADAR_TTL", 1800)
    token_radar_max_candidates: int = _int_env("PEIXAO_TOKEN_RADAR_MAX_CANDIDATES", 50)
    token_radar_max_shortlist: int = _int_env("PEIXAO_TOKEN_RADAR_MAX_SHORTLIST", 5)
    token_radar_min_liquidity_usd: float = _float_env("PEIXAO_TOKEN_RADAR_MIN_LIQUIDITY_USD", 25000)
    token_radar_min_volume_24h_usd: float = _float_env("PEIXAO_TOKEN_RADAR_MIN_VOLUME_24H_USD", 20000)
    token_radar_min_score: float = _float_env("PEIXAO_TOKEN_RADAR_MIN_SCORE", 45)
    dexscreener_base_url: str = os.getenv("PEIXAO_DEXSCREENER_BASE_URL", "https://api.dexscreener.com")

    birdeye_base_url: str = os.getenv("PEIXAO_BIRDEYE_BASE_URL", "https://public-api.birdeye.so")
    birdeye_alpha_enabled: bool = _bool_env("PEIXAO_BIRDEYE_ALPHA", True)
    birdeye_alpha_max_tokens: int = cost_int("PEIXAO_BIRDEYE_ALPHA_MAX_TOKENS")
    birdeye_top_traders_per_token: int = cost_int("PEIXAO_BIRDEYE_TOP_TRADERS_PER_TOKEN")
    birdeye_min_cross_token_hits: int = _int_env("PEIXAO_BIRDEYE_MIN_CROSS_TOKEN_HITS", 2)
    birdeye_max_pnl_wallets: int = cost_int("PEIXAO_BIRDEYE_MAX_PNL_WALLETS")
    birdeye_top_trader_ttl_seconds: int = cost_int("PEIXAO_BIRDEYE_TOP_TRADER_TTL")
    birdeye_pnl_ttl_seconds: int = cost_int("PEIXAO_BIRDEYE_PNL_TTL")
    birdeye_alpha_delay: float = _float_env("PEIXAO_BIRDEYE_ALPHA_DELAY", 1.05)
    birdeye_alpha_timeout: float = _float_env("PEIXAO_BIRDEYE_ALPHA_TIMEOUT", 15)

    dune_base_url: str = os.getenv("PEIXAO_DUNE_BASE_URL", "https://api.dune.com")
    dune_probe_enabled: bool = _bool_env("PEIXAO_DUNE_PROBE", True)
    dune_probe_ttl_seconds: int = _int_env("PEIXAO_DUNE_PROBE_TTL", 86400)
    dune_timeout: float = _float_env("PEIXAO_DUNE_TIMEOUT", 10)
    dune_selectivity_enabled: bool = _bool_env("PEIXAO_DUNE_SELECTIVITY", True)
    dune_selectivity_ttl_seconds: int = cost_int("PEIXAO_DUNE_SELECTIVITY_TTL")
    dune_selectivity_max_wallets: int = cost_int("PEIXAO_DUNE_SELECTIVITY_MAX_WALLETS")
    dune_selectivity_lookback_days: int = _int_env("PEIXAO_DUNE_SELECTIVITY_LOOKBACK_DAYS", 30)
    dune_selectivity_poll_seconds: float = _float_env("PEIXAO_DUNE_SELECTIVITY_POLL_SECONDS", 60)
    # Idade máxima do cache Dune aplicado na tabela final (o Dune roda no ciclo de 6h).
    dune_selectivity_max_age_seconds: int = _int_env("PEIXAO_DUNE_SELECTIVITY_MAX_AGE", 7 * 86400)

    # Ledger de evidências: janela de frescor e retenção do histórico.
    evidence_max_age_days: float = _float_env("PEIXAO_EVIDENCE_MAX_AGE_DAYS", 30)
    evidence_retention_days: float = _float_env("PEIXAO_EVIDENCE_RETENTION_DAYS", 120)

    # Perfil de custo e redes ativas (rede desligada não gasta API paga).
    cost_mode: str = cost_mode()
    chains: tuple[str, ...] = tuple(
        c.strip().lower() for c in (os.getenv("PEIXAO_CHAINS") or "solana,base,robinhood").split(",") if c.strip()
    )
    nansen_wallets_per_chain: int = cost_int("PEIXAO_NANSEN_WALLETS_PER_CHAIN")
    nansen_ttl_seconds: int = cost_int("PEIXAO_NANSEN_TTL_SECONDS")
    nansen_retry_seconds: int = cost_int("PEIXAO_NANSEN_RETRY_SECONDS")
    monitor_refresh_seconds: int = cost_int("PEIXAO_MONITOR_REFRESH_SECONDS")
    paid_min_priority: float = _float_env("PEIXAO_PAID_MIN_PRIORITY", cost_default("PEIXAO_PAID_MIN_PRIORITY"))

    robinhood_rpc_url: str | None = os.getenv("ROBINHOOD_RPC_URL")
    quicknode_rpc_url: str | None = os.getenv("QUICKNODE_RPC_URL")
    quicknode_streams_api_key: str | None = os.getenv("QUICKNODE_STREAMS_API_KEY")
    robinhood_stream_webhook_url: str | None = os.getenv("PEIXAO_ROBINHOOD_STREAM_WEBHOOK_URL")
    coinstats_api_key: str | None = os.getenv("COINSTATS_API_KEY")

    # Login do bot: tentativas por chat antes do bloqueio, duração do bloqueio
    # e validade da autorização (0 = não expira; trocar a senha sempre revoga).
    telegram_auth_max_attempts: int = _int_env("PEIXAO_TELEGRAM_AUTH_MAX_ATTEMPTS", 5)
    telegram_auth_lockout_seconds: int = _int_env("PEIXAO_TELEGRAM_AUTH_LOCKOUT_SECONDS", 900)
    telegram_auth_ttl_days: float = _float_env("PEIXAO_TELEGRAM_AUTH_TTL_DAYS", 0)
    # Chats com poder de admin (convites/revogação); separados por vírgula.
    telegram_admin_chat_ids: tuple[str, ...] = tuple(
        x.strip() for x in (os.getenv("TELEGRAM_ADMIN_CHAT_IDS") or "").split(",") if x.strip()
    )
    # Desligue para aceitar só convites individuais (sem senha compartilhada).
    telegram_password_login: bool = _bool_env("PEIXAO_TELEGRAM_PASSWORD_LOGIN", True)

    gmgn_api_key: str | None = os.getenv("GMGN_API_KEY")
    gmgn_live_probe: bool = _bool_env("PEIXAO_GMGN_LIVE_PROBE", True)
    gmgn_demo_enabled: bool = _bool_env("PEIXAO_GMGN_DEMO_ENABLED", True)
    gmgn_chain: str = os.getenv("PEIXAO_GMGN_CHAIN", "robinhood")
    gmgn_live_timeout: int = _int_env("PEIXAO_GMGN_TIMEOUT", 45)

    @property
    def output_dir(self) -> Path:
        return self.output_dir_override or (self.data_dir / "output")

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def chain_enabled(self, chain: str) -> bool:
        return str(chain or "").strip().lower() in self.chains

    @property
    def master_db(self) -> Path:
        return self.data_dir / "peixao_master.sqlite3"

    @property
    def robinhood_eoa_rpc_url(self) -> str:
        """RPC para checagem de EOA do stream: ROBINHOOD_RPC_URL ou o RPC público."""
        return str(self.robinhood_rpc_url or self.rpc_url or "").strip()

    @property
    def rpc_stage_budgets(self) -> dict[str, int]:
        return {
            "radar": max(0, self.rpc_budget_radar),
            "tx_origin": max(0, self.rpc_budget_tx_origin),
            "classification": max(0, self.rpc_budget_classification),
            "wallet_validation": max(0, self.rpc_budget_wallet_validation),
            "deep_dive": max(0, self.rpc_budget_deep_dive),
        }

    @property
    def v3_cache_dir(self) -> Path | None:
        if self.v3_cache_dir_override:
            return self.v3_cache_dir_override
        if self.project_root:
            return self.project_root / "V3C_CACHE_NORMALIZED"
        return None

    @property
    def v5_reference_dir(self) -> Path | None:
        if self.v5_reference_dir_override:
            return self.v5_reference_dir_override
        if self.project_root:
            return self.project_root / "V5_WALLETS"
        return None

    @property
    def gmgn_cache(self) -> Path | None:
        if self.gmgn_cache_override:
            return self.gmgn_cache_override
        if self.project_root:
            return self.project_root / "V6_GMGN" / "V6_wallets_gmgn_summary.csv"
        return None

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def validate_legacy_inputs(self) -> None:
        if not self.project_root or not self.project_root.is_dir():
            raise FileNotFoundError(
                "PEIXAO_PROJECT_ROOT não configurado ou inexistente. "
                "Aponte para a raiz que contém V3C_CACHE_NORMALIZED e o JSON V3B."
            )
        cache = self.v3_cache_dir
        if not cache or not cache.is_dir():
            raise FileNotFoundError(f"V3C_CACHE_NORMALIZED não encontrado: {cache}")


settings = Settings()
