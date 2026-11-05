"""Quotex trade-history CSV importer + daily analytics (v0).

Real export columns: Info, Profit, ID, Open time, Open Price, Close Time, Close Price, Type, Amount, Income
- "Profit" is the payout percentage (e.g. "85%"), NOT money.
- "Income" is the TOTAL RETURN (stake + profit; 0 on a loss), so net profit = Income - Amount.
- A draw is a refund of the stake (Income == Amount), so its profit is 0 (confirmed by the owner).
- The file has no currency, no account type and no timezone: the upload form supplies them.
  Timezone defaults to IST (confirmed by the owner).
"""
from __future__ import annotations

import sys
import pandas as pd

EXPECTED = ["Info", "Profit", "ID", "Open time", "Open Price",
            "Close Time", "Close Price", "Type", "Amount", "Income"]
DEFAULT_TZ = "Asia/Kolkata"   # IST


class ImportProblem(ValueError):
    """The file cannot be imported at all (wrong file, missing columns)."""


def parse_quotex_csv(source, account_type: str, currency: str = "INR",
                     source_tz: str = DEFAULT_TZ):
    """Return (trades, warnings). `trades` mirrors the `trades` table, minus user_id and import_id.

    Timestamps in the file are taken to be in `source_tz` (the user's Quotex platform timezone)
    and are stored as UTC.
    """
    if account_type not in ("live", "demo"):
        raise ImportProblem("account_type must be 'live' or 'demo'")
    raw = pd.read_csv(source, dtype=str)
    raw.columns = [c.strip() for c in raw.columns]
    missing = [c for c in EXPECTED if c not in raw.columns]
    if missing:
        raise ImportProblem(f"This does not look like a Quotex trade-history export; missing: {missing}")

    def when(col):
        t = pd.to_datetime(raw[col], format="%Y-%m-%d %H:%M:%S", errors="coerce")
        return t.dt.tz_localize(source_tz, ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC")

    df = pd.DataFrame({
        "external_id": raw["ID"].str.strip(),
        "asset": raw["Info"].str.replace(r"\s*\(OTC\)\s*$", "", regex=True).str.strip(),
        "is_otc": raw["Info"].str.contains(r"\(OTC\)", regex=True),
        "payout_pct": pd.to_numeric(raw["Profit"].str.rstrip("%"), errors="coerce"),
        "direction": raw["Type"].str.strip().str.lower().map({"up": "up", "down": "down"}),
        "amount": pd.to_numeric(raw["Amount"], errors="coerce"),
        "returned": pd.to_numeric(raw["Income"], errors="coerce"),
        "open_price": pd.to_numeric(raw["Open Price"], errors="coerce"),
        "close_price": pd.to_numeric(raw["Close Price"], errors="coerce"),
        "opened_at": when("Open time"),
        "closed_at": when("Close Time"),
    })

    warnings = []
    required = ["external_id", "direction", "amount", "returned", "opened_at", "closed_at"]
    bad = df[required].isna().any(axis=1) | (df["closed_at"] < df["opened_at"])
    if bad.any():
        warnings.append(f"Skipped {int(bad.sum())} unreadable row(s) (file lines "
                        f"{', '.join(str(i + 2) for i in df.index[bad][:10])})")
        df = df[~bad]
    dupes = df.duplicated("external_id")
    if dupes.any():
        warnings.append(f"Ignored {int(dupes.sum())} repeated trade ID(s) inside the file")
        df = df[~dupes]

    df["profit"] = df["returned"] - df["amount"]            # NET profit; a loss is -stake
    df["result"] = pd.cut(df["profit"], [-float("inf"), -1e-9, 1e-9, float("inf")],
                          labels=["loss", "draw", "win"]).astype(str)
    df["account_type"], df["currency"] = account_type, currency
    return df.drop(columns="returned").reset_index(drop=True), warnings


def daily_stats(trades: pd.DataFrame, tz: str = DEFAULT_TZ) -> pd.DataFrame:
    """One row per account type, currency and local day. Feeds the P&L heatmap and daily accuracy."""
    t = trades.assign(day=trades["closed_at"].dt.tz_convert(tz).dt.date)
    out = t.groupby(["account_type", "currency", "day"]).agg(
        trades=("external_id", "count"),
        wins=("result", lambda s: int((s == "win").sum())),
        losses=("result", lambda s: int((s == "loss").sum())),
        pnl=("profit", "sum"),
        staked=("amount", "sum"),
    ).reset_index()
    decided = (out["wins"] + out["losses"]).where(lambda s: s > 0)  # draws don't count either way
    out["accuracy_pct"] = (100 * out["wins"] / decided).round(1)
    return out


def summary(trades: pd.DataFrame) -> dict:
    """Overall numbers. Accuracy is given per trade AND per distinct entry, because many
    traders open several identical trades in the same second."""
    decided = trades[trades["result"] != "draw"]
    entries = trades.groupby(["asset", "direction", "opened_at"])["profit"].sum()
    decided_entries = entries[entries != 0]
    pct = lambda a, b: round(100 * float(a) / b, 1) if b else None
    return {
        "trades": len(trades),
        "wins": int((trades["result"] == "win").sum()),
        "losses": int((trades["result"] == "loss").sum()),
        "draws": int((trades["result"] == "draw").sum()),
        "accuracy_pct": pct((decided["result"] == "win").sum(), len(decided)),
        "distinct_entries": len(entries),
        "entry_accuracy_pct": pct((decided_entries > 0).sum(), len(decided_entries)),
        "net_pnl": float(trades["profit"].sum()),
        "staked": float(trades["amount"].sum()),
    }


if __name__ == "__main__":      # python importer.py export.csv live INR Asia/Kolkata
    path, acct = sys.argv[1], sys.argv[2]
    cur = sys.argv[3] if len(sys.argv) > 3 else "INR"
    tz = sys.argv[4] if len(sys.argv) > 4 else DEFAULT_TZ
    trades, warns = parse_quotex_csv(path, acct, cur, tz)
    print("warnings:", warns or "none")
    print(summary(trades))
    print(daily_stats(trades, tz).to_string(index=False))
