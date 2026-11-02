"""Paper portfolio: simulated fills using real Jupiter quotes + modeled Solana costs. No transactions."""
import sqlite3, csv, os, time, json, threading, logging
from . import sources

log = logging.getLogger("paper")
WSOL = sources.WSOL
CSV_FIELDS = ["id", "strategy", "mint", "symbol", "opened", "closed", "hold_s", "size_sol", "tokens",
              "entry_mark_sol", "exit_mark_sol", "sol_out", "fees_sol", "slippage_sol", "pnl_sol", "pnl_pct",
              "exit_reason", "fill_source", "meta"]


class Paper:
    def __init__(self, cfg, data_dir="data"):
        self.cfg = cfg
        self.c = cfg["costs"]
        self.r = cfg["risk"]
        self.lock = threading.RLock()
        os.makedirs(data_dir, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(data_dir, "paper.db"), check_same_thread=False)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, run_id TEXT, strategy TEXT, mint TEXT, symbol TEXT,
          opened REAL, closed REAL, hold_s REAL, size_sol REAL, tokens REAL, entry_mark_sol REAL, exit_mark_sol REAL,
          sol_out REAL, fees_sol REAL, slippage_sol REAL, pnl_sol REAL, pnl_pct REAL, exit_reason TEXT, fill_source TEXT, meta TEXT);
        CREATE TABLE IF NOT EXISTS scans (run_id TEXT, ts REAL, mint TEXT, symbol TEXT, source TEXT, liq REAL, score REAL,
          decision TEXT, reasons TEXT, info TEXT);
        CREATE TABLE IF NOT EXISTS runs (run_id TEXT, started REAL, ended REAL, start_balance REAL, end_balance REAL, summary TEXT);
        """)
        self.csv_path = os.path.join(data_dir, "trades.csv")
        if not os.path.exists(self.csv_path):
            with open(self.csv_path, "w", newline="") as f:
                csv.writer(f).writerow(["run_id"] + CSV_FIELDS)
        self.run_id = time.strftime("%Y%m%d-%H%M%S")
        self.balance = float(self.r["start_balance_sol"])
        self.start_balance = self.balance
        self.positions = {}   # mint -> dict
        self.closed = []
        self.traded_mints = set()
        self.db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (self.run_id, time.time(), None, self.balance, None, None))
        self.db.commit()

    # ---- costs ----
    def tx_cost(self):
        return self.c["base_fee_sol"] + self.c["priority_fee_sol"] + self.c["jito_tip_sol"]

    def log_scan(self, mint, symbol, source, liq, score, decision, reasons, info):
        with self.lock:
            self.db.execute("INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (self.run_id, time.time(), mint, symbol, source, liq, score, decision,
                             ";".join(reasons), json.dumps(info, default=str)))
            self.db.commit()

    def can_open(self, mint):
        with self.lock:
            if mint in self.positions or mint in self.traded_mints:
                return False, "already_traded"
            if len(self.positions) >= self.r["max_concurrent_positions"]:
                return False, "max_positions"
            size = self.size()
            if size < self.r["min_position_sol"]:
                return False, "balance_too_low"
            return True, ""

    def size(self):
        s = min(self.balance * self.r["position_size_pct"] / 100, self.r["max_position_sol"])
        avail = self.balance - self.r["fee_reserve_sol"] - self.tx_cost() - self.c["ata_rent_sol"]
        return max(0.0, min(s, avail))

    # ---- open ----
    def open(self, strategy, mint, symbol, decimals, mark_sol, meta):
        with self.lock:
            ok, why = self.can_open(mint)
            if not ok:
                return None
            size = self.size()
        q = sources.jup_quote(WSOL, mint, size * 1e9, self.c["max_slippage_bps"])
        lat = self.c["latency_slippage_bps"] / 1e4
        if q and q.get("outAmount"):
            tokens = int(q["outAmount"]) / 10 ** decimals * (1 - lat)
            src = "jupiter_quote"
            impact = float(q.get("priceImpactPct") or 0)
        else:
            if not self.c.get("allow_fallback_fills", True):
                return None
            impact = self.c["fallback_impact_pct"] / 100
            tokens = size / mark_sol * (1 - impact - self.c["fallback_amm_fee_pct"] / 100 - lat)
            src = "modeled_fallback"
        if tokens <= 0:
            return None
        fees = self.tx_cost()
        slip_sol = size - tokens * mark_sol  # value lost vs mark at entry (AMM fee + impact + latency)
        with self.lock:
            if mint in self.positions or len(self.positions) >= self.r["max_concurrent_positions"]:
                return None
            self.balance -= size + fees + self.c["ata_rent_sol"]
            pos = dict(strategy=strategy, mint=mint, symbol=symbol, decimals=decimals, opened=time.time(),
                       size_sol=size, tokens=tokens, entry_mark=mark_sol, peak_mark=mark_sol, last_mark=mark_sol,
                       fees=fees, entry_slip=slip_sol, fill_source=src, meta=meta, exit_fail=0, last_price_ok=time.time())
            self.positions[mint] = pos
            self.traded_mints.add(mint)
        log.info("PAPER BUY  [%s] %s %s size=%.4f SOL tokens=%.2f mark=%.3e impact=%.2f%% fill=%s bal=%.4f",
                 strategy, symbol, mint, size, tokens, mark_sol, impact * 100, src, self.balance)
        return pos

    # ---- exit logic ----
    def check_exit(self, pos, mark):
        x = self.cfg["exits"]
        chg = mark / pos["entry_mark"] - 1
        peak = pos["peak_mark"] / pos["entry_mark"] - 1
        if chg >= x["take_profit_pct"] / 100:
            return "take_profit"
        if chg <= -x["stop_loss_pct"] / 100:
            return "stop_loss"
        if peak >= x["trailing_activate_pct"] / 100 and mark <= pos["peak_mark"] * (1 - x["trailing_stop_pct"] / 100):
            return "trailing_stop"
        if time.time() - pos["opened"] >= x["max_hold_minutes"] * 60:
            return "max_hold"
        return None

    def close(self, mint, reason, mark):
        with self.lock:
            pos = self.positions.get(mint)
        if not pos:
            return None
        raw = int(pos["tokens"] * 10 ** pos["decimals"])
        q = sources.jup_quote(mint, WSOL, raw, self.c["max_slippage_bps"])
        lat = self.c["latency_slippage_bps"] / 1e4
        if q and q.get("outAmount"):
            sol_out = int(q["outAmount"]) / 1e9 * (1 - lat)
            src = "jupiter_quote"
        elif mark:
            sol_out = pos["tokens"] * mark * (1 - self.c["fallback_impact_pct"] / 100 - self.c["fallback_amm_fee_pct"] / 100 - lat)
            src = "modeled_fallback"
        else:
            pos["exit_fail"] += 1
            if pos["exit_fail"] < 20:
                return None
            sol_out, src = 0.0, "no_route_writeoff"
        fees = pos["fees"] + self.tx_cost()
        exit_slip = (pos["tokens"] * mark - sol_out) if mark else 0
        rent_back = self.c["ata_rent_sol"] if self.c.get("close_token_account", True) else 0
        pnl = sol_out - pos["size_sol"] - fees - (0 if rent_back else self.c["ata_rent_sol"])
        with self.lock:
            if mint not in self.positions:
                return None
            del self.positions[mint]
            self.balance += sol_out - self.tx_cost() + rent_back
            row = dict(id=None, strategy=pos["strategy"], mint=mint, symbol=pos["symbol"], opened=pos["opened"],
                       closed=time.time(), hold_s=round(time.time() - pos["opened"]), size_sol=pos["size_sol"],
                       tokens=pos["tokens"], entry_mark_sol=pos["entry_mark"], exit_mark_sol=mark, sol_out=sol_out,
                       fees_sol=fees, slippage_sol=pos["entry_slip"] + exit_slip, pnl_sol=pnl,
                       pnl_pct=pnl / pos["size_sol"] * 100, exit_reason=reason,
                       fill_source=pos["fill_source"] + "/" + src, meta=json.dumps(pos["meta"], default=str))
            cur = self.db.execute("INSERT INTO trades (run_id,strategy,mint,symbol,opened,closed,hold_s,size_sol,tokens,"
                                  "entry_mark_sol,exit_mark_sol,sol_out,fees_sol,slippage_sol,pnl_sol,pnl_pct,exit_reason,"
                                  "fill_source,meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (self.run_id,) + tuple(row[k] for k in CSV_FIELDS[1:]))
            row["id"] = cur.lastrowid
            self.db.commit()
            with open(self.csv_path, "a", newline="") as f:
                csv.writer(f).writerow([self.run_id] + [row[k] for k in CSV_FIELDS])
            self.closed.append(row)
        log.info("PAPER SELL [%s] %s reason=%s pnl=%+.5f SOL (%+.1f%%) hold=%ss fill=%s bal=%.4f",
                 pos["strategy"], pos["symbol"], reason, pnl, row["pnl_pct"], row["hold_s"], row["fill_source"], self.balance)
        return row

    def equity(self):
        with self.lock:
            unreal = sum(p["tokens"] * p["last_mark"] for p in self.positions.values())
            rent = len(self.positions) * self.c["ata_rent_sol"]
            return self.balance + unreal + rent, unreal

    def finish(self, summary):
        with self.lock:
            self.db.execute("UPDATE runs SET ended=?, end_balance=?, summary=? WHERE run_id=?",
                            (time.time(), self.balance, json.dumps(summary, default=str), self.run_id))
            self.db.commit()
