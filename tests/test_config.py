import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import yaml

from scripts.adaptive_learning import build_research_plan, open_database


EXPECTED_WATCHLIST = {
    "Siemens Digital Industries Software", "Ansys", "Dassault Systèmes", "Hexagon AB",
    "Altair Engineering", "Cadence Design Systems", "Autodesk", "MSC Software", "COMSOL",
    "ESI Group", "NUMECA International", "Siemens Energy", "Baker Hughes", "Mitsubishi Power",
    "Solar Turbines", "Chromalloy", "Quest Global", "Cyient", "Capgemini Engineering",
    "GE Aerospace", "Rolls-Royce", "Pratt & Whitney", "Safran Aircraft Engines",
    "MTU Aero Engines", "CFM International", "Honeywell Aerospace", "RTX", "GKN Aerospace",
    "Mitsubishi Heavy Industries", "Kawasaki Heavy Industries", "SUBARU",
    "Kawasaki Heavy Industries Aerospace Systems Company", "Hanwha Aerospace",
    "Aero Engine Corporation of China (AECC)", "Aviation Industry Corporation of China (AVIC)",
    "Airbus", "Boeing", "Lockheed Martin", "Northrop Grumman", "BAE Systems", "Leonardo",
    "Avio Aero", "Liebherr-Aerospace", "Woodward", "Parker Aerospace", "Howmet Aerospace",
    "Precision Castparts", "ATI (ATI Inc.)", "Spirit AeroSystems",
}


class ConfigTests(unittest.TestCase):
    def test_focus_and_watchlist_are_complete_and_cover_every_company_weekly(self):
        config = yaml.safe_load(Path("config/sources.yaml").read_text(encoding="utf-8"))
        sources = config["research"]["must_check_sources"]
        self.assertEqual({source["name"] for source in sources}, EXPECTED_WATCHLIST)
        self.assertTrue(all(source.get("domains") for source in sources))
        self.assertEqual(sum(area["share"] for area in config["research"]["priority_areas"]), 100)
        self.assertIn("drone", config["research"]["exclude_terms"])
        with tempfile.TemporaryDirectory() as directory:
            connection = open_database(Path(directory) / "learning.sqlite3")
            try:
                active = set()
                for offset in range(7):
                    plan = build_research_plan(connection, config, date(2026, 7, 13) + timedelta(days=offset))
                    self.assertEqual(len(plan["watchlist"]["active_sources"]), 7)
                    active.update(source["name"] for source in plan["watchlist"]["active_sources"])
            finally:
                connection.close()
        self.assertEqual(active, EXPECTED_WATCHLIST)


if __name__ == "__main__":
    unittest.main()
