from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


DEFAULT_SYMBOLS = (
    "BTC_USDT",
    "ETH_USDT",
    "SOL_USDT",
    "XRP_USDT",
    "BNB_USDT",
    "DOGE_USDT",
    "ADA_USDT",
    "AVAX_USDT",
    "LINK_USDT",
    "LTC_USDT",
    "DOT_USDT", "ATOM_USDT", "NEAR_USDT", "UNI_USDT", "AAVE_USDT",
    "TRX_USDT", "XLM_USDT", "ETC_USDT", "BCH_USDT", "FIL_USDT",
    "ICP_USDT", "APT_USDT", "ARB_USDT", "OP_USDT", "SUI_USDT",
    "INJ_USDT", "RUNE_USDT", "SEI_USDT", "TON_USDT", "HBAR_USDT",
    "SHIB_USDT", "PEPE_USDT", "WIF_USDT", "BONK_USDT", "FLOKI_USDT",
    "POL_USDT", "ALGO_USDT", "VET_USDT", "EOS_USDT", "THETA_USDT",
    "GRT_USDT", "MKR_USDT", "SNX_USDT", "CRV_USDT", "COMP_USDT",
    "LDO_USDT", "SAND_USDT", "MANA_USDT", "AXS_USDT", "ENJ_USDT",
    "CHZ_USDT", "IMX_USDT", "STX_USDT", "TIA_USDT", "JUP_USDT",
    "JTO_USDT", "PYTH_USDT", "STRK_USDT", "ZK_USDT", "EIGEN_USDT",
    "ENA_USDT", "ONDO_USDT", "TAO_USDT", "RENDER_USDT", "FET_USDT",
    "KAS_USDT", "XMR_USDT", "ZEC_USDT", "DASH_USDT", "QNT_USDT",
    "KSM_USDT", "MINA_USDT", "ROSE_USDT", "CELO_USDT", "1INCH_USDT",
    "DYDX_USDT", "GMX_USDT", "CAKE_USDT", "SUSHI_USDT", "YFI_USDT",
    "AR_USDT", "ASTR_USDT", "KAVA_USDT", "EGLD_USDT", "FLOW_USDT",
    "IOTA_USDT", "NEO_USDT", "QTUM_USDT", "ZIL_USDT", "IOTX_USDT",
    "MASK_USDT", "BLUR_USDT", "MAGIC_USDT", "LPT_USDT", "METIS_USDT",
    "APE_USDT", "GMT_USDT", "RAY_USDT", "ORDI_USDT", "WLD_USDT",
    "JASMY_USDT", "GALA_USDT", "ENS_USDT", "OSMO_USDT", "HNT_USDT",
    "XTZ_USDT", "KDA_USDT", "ANKR_USDT", "ONE_USDT", "ONT_USDT",
    "BAND_USDT", "API3_USDT", "LUNC_USDT", "USTC_USDT", "RON_USDT",
)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw in (None, "") else float(raw)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw in (None, "") else int(raw)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    symbols: tuple[str, ...]
    data_mode: str
    db_path: Path
    paper_balance: float
    maker_fee_rate: float
    taker_fee_rate: float
    min_slippage_bps: float
    impact_slippage_bps: float
    base_risk_pct: float
    turbo_risk_pct: float
    control_risk_pct: float
    baseline_risk_pct: float
    module_risk_pct: float
    reversal_risk_pct: float
    ensemble_risk_pct: float
    composite_low_risk_pct: float
    composite_base_risk_pct: float
    composite_high_risk_pct: float
    base_max_leverage: float
    turbo_max_leverage: float
    max_margin_utilization: float
    max_portfolio_risk_pct: float
    max_open_positions: int
    daily_stop_pct: float
    monthly_stop_pct: float
    risk_reduction_drawdown_pct: float
    signal_threshold: float
    high_conviction_threshold: float
    startup_warmup_seconds: int
    regime_confirm_seconds: int
    min_ready_ratio: float
    max_entry_spread_bps: float
    evaluation_interval_seconds: float
    feature_persist_seconds: int
    account_persist_seconds: int
    cooldown_seconds: int
    max_holding_minutes: int
    min_flow_exit_minutes: int
    flow_exit_confirm_seconds: int
    stale_after_seconds: int
    bybit_depth: int
    enable_binance: bool
    dashboard_token: str
    log_level: str

    high_leverage_lab: bool = True
    liquidation_min_notional: float = 50000.0
    liquidation_min_events: int = 5
    liquidation_min_volume_ratio: float = 0.005
    reversal_interval_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> "Settings":
        symbols_raw = os.getenv("SYMBOLS", ",".join(DEFAULT_SYMBOLS))
        symbols = tuple(
            item.strip().upper().replace("/", "_")
            for item in symbols_raw.split(",")
            if item.strip()
        )
        if not symbols:
            raise ValueError("SYMBOLS must contain at least one contract")

        data_mode = os.getenv("DATA_MODE", "live").strip().lower()
        if data_mode not in {"live", "synthetic"}:
            raise ValueError("DATA_MODE must be 'live' or 'synthetic'")

        db_path = Path(os.getenv("DB_PATH", "data/paper_v040.db")).expanduser()
        return cls(
            high_leverage_lab=_bool("HIGH_LEVERAGE_LAB", True),
            liquidation_min_notional=_float("LIQUIDATION_MIN_NOTIONAL", 50000.0),
            liquidation_min_events=_int("LIQUIDATION_MIN_EVENTS", 5),
            liquidation_min_volume_ratio=_float("LIQUIDATION_MIN_VOLUME_RATIO", 0.005),
            reversal_interval_seconds=_float("REVERSAL_INTERVAL_SECONDS", 15.0),
            symbols=symbols,
            data_mode=data_mode,
            db_path=db_path,
            paper_balance=_float("PAPER_BALANCE", 1000.0),
            # MEXC standard API Futures rates announced for 2026-06-01.
            # Both remain configurable because actual account rates may differ.
            maker_fee_rate=_float("MAKER_FEE_RATE", 0.0006),
            taker_fee_rate=_float("TAKER_FEE_RATE", 0.0008),
            min_slippage_bps=_float("MIN_SLIPPAGE_BPS", 0.5),
            impact_slippage_bps=_float("IMPACT_SLIPPAGE_BPS", 8.0),
            base_risk_pct=_float("BASE_RISK_PCT", 1.5),
            turbo_risk_pct=_float("TURBO_RISK_PCT", 3.0),
            control_risk_pct=_float("CONTROL_RISK_PCT", 2.0),
            baseline_risk_pct=_float("BASELINE_RISK_PCT", 0.25),
            module_risk_pct=_float("MODULE_RISK_PCT", 0.50),
            reversal_risk_pct=_float("REVERSAL_RISK_PCT", 0.35),
            ensemble_risk_pct=_float("ENSEMBLE_RISK_PCT", 0.50),
            composite_low_risk_pct=_float("COMPOSITE_LOW_RISK_PCT", 0.35),
            composite_base_risk_pct=_float("COMPOSITE_BASE_RISK_PCT", 0.50),
            composite_high_risk_pct=_float("COMPOSITE_HIGH_RISK_PCT", 0.60),
            base_max_leverage=_float("BASE_MAX_LEVERAGE", 5.0),
            turbo_max_leverage=_float("TURBO_MAX_LEVERAGE", 8.0),
            max_margin_utilization=_float("MAX_MARGIN_UTILIZATION", 0.75),
            max_portfolio_risk_pct=_float("MAX_PORTFOLIO_RISK_PCT", 1.20),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 2),
            daily_stop_pct=_float("DAILY_STOP_PCT", 2.0),
            monthly_stop_pct=_float("MONTHLY_STOP_PCT", 8.0),
            risk_reduction_drawdown_pct=_float("RISK_REDUCTION_DRAWDOWN_PCT", 10.0),
            signal_threshold=_float("SIGNAL_THRESHOLD", 86.0),
            high_conviction_threshold=_float("HIGH_CONVICTION_THRESHOLD", 90.0),
            startup_warmup_seconds=_int("STARTUP_WARMUP_SECONDS", 300),
            regime_confirm_seconds=_int("REGIME_CONFIRM_SECONDS", 180),
            # A large multi-symbol pool can contain contracts with delayed
            # history.  Do not let those permanently block ready symbols.
            min_ready_ratio=_float("MIN_READY_RATIO", 0.50),
            max_entry_spread_bps=_float("MAX_ENTRY_SPREAD_BPS", 10.0),
            evaluation_interval_seconds=_float("EVALUATION_INTERVAL_SECONDS", 2.0),
            feature_persist_seconds=_int("FEATURE_PERSIST_SECONDS", 10),
            account_persist_seconds=_int("ACCOUNT_PERSIST_SECONDS", 10),
            cooldown_seconds=_int("COOLDOWN_SECONDS", 900),
            max_holding_minutes=_int("MAX_HOLDING_MINUTES", 120),
            min_flow_exit_minutes=_int("MIN_FLOW_EXIT_MINUTES", 15),
            flow_exit_confirm_seconds=_int("FLOW_EXIT_CONFIRM_SECONDS", 90),
            stale_after_seconds=_int("STALE_AFTER_SECONDS", 8),
            bybit_depth=_int("BYBIT_DEPTH", 50),
            enable_binance=_bool("ENABLE_BINANCE", True),
            dashboard_token=os.getenv("DASHBOARD_TOKEN", "").strip(),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        )


@dataclass(frozen=True, slots=True)
class AccountConfig:
    name: str
    strategy: str
    starting_balance: float
    risk_pct: float
    max_leverage: float


def account_configs(settings: Settings) -> tuple[AccountConfig, ...]:
    if settings.high_leverage_lab:
        return tuple(AccountConfig(f"LIQ_ROI250_{lev}X_V040", "composite", settings.paper_balance, 0.25, float(lev))
                     for lev in (20, 50, 100, 200))
    return (
        AccountConfig(
            name="COMPOSITE_FLOW",
            strategy="composite",
            starting_balance=settings.paper_balance,
            risk_pct=settings.composite_base_risk_pct,
            max_leverage=settings.base_max_leverage,
        ),
    )
