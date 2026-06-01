"""
Unit tests for midas_risk.RiskManager.

Each test gets a fresh MidasConfig pointing at a tmp_path state file so
tests never touch ~/midas_ai/state/state.json and cannot interfere with
each other.
"""

import pytest

from midas_config import MidasConfig
from midas_risk import RiskManager, TradeSignal


# ══════════════════════════════════════════════════════════════════
#  FIXTURES
# ══════════════════════════════════════════════════════════════════

@pytest.fixture
def cfg(tmp_path):
    """MidasConfig with a temp state file — isolated from real bot state."""
    return MidasConfig(state_file=str(tmp_path / "state.json"))


@pytest.fixture
def rm(cfg):
    """Fresh RiskManager for each test."""
    return RiskManager(cfg)


def _signal(**overrides) -> TradeSignal:
    """Build a passing TradeSignal; override any field for edge-case tests."""
    defaults = dict(
        symbol="TEST",
        side="buy",
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        position_size_base=15.0,
        position_size_quote=1_500.0,
        risk_amount=75.0,
        risk_pct=0.5,        # exactly at the 0.5% limit — should pass
    )
    defaults.update(overrides)
    return TradeSignal(**defaults)


# ══════════════════════════════════════════════════════════════════
#  calculate_position_size
# ══════════════════════════════════════════════════════════════════

class TestCalculatePositionSize:

    def test_risk_capped_at_half_percent(self, rm):
        """
        equity=$15,000  entry=$100  stop=$98  (2% stop distance)

        Unconstrained size = $75 / 0.02 = $3,750.
        But the 20% notional cap ($3,000) is more restrictive here,
        so the position is clamped and the actual dollar risk drops to
        $3,000 × 0.02 = $60.  The risk still never exceeds the 0.5%
        cap ($75) — the cap is the ceiling, not the floor.
        """
        result = rm.calculate_position_size(
            entry_price=100.0,
            stop_loss_price=98.0,
            current_equity=15_000.0,
        )
        # Notional cap is the binding constraint at a 2% stop
        assert result["position_size_quote"] == 3_000.0
        assert result["risk_amount"]         == 60.0
        # Risk must never exceed the 0.5% ceiling
        assert result["risk_amount"] <= 15_000.0 * 0.005
        assert result["stop_distance_pct"]   == 2.0

    def test_dollar_risk_exactly_at_half_percent_when_cap_not_binding(self, rm):
        """
        equity=$15,000  entry=$100  stop=$95  (5% stop distance)

        Unconstrained size = $75 / 0.05 = $1,500 < $3,000 notional cap.
        Neither cap is binding; dollar risk is exactly $75 (0.5%).
        """
        result = rm.calculate_position_size(
            entry_price=100.0,
            stop_loss_price=95.0,
            current_equity=15_000.0,
        )
        assert result["risk_amount"]         == 75.0
        assert result["risk_pct"]            == 0.5
        assert result["position_size_quote"] == 1_500.0
        assert result["stop_distance_pct"]   == 5.0

    def test_notional_cap_clamps_oversized_position(self, rm):
        """
        equity=$15,000  entry=$100  stop=$99  (1% stop distance)

        Unconstrained size = $75 / 0.01 = $7,500 — far above the
        20% notional cap of $3,000.  Position must be clamped and
        actual dollar risk reduced proportionally.
        """
        result = rm.calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            current_equity=15_000.0,
        )
        assert result["position_size_quote"] == 3_000.0
        # Risk is capped by notional: $3,000 × 1% = $30
        assert result["risk_amount"]         == 30.0
        assert result["risk_amount"]         < 75.0    # less than the 0.5% cap

    def test_raises_on_stop_below_minimum_distance(self, rm):
        """Stop distance < 0.05% is rejected to prevent catastrophic oversizing."""
        with pytest.raises(ValueError, match="below minimum"):
            rm.calculate_position_size(
                entry_price=100.0,
                stop_loss_price=99.9999,   # ~0.0001% stop — absurdly tight
                current_equity=15_000.0,
            )

    def test_raises_on_zero_equity(self, rm):
        with pytest.raises(ValueError):
            rm.calculate_position_size(
                entry_price=100.0,
                stop_loss_price=95.0,
                current_equity=0.0,
            )

    def test_raises_on_negative_equity(self, rm):
        with pytest.raises(ValueError):
            rm.calculate_position_size(
                entry_price=100.0,
                stop_loss_price=95.0,
                current_equity=-1_000.0,
            )

    def test_raises_on_zero_entry_price(self, rm):
        with pytest.raises(ValueError):
            rm.calculate_position_size(
                entry_price=0.0,
                stop_loss_price=95.0,
                current_equity=15_000.0,
            )

    def test_raises_on_zero_stop_price(self, rm):
        with pytest.raises(ValueError):
            rm.calculate_position_size(
                entry_price=100.0,
                stop_loss_price=0.0,
                current_equity=15_000.0,
            )


# ══════════════════════════════════════════════════════════════════
#  check_kill_switch
# ══════════════════════════════════════════════════════════════════

class TestKillSwitch:

    def test_fires_at_exactly_3pct_drawdown(self, rm):
        """
        Day-start equity $15,000.  A drop to $14,550 is exactly 3%
        (the limit is ≥ 3%) so the kill switch must fire.
        """
        rm.state.day_start_equity = 15_000.0
        result = rm.check_kill_switch(14_550.0)
        assert result is True
        assert rm.state.halted is True
        assert rm.state.halt_reason != ""

    def test_fires_beyond_3pct_drawdown(self, rm):
        """Any loss beyond 3% must also trigger."""
        rm.state.day_start_equity = 15_000.0
        assert rm.check_kill_switch(14_000.0) is True   # ~6.7% drawdown
        assert rm.state.halted is True

    def test_halt_state_persists_after_equity_recovers(self, rm):
        """
        Once fired, the halt must NOT lift automatically even if equity
        rebounds above the threshold — daily_reset() is the only unlock.
        """
        rm.state.day_start_equity = 15_000.0
        rm.check_kill_switch(14_400.0)   # fires at 4% drawdown
        assert rm.state.halted is True

        # Equity "magically recovers" to above day-start
        result = rm.check_kill_switch(15_200.0)
        assert result is True            # still halted
        assert rm.state.halted is True

    def test_does_not_fire_within_limit(self, rm):
        """A 2% drop is below the 3% threshold — trading must continue."""
        rm.state.day_start_equity = 15_000.0
        result = rm.check_kill_switch(14_700.0)   # 2% drawdown
        assert result is False
        assert rm.state.halted is False

    def test_no_trigger_at_zero_drawdown(self, rm):
        """Flat equity must never trip the kill switch."""
        rm.state.day_start_equity = 15_000.0
        assert rm.check_kill_switch(15_000.0) is False

    def test_no_trigger_on_equity_gain(self, rm):
        """A positive day must definitely not halt the bot."""
        rm.state.day_start_equity = 15_000.0
        assert rm.check_kill_switch(15_500.0) is False


# ══════════════════════════════════════════════════════════════════
#  approve_trade
# ══════════════════════════════════════════════════════════════════

class TestApproveTrade:

    def test_rejects_when_bot_is_halted(self, rm):
        rm.state.halted      = True
        rm.state.halt_reason = "daily drawdown exceeded"
        approved, reason = rm.approve_trade(_signal(), open_positions=0)
        assert approved is False
        assert "halted" in reason.lower()

    def test_rejects_when_at_max_open_positions(self, rm):
        """max_open_trades=2; passing open_positions=2 means the slot is full."""
        approved, reason = rm.approve_trade(_signal(), open_positions=2)
        assert approved is False
        assert "Max open trades" in reason

    def test_rejects_when_risk_exceeds_half_percent(self, rm):
        """risk_pct=0.6 is above the 0.5% per-trade limit."""
        sig = _signal(risk_pct=0.6, risk_amount=90.0)
        approved, reason = rm.approve_trade(sig, open_positions=0)
        assert approved is False
        assert "exceeds limit" in reason

    def test_rejects_position_too_small(self, rm):
        """Notional below $10 is rejected regardless of risk percentage."""
        sig = _signal(position_size_quote=5.0, risk_pct=0.1, risk_amount=1.0)
        approved, reason = rm.approve_trade(sig, open_positions=0)
        assert approved is False
        assert "too small" in reason

    def test_approves_valid_trade(self, rm):
        """A trade within all limits must be approved with reason 'Approved'."""
        sig = _signal(risk_pct=0.5, position_size_quote=1_500.0, risk_amount=75.0)
        approved, reason = rm.approve_trade(sig, open_positions=0)
        assert approved is True
        assert reason == "Approved"

    def test_approves_with_one_slot_remaining(self, rm):
        """max_open_trades=2; one position already open — still one slot free."""
        sig = _signal(risk_pct=0.5, position_size_quote=1_500.0)
        approved, _ = rm.approve_trade(sig, open_positions=1)
        assert approved is True

    def test_rejects_when_open_positions_exceeds_max(self, rm):
        """Three open positions when max is 2 must be rejected."""
        approved, reason = rm.approve_trade(_signal(), open_positions=3)
        assert approved is False
        assert "Max open trades" in reason
