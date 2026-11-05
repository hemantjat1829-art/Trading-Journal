import io
import pytest
from importer import ImportProblem, daily_stats, parse_quotex_csv, summary

HEADER = '"Info","Profit","ID","Open time","Open Price","Close Time","Close Price","Type","Amount","Income"\n'


def rows(*lines):
    return io.StringIO(HEADER + "\n".join(lines) + "\n")


def row(i, income, amount=200, pay="85%", opened="2026-11-03 11:24:02",
        closed="2026-11-03 11:26:00", asset="NZD/JPY (OTC)", kind="Down"):
    return (f'"{asset}","{pay}","id-{i}","{opened}","89.477","{closed}","89.458",'
            f'"{kind}","{amount}","{income}"')


def parse(*lines, **kw):
    return parse_quotex_csv(rows(*lines), kw.pop("account_type", "live"), **kw)


def test_net_profit_is_income_minus_stake():
    t, _ = parse(row(1, 370), row(2, 0))
    assert list(t["profit"]) == [170, -200]
    assert list(t["result"]) == ["win", "loss"]


def test_identical_simultaneous_trades_are_all_kept():
    t, w = parse(*[row(i, 370) for i in range(4)])
    assert len(t) == 4 and not w


def test_repeated_trade_ids_inside_a_file_are_dropped():
    t, w = parse(row(1, 370), row(1, 370))
    assert len(t) == 1 and "repeated" in w[0]


def test_wrong_file_is_rejected():
    with pytest.raises(ImportProblem):
        parse_quotex_csv(io.StringIO("a,b\n1,2\n"), "live")


def test_unreadable_row_is_skipped_with_warning():
    t, w = parse(row(1, 370), row(2, 370, opened="not a date"))
    assert len(t) == 1 and "Skipped 1" in w[0]


def test_otc_flag_and_asset_name():
    t, _ = parse(row(1, 370), row(2, 370, asset="EUR/USD"))
    assert list(t["asset"]) == ["NZD/JPY", "EUR/USD"] and list(t["is_otc"]) == [True, False]


def test_timestamps_are_converted_from_platform_timezone_to_utc():
    t, _ = parse(row(1, 370), source_tz="Asia/Kolkata")
    assert str(t["opened_at"][0]) == "2026-11-03 05:54:02+00:00"


def test_day_boundary_uses_the_users_timezone():
    # 00:10 IST on 4 Nov is still 3 Nov in UTC, but the user's day is 4 Nov
    t, _ = parse(row(1, 370, opened="2026-11-04 00:08:00", closed="2026-11-04 00:10:00"))
    assert str(daily_stats(t, "Asia/Kolkata")["day"][0]) == "2026-11-04"
    assert str(daily_stats(t, "UTC")["day"][0]) == "2026-11-03"


def test_draws_are_not_counted_as_wins_or_losses():
    t, _ = parse(row(1, 370), row(2, 200), row(3, 0))      # win, draw, loss
    s = summary(t)
    assert (s["wins"], s["draws"], s["losses"], s["accuracy_pct"]) == (1, 1, 1, 50.0)


def test_entry_accuracy_collapses_identical_trades():
    # four identical winning trades in one second, then one losing trade
    t, _ = parse(*[row(i, 370) for i in range(4)],
                 row(9, 0, opened="2026-11-03 12:00:00", closed="2026-11-03 12:02:00"))
    s = summary(t)
    assert s["accuracy_pct"] == 80.0 and s["entry_accuracy_pct"] == 50.0


def test_bad_account_type_rejected():
    with pytest.raises(ImportProblem):
        parse(row(1, 370), account_type="paper")


def test_default_timezone_is_ist():
    from importer import DEFAULT_TZ
    t, _ = parse(row(1, 370))                       # no source_tz given
    assert DEFAULT_TZ == "Asia/Kolkata"
    assert str(t["opened_at"][0]) == "2026-11-03 05:54:02+00:00"
