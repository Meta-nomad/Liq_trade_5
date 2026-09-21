from __future__ import annotations

import time
import math
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable

from .config import AccountConfig, Settings
from .market import MarketState
from .models import AccountState, ClosedTrade, FeatureSnapshot, Position, Side, Signal


def _utc_keys(ts: float) -> tuple[str, str]:
    current = datetime.fromtimestamp(ts, tz=UTC)
    return current.strftime("%Y-%m-%d"), current.strftime("%Y-%m")


@dataclass(slots=True)
class Fill:
    price: float
    slippage_bps: float


class PaperBroker:
    """Virtual execution engine. It has no exchange credentials or order client."""

    def __init__(
        self,
        market: MarketState,
        settings: Settings,
        configs: Iterable[AccountConfig],
    ) -> None:
        self.entry_decisions: dict[str, dict[str, Any]] = {}
        self.market = market
        self.settings = settings
        now = time.time()
        day_key, month_key = _utc_keys(now)
        self.accounts: dict[str, AccountState] = {
            config.name: AccountState(
                name=config.name,
                strategy=config.strategy,
                starting_balance=config.starting_balance,
                balance=config.starting_balance,
                risk_pct=config.risk_pct,
                max_leverage=config.max_leverage,
                peak_equity=config.starting_balance,
                day_start_equity=config.starting_balance,
                month_start_equity=config.starting_balance,
                day_key=day_key,
                month_key=month_key,
            )
            for config in configs
        }

    def restore(self, payloads: dict[str, dict[str, Any]]) -> None:
        for name, payload in payloads.items():
            if name in self.accounts:
                self.accounts[name] = AccountState.from_dict(payload)

    def mark_price(self, symbol: str) -> float:
        return self.market.symbol(symbol).reference_price()

    def equity(self, account: AccountState) -> float:
        unrealized = sum(
            position.unrealized_pnl(self.mark_price(symbol))
            for symbol, position in account.positions.items()
            if self.mark_price(symbol) > 0
        )
        return account.balance + unrealized

    @staticmethod
    def used_margin(account: AccountState) -> float:
        return sum(position.margin for position in account.positions.values())

    def _roll_risk_periods(self, account: AccountState, now: float) -> None:
        day_key, month_key = _utc_keys(now)
        equity = self.equity(account)
        if account.day_key != day_key:
            account.day_key = day_key
            account.day_start_equity = equity
            account.stop_losses_today = 0
            if account.halted_reason.startswith("daily"):
                account.halted_reason = ""
        if account.month_key != month_key:
            account.month_key = month_key
            account.month_start_equity = equity
            if account.halted_reason.startswith("monthly"):
                account.halted_reason = ""
        account.peak_equity = max(account.peak_equity, equity)

        daily_return = (
            equity / account.day_start_equity - 1.0 if account.day_start_equity > 0 else 0.0
        )
        monthly_return = (
            equity / account.month_start_equity - 1.0 if account.month_start_equity > 0 else 0.0
        )
        if monthly_return <= -self.settings.monthly_stop_pct / 100.0:
            account.halted_reason = f"monthly stop {monthly_return:.1%}"
        elif daily_return <= -self.settings.daily_stop_pct / 100.0:
            account.halted_reason = f"daily stop {daily_return:.1%}"

    def _fill(
        self,
        symbol: str,
        execution_side: Side,
        notional: float,
        *,
        now: float | None = None,
        stress: float = 1.0,
        base_qty: float | None = None,
    ) -> Fill | None:
        state = self.market.symbol(symbol)
        current_time = now or time.time()
        book = state.book("mexc")
        bbo = book.best_bid_ask()
        if not bbo or not book.is_fresh(current_time, self.settings.stale_after_seconds):
            return None
        bid, ask = bbo
        if bid >= ask:
            return None
        reference = ask if execution_side == Side.LONG else bid
        impact = (book.quantity_impact_bps(execution_side, base_qty)
                  if base_qty is not None else book.impact_bps(execution_side, notional))
        if not math.isfinite(impact):
            return None
        slippage = max(self.settings.min_slippage_bps, impact) * stress
        price = reference * (1.0 + float(execution_side) * slippage / 10_000.0)
        return Fill(price=price, slippage_bps=slippage)

    def _effective_risk_pct(self, account: AccountState, signal: Signal) -> float:
        if self.settings.high_leverage_lab:
            return min(account.risk_pct, signal.risk_pct) if signal.risk_pct > 0 else account.risk_pct
        if signal.risk_pct > 0:
            risk_pct = signal.risk_pct
        else:
            if signal.score < 84.0:
                conviction = 0.65
            elif signal.score < self.settings.high_conviction_threshold:
                conviction = 0.85
            else:
                conviction = 1.0
            risk_pct = account.risk_pct * conviction
        drawdown = 1.0 - self.equity(account) / account.peak_equity if account.peak_equity > 0 else 0.0
        if drawdown >= self.settings.risk_reduction_drawdown_pct / 100.0:
            risk_pct *= 0.5
        return risk_pct

    def _entry_decision(self, account, signal, now, reason, **details):
        self.entry_decisions[f"{account.name}:{signal.symbol}"] = {
            "account": account.name, "symbol": signal.symbol, "ts": now,
            "setup": signal.setup, "side": signal.side.label,
            "score": signal.score, "stop_pct": signal.stop_pct,
            "leverage": account.max_leverage, "reason": reason, **details,
        }
        return False

    def can_open(self, account: AccountState, signal: Signal, now: float) -> bool:
        def reject(reason):
            return self._entry_decision(account, signal, now, reason)
        if signal.symbol not in self.market.symbols:
            return reject("unknown_symbol")
        if not all(math.isfinite(v) for v in (signal.score, signal.stop_pct, signal.risk_pct, signal.ts)):
            return reject("invalid_signal_numbers")
        if not 0 < signal.stop_pct < 1 or signal.risk_pct < 0:
            return reject("invalid_signal_risk")
        self._roll_risk_periods(account, now)
        if self.settings.data_mode == "live" and self.market.symbol(signal.symbol).universe_valid_until < now:
            return reject("universe_not_verified")
        if account.halted_reason:
            return reject(account.halted_reason)
        if signal.symbol in account.positions:
            return reject("position_already_open")
        if len(account.positions) >= self.settings.max_open_positions:
            return reject("position_limit")
        if self.settings.high_leverage_lab and account.positions:
            return reject("one_position_per_lab_account")
        cluster = self._correlation_cluster(signal.symbol)
        if any(self._correlation_cluster(symbol) == cluster for symbol in account.positions):
            return reject("correlation_limit")
        if account.cooldowns.get(signal.symbol, 0.0) > now:
            return reject("cooldown")
        if signal.score < self.settings.signal_threshold:
            return reject("score_below_threshold")
        return True

    @staticmethod
    def _correlation_cluster(symbol: str) -> str:
        asset = symbol.split("_", 1)[0]
        if asset in {"BTC", "ETH", "BNB", "SOL"}:
            return "majors"
        if asset in {"XRP", "ADA", "DOGE"}:
            return "high_beta"
        if asset in {"AVAX", "LINK"}:
            return "infrastructure"
        return asset

    def open_from_signal(self, account: AccountState, signal: Signal, now: float) -> Position | None:
        if self.settings.high_leverage_lab and signal.setup not in {
            "LIQUIDATION_EXHAUSTION",
            "SYSTEMATIC_BREAKOUT_4H",
        }:
            self._entry_decision(account, signal, now, "setup_not_allowed")
            return None
        if not self.can_open(account, signal, now):
            return None
        equity = self.equity(account)
        if equity <= 0:
            account.halted_reason = "account depleted"
            self._entry_decision(account, signal, now, "account_depleted")
            return None

        state = self.market.symbol(signal.symbol)
        if self.settings.high_leverage_lab and self.settings.data_mode == "live":
            distance = 1.0 / account.max_leverage - state.maintenance_margin_rate - 2*self.settings.taker_fee_rate
            if not state.contract_metadata_ready or not state.api_allowed or account.max_leverage > state.contract_max_leverage or signal.stop_pct + 0.002 >= distance:
                reason = ("metadata_unverified" if not state.contract_metadata_ready else
                          "api_not_allowed" if not state.api_allowed else
                          "contract_leverage_limit" if account.max_leverage > state.contract_max_leverage else
                          "stop_outside_liquidation_buffer")
                self._entry_decision(account, signal, now, reason,
                    liquidation_distance_pct=distance*100,
                    required_stop_buffer_pct=(signal.stop_pct+.002)*100)
                logging.getLogger(__name__).info("PAPER REJECT account=%s symbol=%s reason=%s", account.name, signal.symbol, reason)
                return None
        risk_pct = self._effective_risk_pct(account, signal)
        risk_budget = equity * risk_pct / 100.0
        open_risk = sum(position.initial_risk_usdt for position in account.positions.values())
        max_open_risk = equity * self.settings.max_portfolio_risk_pct / 100.0
        if open_risk + risk_budget > max_open_risk:
            self._entry_decision(account, signal, now, "portfolio_risk_limit")
            return None
        estimated_cost_pct = 2.0 * self.settings.taker_fee_rate + (
            2.0 * self.settings.min_slippage_bps / 10_000.0
        )
        risk_fraction = signal.stop_pct + estimated_cost_pct
        desired_notional = risk_budget / risk_fraction if risk_fraction > 0 else 0.0

        available_margin = max(
            0.0,
            equity * self.settings.max_margin_utilization - self.used_margin(account),
        )
        notional = min(desired_notional, available_margin * account.max_leverage)
        if notional < 10.0:
            self._entry_decision(account, signal, now, "notional_below_minimum")
            return None
        fill = self._fill(signal.symbol, signal.side, notional, now=now)
        if fill is None or fill.slippage_bps > self.settings.impact_slippage_bps or state.book("mexc").spread_bps() > self.settings.max_entry_spread_bps:
            self._entry_decision(account, signal, now, "execution_depth_spread")
            logging.getLogger(__name__).info("PAPER REJECT account=%s symbol=%s reason=execution_depth_spread", account.name, signal.symbol)
            return None
        # Re-size using observed entry impact and stress allowance for exit.
        notional = min(notional, risk_budget / (signal.stop_pct + 2*self.settings.taker_fee_rate + 3*fill.slippage_bps/10000))
        qty = notional / fill.price
        entry_fee = notional * self.settings.taker_fee_rate
        account.balance -= entry_fee
        account.total_fees += entry_fee

        actual_risk = notional * (
            signal.stop_pct
            + 2.0 * self.settings.taker_fee_rate
            + 2.0 * fill.slippage_bps / 10_000.0
        )
        stop_price = fill.price * (1.0 - float(signal.side) * signal.stop_pct)
        # Translate the requested reward/risk into a target after estimated
        # round-trip fees. The old implementation called a gross 1.5R target
        # "1.5R" even when the realised net trade was close to 1R.
        target_gross = signal.target_r * actual_risk + entry_fee + notional * self.settings.taker_fee_rate
        target_move_pct = target_gross / notional
        if self.settings.high_leverage_lab:
            c = self.settings.taker_fee_rate + self.settings.min_slippage_bps/10000
            target_move_pct = (2.5/account.max_leverage + self.settings.taker_fee_rate + c)/(1-float(signal.side)*c)
        target_price = fill.price * (1.0 + float(signal.side) * target_move_pct)
        position = Position(
            id=uuid.uuid4().hex,
            account=account.name,
            symbol=signal.symbol,
            setup=signal.setup,
            side=signal.side,
            score=signal.score,
            opened_at=now,
            entry_price=fill.price,
            qty=qty,
            notional=notional,
            leverage=account.max_leverage,
            margin=notional / account.max_leverage,
            initial_risk_usdt=actual_risk,
            initial_stop_price=stop_price,
            stop_price=stop_price,
            target_price=target_price,
            target_r=signal.target_r,
            entry_fee=entry_fee,
            exit_mode=signal.exit_mode,
            max_holding_minutes=signal.max_holding_minutes,
        )
        logging.getLogger(__name__).info("PAPER POSITION account=%s leverage=%.0f margin=%.4f notional=%.4f risk=%.4f target_net_roi=250%% liquidation_model=APPROXIMATE", account.name, position.leverage, position.margin, position.notional, position.initial_risk_usdt)
        self._entry_decision(account, signal, now, "opened", position_id=position.id,
                             notional=notional, planned_risk=actual_risk)
        account.positions[signal.symbol] = position
        account.cooldowns[signal.symbol] = now + self.settings.cooldown_seconds
        return position

    def handle_signal(self, signal: Signal, now: float | None = None) -> list[Position]:
        current_time = now or time.time()
        opened: list[Position] = []
        for account in self.accounts.values():
            if account.strategy != signal.strategy:
                continue
            position = self.open_from_signal(account, signal, current_time)
            if position:
                opened.append(position)
        return opened

    def close_position(
        self,
        account: AccountState,
        position: Position,
        reason: str,
        now: float,
        *,
        stress: float = 1.0,
        liquidation_fee_rate: float = 0.0,
    ) -> ClosedTrade | None:
        exit_side = Side.SHORT if position.side == Side.LONG else Side.LONG
        exit_reference = self.market.symbol(position.symbol).book("mexc").best_bid_ask()
        exit_value = position.qty * (exit_reference[0] if position.side == Side.LONG else exit_reference[1]) if exit_reference else position.notional
        fill = self._fill(position.symbol, exit_side, exit_value, now=now, stress=stress, base_qty=position.qty)
        if fill is None:
            logging.getLogger(__name__).warning("PAPER EXIT UNRESOLVED account=%s symbol=%s reason=missing_execution_liquidity", account.name, position.symbol)
            return None
        gross = position.unrealized_pnl(fill.price)
        exit_notional = abs(position.qty * fill.price)
        exit_fee = exit_notional * self.settings.taker_fee_rate
        liquidation_fee = exit_notional * liquidation_fee_rate
        account.balance += gross - exit_fee - liquidation_fee
        account.total_fees += exit_fee + liquidation_fee
        net = gross - position.entry_fee - exit_fee - liquidation_fee + position.accrued_funding
        logging.getLogger(__name__).info("PAPER ROI account=%s symbol=%s net_pnl=%.6f initial_margin=%.6f net_roi_pct=%.4f", account.name, position.symbol, net, position.margin, net/position.margin*100)
        account.realized_pnl += gross - position.entry_fee - exit_fee - liquidation_fee
        if net >= 0:
            account.wins += 1
        else:
            account.losses += 1
        if reason == "STOP" and net < 0:
            account.stop_losses_today += 1
            if account.stop_losses_today >= 3:
                account.halted_reason = "daily stop-count 3"
        trade = ClosedTrade(
            id=position.id,
            account=account.name,
            symbol=position.symbol,
            setup=position.setup,
            side=position.side,
            score=position.score,
            opened_at=position.opened_at,
            closed_at=now,
            entry_price=position.entry_price,
            exit_price=fill.price,
            qty=position.qty,
            notional=position.notional,
            gross_pnl=gross,
            fees=position.entry_fee + exit_fee + liquidation_fee,
            funding=position.accrued_funding,
            net_pnl=net,
            r_multiple=net / position.initial_risk_usdt if position.initial_risk_usdt > 0 else 0.0,
            reason=reason,
            leverage=position.leverage,
            initial_margin=position.margin,
            net_roi_pct=net/position.margin*100,
        )
        account.positions.pop(position.symbol, None)
        self._roll_risk_periods(account, now)
        return trade

    def evaluate_positions(
        self,
        features: dict[str, FeatureSnapshot],
        now: float | None = None,
    ) -> list[ClosedTrade]:
        current_time = now or time.time()
        closed: list[ClosedTrade] = []
        for account in self.accounts.values():
            self._roll_risk_periods(account, current_time)
            for symbol, position in list(account.positions.items()):
                state = self.market.symbol(symbol)
                book = state.book("mexc")
                bbo = book.best_bid_ask() if book.is_fresh(current_time, self.settings.stale_after_seconds) else None
                if not bbo:
                    continue
                bid, ask = bbo
                mark = bid if position.side == Side.LONG else ask
                position.best_price = (
                    max(position.best_price, mark)
                    if position.side == Side.LONG
                    else min(position.best_price, mark)
                )
                position.worst_price = (
                    min(position.worst_price, mark)
                    if position.side == Side.LONG
                    else max(position.worst_price, mark)
                )

                leverage_distance = max(0.0, 1.0 / position.leverage - state.maintenance_margin_rate - 2*self.settings.taker_fee_rate)
                liquidation_price = position.entry_price * (
                    1.0 - float(position.side) * leverage_distance
                )
                liquidated = (
                    mark <= liquidation_price
                    if position.side == Side.LONG
                    else mark >= liquidation_price
                )
                stop_hit = mark <= position.stop_price if position.side == Side.LONG else mark >= position.stop_price
                target_hit = mark >= position.target_price if position.side == Side.LONG else mark <= position.target_price
                if self.settings.high_leverage_lab:
                    exit_fill = self._fill(symbol, Side(-int(position.side)), position.qty*mark, now=current_time, base_qty=position.qty)
                    if exit_fill is None:
                        target_hit = False
                    else:
                        expected_net = position.unrealized_pnl(exit_fill.price)-position.entry_fee-position.qty*exit_fill.price*self.settings.taker_fee_rate+position.accrued_funding
                        target_hit = expected_net >= 2.5*position.margin

                current_r = position.current_r(mark)
                if current_r >= 1.0:
                    break_even_buffer = 2.0 * self.settings.taker_fee_rate + (
                        2.0 * self.settings.min_slippage_bps / 10_000.0
                    )
                    break_even = position.entry_price * (
                        1.0 + float(position.side) * break_even_buffer
                    )
                    if position.side == Side.LONG:
                        position.stop_price = max(position.stop_price, break_even)
                    else:
                        position.stop_price = min(position.stop_price, break_even)

                if position.exit_mode == "trend" and current_r >= 1.25:
                    risk_distance = abs(position.entry_price - position.initial_stop_price)
                    trailing_distance = risk_distance * 1.35
                    trailing_stop = position.best_price - float(position.side) * trailing_distance
                    if position.side == Side.LONG:
                        position.stop_price = max(position.stop_price, trailing_stop)
                    else:
                        position.stop_price = min(position.stop_price, trailing_stop)

                age = current_time - position.opened_at
                feature = features.get(symbol)
                reversal_observed = bool(
                    account.strategy == "baseline"
                    and age >= self.settings.min_flow_exit_minutes * 60
                    and feature
                    and current_r < 0.75
                    and float(position.side) * feature.flow_fast < -0.30
                    and float(position.side) * feature.flow_slow < -0.12
                    and float(position.side) * feature.book_imbalance < -0.10
                    and float(position.side) * feature.cross_venue_consensus < -0.10
                )
                if reversal_observed:
                    if not position.flow_reversal_started_at:
                        position.flow_reversal_started_at = current_time
                else:
                    position.flow_reversal_started_at = 0.0
                flow_reversal = bool(
                    position.flow_reversal_started_at
                    and current_time - position.flow_reversal_started_at
                    >= self.settings.flow_exit_confirm_seconds
                )
                max_holding = position.max_holding_minutes or self.settings.max_holding_minutes
                timed_out = age >= max_holding * 60 and current_r < 0.75

                reason = ""
                stress = 1.0
                liquidation_fee = 0.0
                if liquidated:
                    reason = "APPROX_LIQUIDATION"
                    stress = 3.0
                    liquidation_fee = 0.0004
                elif stop_hit:
                    reason = "STOP"
                    stress = 2.0
                elif target_hit:
                    reason = "TARGET"
                elif flow_reversal:
                    reason = "ORDER_FLOW_REVERSAL"
                elif timed_out:
                    reason = "TIME_STOP"

                if reason:
                    trade = self.close_position(
                        account,
                        position,
                        reason,
                        current_time,
                        stress=stress,
                        liquidation_fee_rate=liquidation_fee,
                    )
                    if trade:
                        closed.append(trade)
        return closed

    def apply_funding(self, symbol: str, rate: float, settlement_ts: float) -> None:
        if not rate or not settlement_ts:
            return
        for account in self.accounts.values():
            position = account.positions.get(symbol)
            if not position or settlement_ts <= max(position.last_funding_at, position.opened_at):
                continue
            cashflow = -position.notional * rate * float(position.side)
            account.balance += cashflow
            account.total_funding += cashflow
            position.accrued_funding += cashflow
            position.last_funding_at = settlement_ts

    def account_summary(self, account: AccountState) -> dict[str, Any]:
        equity = self.equity(account)
        drawdown = equity / account.peak_equity - 1.0 if account.peak_equity > 0 else 0.0
        total_return = equity / account.starting_balance - 1.0 if account.starting_balance > 0 else 0.0
        trades = account.wins + account.losses
        return {
            "name": account.name,
            "strategy": account.strategy,
            "starting_balance": account.starting_balance,
            "balance": account.balance,
            "equity": equity,
            "return_pct": total_return * 100.0,
            "drawdown_pct": drawdown * 100.0,
            "peak_equity": account.peak_equity,
            "risk_pct": account.risk_pct,
            "max_leverage": account.max_leverage,
            "used_margin": self.used_margin(account),
            "positions_count": len(account.positions),
            "wins": account.wins,
            "losses": account.losses,
            "stop_losses_today": account.stop_losses_today,
            "win_rate_pct": account.wins / trades * 100.0 if trades else 0.0,
            "realized_pnl": account.realized_pnl,
            "total_fees": account.total_fees,
            "total_funding": account.total_funding,
            "halted_reason": account.halted_reason,
            "entry_state": ('halted' if account.halted_reason else
                            'position_limit' if len(account.positions) >= (1 if self.settings.high_leverage_lab else self.settings.max_open_positions)
                            else 'awaiting_valid_signal'),
            "positions": [
                position.as_dict(self.mark_price(position.symbol))
                for position in account.positions.values()
            ],
        }

    def status(self) -> dict[str, Any]:
        return {
            name: self.account_summary(account) for name, account in self.accounts.items()
        }

    def snapshots(self) -> dict[str, dict[str, Any]]:
        return {name: account.as_dict() for name, account in self.accounts.items()}
