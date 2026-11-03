"""One-time, idempotent PAPER bankroll resets (runner/paper_resets.toml). Called by runner.py after load_state().

For each reset id not yet in data/paper_resets_applied.json:
  * archive the bot's current run: per-agent W/L/net rows appended to legacy_agents.json (the app's "Before the merge" card),
    plus a JSON summary in data/archive/<id>.json; the old run's trades/fills/equity stay in the bot's sqlite db
  * start a new run id for that bot with balance = start = balance_sol, pnl/closed counters 0 and no daily-loss baseline;
    open positions, traded-mint memory and cooldowns are carried over unchanged
  * record the id (and log it). Paper only: refuses if the bot's config is not paper."""
import json, os, sqlite3, time, tomllib

APPLIED = "paper_resets_applied.json"


def _load(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default


def _dump(obj, p):
    tmp = p + ".tmp"
    json.dump(obj, open(tmp, "w"), default=str, ensure_ascii=False)
    os.replace(tmp, p)


def applied_ids(data):
    return set(_load(os.path.join(data, APPLIED), {}).get("applied", []))


def apply_resets(bot_dir, cfg_file, log=print, now=None):
    now = now or time.time()
    data = os.path.join(bot_dir, "data")
    if not os.path.exists(cfg_file):
        return []
    resets = tomllib.load(open(cfg_file, "rb")).get("reset") or []
    rec = _load(os.path.join(data, APPLIED), {"applied": [], "log": []})
    done = []
    for r in resets:
        rid, bot, bal = r["id"], r["bot"], float(r["balance_sol"])
        if rid in rec["applied"]:
            continue
        b = bot.lower()
        bcfg = tomllib.load(open(os.path.join(bot_dir, f"{b}_bot.toml"), "rb"))
        assert bcfg["general"]["paper"] is True, "paper only"
        run_f, acct_f = os.path.join(data, f"{b}_bot_run.txt"), os.path.join(data, f"{b}_bot_account.json")
        old_run = open(run_f).read().strip() if os.path.exists(run_f) else None
        acct = _load(acct_f, None)
        if acct is not None and acct.get("run") != old_run:
            acct = None                                   # account belongs to another run: nothing valid to carry over
        rows, summary = [], {"id": rid, "bot": bot, "old_run": old_run, "applied_at": now}
        dbf = os.path.join(data, f"{b}_bot.db")
        if old_run and os.path.exists(dbf):
            c = sqlite3.connect(dbf)
            try:
                for agent, n, w, net in c.execute("SELECT agent, COUNT(*), SUM(pnl_sol > 0), SUM(pnl_sol) FROM trades WHERE run=? GROUP BY agent ORDER BY SUM(pnl_sol) DESC", (old_run,)):
                    rows.append({"bot": r.get("label", f"{bot} before reset"), "agent": agent, "wins": int(w or 0), "losses": int(n - (w or 0)),
                                 "net": round(net or 0, 6), "decision": f"paper reset {rid}: run {old_run} archived, bankroll set to {bal:g} SOL"})
            except sqlite3.OperationalError:
                pass
            c.close()
        if acct:
            summary.update(balance_before=acct.get("balance"), start_before=acct.get("start"), pnl_before=acct.get("pnl"), closed_before=acct.get("closed_n"),
                           agent_pnl_before=acct.get("agent_pnl"), agent_n_before=acct.get("agent_n"), open_positions=len(acct.get("positions") or {}))
        summary["rows"] = rows
        os.makedirs(os.path.join(data, "archive"), exist_ok=True)
        _dump(summary, os.path.join(data, "archive", f"{rid}.json"))
        if rows:
            L = _load(os.path.join(data, "legacy_agents.json"), {"rows": [], "by_key": {}, "note": ""})
            L["rows"] = [x for x in L.get("rows", []) if not str(x.get("decision", "")).startswith(f"paper reset {rid}:")] + rows
            _dump(L, os.path.join(data, "legacy_agents.json"))
        new_run = time.strftime(f"{bot}%Y%m%d-%H%M%S", time.localtime(now))
        keep = acct or {}
        new = {"balance": bal, "start": bal, "positions": keep.get("positions") or {}, "traded": keep.get("traded") or [], "closed_n": 0, "pnl": 0.0,
               "day": None, "day_eq": None, "cooldown": keep.get("cooldown") or {}, "agent_pnl": {}, "agent_n": {}, "run": new_run}
        if new["positions"]:
            new["balance"] = bal - sum(float(p.get("size_sol") or 0) for p in new["positions"].values())   # open stakes stay funded
        _dump(new, acct_f)
        open(run_f, "w").write(new_run)
        summary["new_run"] = new_run
        rec["applied"].append(rid)
        rec.setdefault("log", []).append({k: summary.get(k) for k in ("id", "bot", "old_run", "new_run", "applied_at", "balance_before", "closed_before", "open_positions")})
        _dump(rec, os.path.join(data, APPLIED))
        log(f"paper reset {rid}: {bot} run {old_run} archived ({sum(x['wins'] + x['losses'] for x in rows)} closed trades), "
            f"new run {new_run} with {bal:g} SOL paper balance, {len(new['positions'])} open positions kept")
        done.append(rid)
    return done
