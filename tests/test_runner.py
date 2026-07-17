import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "ops" / "run-collector.sh"


class RunnerTests(unittest.TestCase):
    def test_rejects_non_numeric_scout_timeouts_before_side_effects(self):
        for variable in ("DAILY_SIGNAL_SCOUT_TIMEOUT", "DAILY_SIGNAL_SCOUT_REPAIR_TIMEOUT"):
            with self.subTest(variable=variable):
                environment = os.environ.copy()
                environment[variable] = "not-a-duration"
                result = subprocess.run(
                    ["bash", str(RUNNER), "digest"],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn(f"{variable} must be a positive integer", result.stderr)

    def test_scout_uses_one_local_agent_with_a_wall_clock_limit(self):
        runner = RUNNER.read_text(encoding="utf-8")

        self.assertIn("timeout --foreground --kill-after=30s", runner)
        self.assertIn('--name "$active_scout_container" openclaw-cli agent', runner)
        self.assertIn("      --local \\", runner)
        self.assertIn("trap cleanup_scout_container EXIT", runner)


if __name__ == "__main__":
    unittest.main()
