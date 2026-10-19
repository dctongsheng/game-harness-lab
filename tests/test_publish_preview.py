"""The installer publisher must bind verification to the requested successful build."""
from copy import deepcopy
import unittest

from scripts.publish_genshin_preview import verified_source


class PreviewProvenanceTests(unittest.TestCase):
    def test_source_and_install_checks_are_required(self):
        request = {"build_run_id": 12, "platform_version": "0.3.0"}
        run = {"id": 12, "head_sha": "a" * 40, "head_branch": "hhr-yuanshen",
               "path": ".github/workflows/genshin.yml", "repository": {"full_name": "owner/repo"},
               "conclusion": "success", "status": "completed"}
        verification = {"source_commit": "a" * 40, "installer_sha256": "b" * 64,
                        "platform_version": "0.3.0", "installation_verified": True,
                        "uninstall_verified": True, "frozen_runtime_verified": True,
                        "chinese_decoder_verified": True, "desktop_controls_verified": True,
                        "genshin_live_verified": False, "microphone_verified": False,
                        "asr_accuracy_verified": False}
        self.assertEqual(verified_source(run, verification, request, "b" * 64, "owner/repo"), "a" * 40)
        cases = [
            ("run", "head_branch", "main"), ("run", "conclusion", "failure"),
            ("run", "path", ".github/workflows/other.yml"), ("run", "id", 99),
            ("run", "repository", {"full_name": "other/repo"}),
            ("verification", "source_commit", "c" * 40),
            ("verification", "platform_version", "0.2.0"),
            ("verification", "installer_sha256", "d" * 64),
            ("verification", "installation_verified", False),
            ("verification", "genshin_live_verified", True),
            ("verification", "microphone_verified", None),
        ]
        for section, key, value in cases:
            with self.subTest(section=section, key=key):
                payload = deepcopy({"run": run, "verification": verification})
                payload[section][key] = value
                with self.assertRaises(ValueError):
                    verified_source(payload["run"], payload["verification"], request, "b" * 64, "owner/repo")


if __name__ == "__main__":
    unittest.main()
