"""S5 core: pure, testable pieces shared by the backtest (s5_research) and the live paper bot (s5_strategy.py).
PAPER ONLY. No network, no wallet. Prices are in SOL per token.

pump.fun bonding curve (constant product on virtual reserves): starts at 30 virtual SOL / 1.073B virtual tokens and
completes ("graduates") when ~85 real SOL are in the curve. Price alone therefore tells the curve progress.
"""
import math, time

CURVE_V_SOL0 = 30.0
CURVE_V_TOK0 = 1_073_000_000.0
CURVE_K = CURVE_V_SOL0 * CURVE_V_TOK0
CURVE_REAL_SOL_GRAD = 85.0
GRAD_V_SOL = CURVE_V_SOL0 + CURVE_REAL_SOL_GRAD
GRAD_PRICE_SOL = GRAD_V_SOL ** 2 / CURVE_K            # ~4.11e-7 SOL/token = ~411 SOL market cap
SUPPLY = 1_000_000_000.0
# PumpSwap pool right after graduation (estimate): ~80 SOL against the 206.9M tokens left in the curve
AMM_K_EST = 80.0 * 206_900_000.0

LATENCY = 0.01                 # 1% adverse move on every fill (polling / landing delay)
TX_FEE_SOL = 0.000005 + 0.0005 + 0.0001   # base + priority + jito tip, per transaction (same profile as the other bots)


def curve_virtual_sol(price_sol):
    return math.sqrt(CURVE_K * max(price_sol, 0.0))


def curve_progress(price_sol):
    """0..100 % of the way to graduation (clamped)."""
    v = curve_virtual_sol(price_sol)
    return max(0.0, min(100.0, (v - CURVE_V_SOL0) / CURVE_REAL_SOL_GRAD * 100))


def price_at_progress(pct):
    v = CURVE_V_SOL0 + CURVE_REAL_SOL_GRAD * pct / 100
    return v * v / CURVE_K


def pool_fee_pct(phase, mcap_sol):
    """pump.fun pool fee per side (same table as bot/pumpfun.py, pump.fun docs updated 17 Oct 2026)."""
    if phase == "bonding" or mcap_sol is None or mcap_sol < 420:
        return 1.25
    tiers = [(1470, 1.20), (2460, 1.15), (3440, 1.10), (4420, 1.05), (9820, 1.00), (14740, 0.95), (19650, 0.90),
             (24560, 0.85), (29470, 0.80), (34380, 0.75), (39300, 0.70), (44210, 0.65), (49120, 0.60)]
    for cap, fee in tiers:
        if mcap_sol < cap:
            return fee
    return 0.55


def quote_reserve_sol(price_sol, phase):
    """SOL depth the trade walks against (estimate for PumpSwap: constant product from graduation, ignores LP changes)."""
    if phase == "bonding":
        return curve_virtual_sol(price_sol)
    return math.sqrt(AMM_K_EST * max(price_sol, 0.0))


def _impact(size_sol, price_sol, phase):
    q = max(quote_reserve_sol(price_sol, phase), 1e-9)
    return size_sol / q            # constant-product: average fill moves by ~x/Q for a buy (and a sell of the same value)


def buy_fill_price(price_sol, size_sol, phase):
    fee = pool_fee_pct(phase, price_sol * SUPPLY) / 100
    return price_sol * (1 + _impact(size_sol, price_sol, phase)) * (1 + LATENCY) / (1 - fee)


def sell_fill_price(price_sol, value_sol, phase):
    fee = pool_fee_pct(phase, price_sol * SUPPLY) / 100
    return price_sol / (1 + _impact(value_sol, price_sol, phase)) * (1 - LATENCY) * (1 - fee)


# ---------------------------------------------------------------- sizing and account-level risk
def position_size(equity, stop_frac, risk_pct=1.0, cap_frac=0.10):
    """Size so that hitting the stop costs ~risk_pct of equity. No stop -> the whole stake is at risk."""
    risk = equity * risk_pct / 100
    size = risk / stop_frac if stop_frac else risk
    return min(size, equity * cap_frac)


class RiskGate:
    """Max open positions + daily auto-stop (day = local calendar day, Asia/Dubai by default)."""

    def __init__(self, start_equity, max_open=3, daily_stop_pct=3.0, tz_offset_h=4):
        self.max_open, self.daily_stop_pct, self.tz = max_open, daily_stop_pct, tz_offset_h * 3600
        self.day, self.day_eq = None, start_equity

    def _day(self, ts):
        return int((ts + self.tz) // 86400)

    def day_start(self, ts, equity):
        self.day, self.day_eq = self._day(ts), equity

    def roll(self, ts, equity):
        if self.day != self._day(ts):
            self.day_start(ts, equity)

    def day_pnl_pct(self, equity):
        return (equity / self.day_eq - 1) * 100 if self.day_eq else 0.0

    def halted(self, ts, equity):
        self.roll(ts, equity)
        return self.day_pnl_pct(equity) <= -self.daily_stop_pct

    def can_enter(self, ts, open_n, equity):
        if self.day is not None and self.day != self._day(ts):
            self.day_start(ts, equity)
        if self.day is not None and self.day_pnl_pct(equity) <= -self.daily_stop_pct:
            return False, f"daily_auto_stop({self.day_pnl_pct(equity):.1f}%)"
        if open_n >= self.max_open:
            return False, "max_open_positions"
        return True, ""


# ---------------------------------------------------------------- exit engine (the strategy card's exits)
class Position:
    """Card keys: stop_pct, tp=[(x, frac_of_original)], trail_pct, trail_arm_x (None = after first TP), timeout_min,
    close_below_entry, exit_on_graduation, exit_after_grad_min. Prices are DEX marks; fill costs are applied by the caller.
    step() takes one candle (open/high/low/close); conservative ordering: stop before take-profit inside a candle."""

    def __init__(self, entry_px, opened, card, phase):
        self.entry, self.opened, self.card, self.phase0 = entry_px, opened, card, phase
        self.left, self.peak, self.tp_done, self.closed = 1.0, entry_px, 0, False
        self.grad_ts = opened if phase == "graduated" else None
        self.trail_armed = card.get("trail_arm_x") is not None and card.get("trail_pct") and entry_px * card["trail_arm_x"] <= entry_px

    def _trail(self):
        """Trailing-stop % in force: optional wider trail for what is left after the first take-profit (S6 moonbag)."""
        c = self.card
        return c["trail_after_tp_pct"] if self.tp_done and c.get("trail_after_tp_pct") else c.get("trail_pct")

    def _stop_px(self):
        c = self.card
        s = self.entry * (1 - c["stop_pct"] / 100) if c.get("stop_pct") else None
        if self.trail_armed and c.get("trail_pct"):
            t = self.peak * (1 - self._trail() / 100)
            s = t if s is None else max(s, t)
        return s

    def _stop_reason(self):
        c = self.card
        if self.trail_armed and c.get("trail_pct"):
            t = self.peak * (1 - self._trail() / 100)
            if not c.get("stop_pct") or t >= self.entry * (1 - c["stop_pct"] / 100):
                return "trailing_stop"
        return "stop_loss"

    def _close(self, px, reason):
        f = self.left
        self.left, self.closed = 0.0, True
        return [(f, px, reason)]

    def step(self, ts, o, h, l, c, phase):
        if self.closed:
            return []
        C = self.card
        if phase == "graduated" and self.grad_ts is None:
            self.grad_ts = ts
            if C.get("exit_on_graduation"):
                return self._close(o, "graduated_sold")
        # 1. stop / trailing stop (from the peak known BEFORE this candle)
        sp = self._stop_px()
        if sp is not None and l <= sp:
            px = o if o <= sp else (sp if c >= sp else c)
            return self._close(px, self._stop_reason())
        # 2. time rules (evaluated at the candle's open)
        tmo = C["timeout_after_tp_min"] if self.tp_done and C.get("timeout_after_tp_min") else C.get("timeout_min")
        if tmo and ts - self.opened >= tmo * 60:
            return self._close(o, "moonbag_time_exit" if tmo != C.get("timeout_min") else "time_exit")
        if C.get("exit_after_grad_min") is not None and self.grad_ts is not None and ts - self.grad_ts >= C["exit_after_grad_min"] * 60:
            return self._close(o, "time_after_graduation")
        acts = []
        # 3. take-profit ladder
        while self.tp_done < len(C.get("tp") or []):
            x, frac = C["tp"][self.tp_done]
            if h < self.entry * x:
                break
            self.tp_done += 1
            f = self.left if frac >= self.left - 1e-12 else frac
            px = max(o, self.entry * x)
            self.left -= f
            acts.append((f, px, f"take_profit_{x:g}x"))
            if C.get("trail_pct") and C.get("trail_arm_x") is None:
                self.trail_armed = True
            if self.left <= 1e-12:
                self.closed, self.left = True, 0.0
                return acts
        # 4. update peak, arm trail, then close-based rules
        self.peak = max(self.peak, h)
        if C.get("trail_pct") and C.get("trail_arm_x") is not None and self.peak >= self.entry * C["trail_arm_x"]:
            self.trail_armed = True
        if self.trail_armed and C.get("trail_pct") and c <= self.peak * (1 - self._trail() / 100) and not acts:
            return self._close(c, "trailing_stop")
        if C.get("close_below_entry") and c < self.entry:
            return acts + self._close(c, "close_below_entry")
        return acts

    def tick(self, ts, mark, phase):
        """Live use: one price observation."""
        return self.step(ts, mark, mark, mark, mark, phase)

    def state(self):
        return {"entry": self.entry, "opened": self.opened, "left": self.left, "peak": self.peak, "tp_done": self.tp_done,
                "closed": self.closed, "grad_ts": self.grad_ts, "trail_armed": bool(self.trail_armed), "phase0": self.phase0}

    @classmethod
    def from_state(cls, s, card):
        p = cls(s["entry"], s["opened"], card, s.get("phase0", "bonding"))
        for k in ("left", "peak", "tp_done", "closed", "grad_ts", "trail_armed"):
            setattr(p, k, s[k])
        return p
