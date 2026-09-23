from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from core.analysis.sector_v2 import build_context


def make_context_fixture(days: int = 70, codes: int = 6) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    dates = pd.bdate_range("2026-01-05", periods=days).strftime("%Y%m%d")
    names = [f"6000{index:02d}.SH" for index in range(codes)]
    rows = []
    for code_index, code in enumerate(names):
        for date_index, date in enumerate(dates):
            close = 10.0 * np.exp((0.001 + code_index * 0.0001) * date_index)
            rows.append(
                {
                    "code": code,
                    "date": date,
                    "adjusted_close": close,
                    "amount_cny": 30_000_000.0 + code_index * 1_000_000.0,
                    "mark_only": False,
                }
            )
    memberships = pd.DataFrame(
        [
            {
                "namespace": "SW_L1",
                "sector_id": "BANK",
                "code": code,
                "valid_from": dates[0],
                "valid_to": None,
                "observed_at": "2026-04-30T16:10:00+08:00",
                "history_mode": "RECONSTRUCTED_PIT",
            }
            for code in names
        ]
    )
    return pd.DataFrame(rows), memberships, names


class SectorContextTest(unittest.TestCase):
    def test_builds_market_and_sector_context_with_explicit_coverage(self) -> None:
        panel, memberships, universe = make_context_fixture()

        context = build_context(panel, memberships, as_of=panel["date"].max(), universe=universe)

        latest_market = context.market.iloc[-1]
        latest_sector = context.sectors.query("sector_id == 'BANK'").iloc[-1]
        self.assertEqual(latest_market["status"], "OK")
        self.assertEqual(latest_sector["status"], "OK")
        self.assertEqual(int(latest_market["member_count"]), 6)
        self.assertEqual(int(latest_market["covered_count"]), 6)
        self.assertAlmostEqual(float(latest_market["coverage"]), 1.0)
        self.assertTrue(0.0 <= float(latest_sector["breadth20"]) <= 1.0)
        self.assertEqual(set(context.assignments["sector_id"]), {"BANK"})

    def test_unexplained_market_gap_above_ten_percent_is_unavailable(self) -> None:
        panel, memberships, universe = make_context_fixture()
        latest = panel["date"].max()
        panel = panel.loc[~((panel["code"] == universe[-1]) & (panel["date"] == latest))]

        context = build_context(panel, memberships, as_of=latest, universe=universe)
        row = context.market.loc[context.market["date"] == latest].iloc[0]

        self.assertEqual(row["status"], "UNAVAILABLE")
        self.assertEqual(row["reason_code"], "INSUFFICIENT_MEMBER_COVERAGE")
        self.assertAlmostEqual(float(row["coverage"]), 5 / 6)
        self.assertTrue(pd.isna(row["index_level"]))

    def test_sector_below_minimum_member_count_is_reported_not_invented(self) -> None:
        panel, memberships, universe = make_context_fixture(codes=4)

        context = build_context(panel, memberships, as_of=panel["date"].max(), universe=universe)
        latest = context.sectors.iloc[-1]

        self.assertEqual(latest["status"], "UNAVAILABLE")
        self.assertEqual(latest["reason_code"], "INSUFFICIENT_SECTOR_MEMBERS")
        self.assertTrue(pd.isna(latest["index_level"]))
        self.assertTrue(pd.isna(latest["breadth20"]))

    def test_multi_membership_is_preserved_for_concept_namespaces(self) -> None:
        panel, memberships, universe = make_context_fixture()
        concepts = pd.DataFrame(
            [
                {
                    "namespace": "CONCEPT",
                    "sector_id": sector_id,
                    "code": universe[0],
                    "valid_from": panel["date"].min(),
                    "valid_to": None,
                    "history_mode": "OBSERVED_PIT",
                }
                for sector_id in ("DIVIDEND", "LARGE_CAP")
            ]
        )

        context = build_context(
            panel,
            pd.concat([memberships, concepts], ignore_index=True),
            as_of=panel["date"].max(),
            universe=universe,
            min_sector_members=1,
        )

        assigned = context.assignments.query("namespace == 'CONCEPT' and code == @universe[0]")
        self.assertEqual(set(assigned["sector_id"]), {"DIVIDEND", "LARGE_CAP"})

    def test_observed_membership_is_not_visible_before_observation_date(self) -> None:
        panel, memberships, universe = make_context_fixture(days=25)
        dates = sorted(panel["date"].unique())
        memberships.loc[memberships["code"] == universe[0], "history_mode"] = "OBSERVED_PIT"
        memberships.loc[memberships["code"] == universe[0], "observed_at"] = dates[-2]

        context = build_context(panel, memberships, as_of=dates[-1], universe=universe)
        stock = context.assignments.loc[context.assignments["code"] == universe[0]]

        self.assertEqual(stock["date"].min(), dates[-2])

    def test_sector_return_uses_membership_effective_on_previous_session(self) -> None:
        panel, memberships, universe = make_context_fixture(days=25)
        dates = sorted(panel["date"].unique())
        entrant = universe[-1]
        memberships.loc[memberships["code"] == entrant, "valid_from"] = dates[-1]
        entrant_return = 0.50
        previous = panel.loc[(panel["code"] == entrant) & (panel["date"] == dates[-2]), "adjusted_close"].iloc[0]
        panel.loc[(panel["code"] == entrant) & (panel["date"] == dates[-1]), "adjusted_close"] = previous * (1 + entrant_return)

        context = build_context(
            panel,
            memberships,
            as_of=dates[-1],
            universe=universe,
            min_sector_members=5,
        )
        sector = context.sectors.query("sector_id == 'BANK' and date == @dates[-1]").iloc[0]
        expected_codes = universe[:-1]
        latest_rows = panel.loc[(panel["date"] == dates[-1]) & panel["code"].isin(expected_codes)]
        previous_rows = panel.loc[(panel["date"] == dates[-2]) & panel["code"].isin(expected_codes)]
        expected = (latest_rows.set_index("code")["adjusted_close"] / previous_rows.set_index("code")["adjusted_close"] - 1).mean()

        self.assertEqual(int(sector["member_count"]), 5)
        self.assertAlmostEqual(float(sector["simple_return"]), float(expected), places=12)
        self.assertTrue(bool(sector["membership_changed"]))

    def test_calendar_session_with_no_rows_remains_in_coverage_audit(self) -> None:
        panel, memberships, universe = make_context_fixture(days=25)
        sessions = sorted(panel["date"].unique())
        panel = panel.loc[panel["date"] != sessions[-1]]

        context = build_context(
            panel,
            memberships,
            as_of=sessions[-1],
            universe=universe,
            sessions=sessions,
        )
        latest = context.market.loc[context.market["date"] == sessions[-1]].iloc[0]

        self.assertEqual(latest["status"], "UNAVAILABLE")
        self.assertEqual(int(latest["covered_count"]), 0)
        self.assertEqual(float(latest["coverage"]), 0.0)

    def test_input_order_does_not_change_context(self) -> None:
        panel, memberships, universe = make_context_fixture()

        ordered = build_context(panel, memberships, as_of=panel["date"].max(), universe=universe)
        shuffled = build_context(
            panel.sample(frac=1.0, random_state=7),
            memberships.sample(frac=1.0, random_state=8),
            as_of=panel["date"].max(),
            universe=list(reversed(universe)),
        )

        pd.testing.assert_frame_equal(ordered.market, shuffled.market)
        pd.testing.assert_frame_equal(ordered.sectors, shuffled.sectors)
        pd.testing.assert_frame_equal(ordered.assignments, shuffled.assignments)


if __name__ == "__main__":
    unittest.main()
