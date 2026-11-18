"""Verify the default-off proprietary-component acceptance without vendor access."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


consent = load("proprietary_consent_under_test", "client/proprietary_consent.py")
runtime = load("consent_runtime_under_test", "client/standalone.py")
setup = load("consent_setup_under_test", "client/standalone-setup.py")
gameinput = load("consent_gameinput_under_test", "client/standalone-gameinput.py")


class ProprietaryConsentTests(unittest.TestCase):
    def write_eula(self, root, accepted):
        contents = (ROOT / "EULA.md").read_text()
        if accepted:
            contents = contents.replace(
                "PROPRIETARY_COMPONENTS_ACCEPTED = false",
                "PROPRIETARY_COMPONENTS_ACCEPTED = true",
            )
        (root / "EULA.md").write_text(contents)

    def test_project_default_and_copied_default_are_disabled(self):
        self.assertFalse(consent.acceptance_status(ROOT)[0])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_eula(root, accepted=False)
            self.assertFalse(consent.acceptance_status(root)[0])
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                consent.require_acceptance(root)

    def test_explicit_user_setting_enables_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_eula(root, accepted=True)
            self.assertEqual(consent.acceptance_status(root), (True, ""))
            consent.require_acceptance(root)

    def test_missing_stale_duplicate_and_linked_eulas_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertFalse(consent.acceptance_status(root)[0])
            self.write_eula(root, accepted=True)
            eula = root / "EULA.md"
            original = eula.read_text()
            eula.write_text(original.replace("EULA_VERSION = 1", "EULA_VERSION = 0"))
            self.assertFalse(consent.acceptance_status(root)[0])
            eula.write_text(original + "\nPROPRIETARY_COMPONENTS_ACCEPTED = true\n")
            self.assertFalse(consent.acceptance_status(root)[0])
            eula.unlink()
            target = root / "user-eula"
            target.write_text(original)
            eula.symlink_to(target)
            self.assertFalse(consent.acceptance_status(root)[0])

    def test_direct_runtime_and_component_acquisition_refuse_before_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_eula(root, accepted=False)
            with patch.object(runtime, "ROOT", root), \
                    patch.object(runtime.subprocess, "Popen", side_effect=AssertionError("helper started")):
                with self.assertRaisesRegex(RuntimeError, "disabled"):
                    with runtime.account(root / "game"):
                        self.fail("disabled account session entered")
                with patch.object(runtime, "prerequisites", side_effect=AssertionError("runtime prepared")):
                    with self.assertRaisesRegex(RuntimeError, "disabled"):
                        runtime.prepare(root / "game")

            with patch.object(setup, "ROOT", root), patch.object(setup, "THREADING", root / "runtime/threading.dll"), \
                    patch.object(setup.subprocess, "run", side_effect=AssertionError("network request")):
                with self.assertRaisesRegex(RuntimeError, "disabled"):
                    setup.acquire_threading()
                self.assertFalse((root / "downloads").exists())

            with patch.object(gameinput, "ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "disabled"):
                    gameinput.payload(root / "missing.msi")


if __name__ == "__main__":
    unittest.main()
