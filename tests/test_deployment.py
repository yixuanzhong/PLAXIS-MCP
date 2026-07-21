from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from plaxis_mcp.deployment import (
    CURRENT_312,
    LEGACY_38,
    CredentialStoreUnavailable,
    DeploymentError,
    EndpointProfile,
    PlaxisInstallation,
    ProfileError,
    RuntimeProfileError,
    credential_target_for,
    credentials_main,
    discover_plaxis_installations,
    load_profile,
    runtime_profile_for_python,
    select_installation,
    validate_worker_attestation,
    write_role_profiles,
)


class MemoryStore:
    def __init__(self):
        self.values: dict[str, str] = {}

    def read(self, target: str) -> str:
        if target not in self.values:
            raise CredentialStoreUnavailable("not found")
        return self.values[target]

    def write(self, target: str, password: str) -> None:
        self.values[target] = password

    def delete(self, target: str) -> None:
        self.values.pop(target, None)


class DeploymentTests(unittest.TestCase):
    def test_exact_certified_python_versions_fail_closed(self):
        self.assertEqual(runtime_profile_for_python("Python 3.12.3").name, "current-312")
        self.assertEqual(runtime_profile_for_python((3, 8, 10)).name, "legacy-38")
        self.assertEqual(runtime_profile_for_python("3.7.4").name, "legacy-37")
        with self.assertRaises(RuntimeProfileError):
            runtime_profile_for_python("3.12.4")

    def test_attestation_binds_protocol_role_and_profile(self):
        evidence = {
            "protocol": 1,
            "role": "output",
            "worker_python_version": "3.8.10",
            "runtime_profile": "legacy-38",
            "product_version": "PLAXIS V22",
        }
        self.assertEqual(
            validate_worker_attestation(evidence, "output", expected_runtime_profile="legacy-38"),
            LEGACY_38,
        )
        with self.assertRaises(RuntimeProfileError):
            validate_worker_attestation(evidence, "input", expected_runtime_profile="legacy-38")
        with self.assertRaises(RuntimeProfileError):
            validate_worker_attestation(evidence, "output", expected_runtime_profile="current-312")

    @staticmethod
    def _fake_installation(root: Path) -> PlaxisInstallation:
        """Build a real on-disk product root so binding checks can run."""

        (root / "python").mkdir(parents=True, exist_ok=True)
        interpreter = root / "python" / "python.exe"
        interpreter.touch()
        return PlaxisInstallation(
            root=root.resolve(),
            worker_python=interpreter.resolve(),
            runtime_profile=LEGACY_38,
        )

    def test_profiles_are_secret_free_and_literal_loopback_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            installation = self._fake_installation(Path(temporary) / "V22")
            written = write_role_profiles(Path(temporary), installation)
            input_profile = load_profile(written["input"])
            output_profile = load_profile(written["output"])
            self.assertEqual(input_profile.role, "input")
            self.assertEqual(output_profile.role, "output")
            self.assertNotEqual(input_profile.credential_target, output_profile.credential_target)
            self.assertNotIn("password =", written["input"].read_text(encoding="utf-8").lower())
        with self.assertRaises(ProfileError):
            EndpointProfile(
                role="input", runtime_profile="legacy-38", worker_python=Path("C:/python.exe"),
                installation_root=Path("C:/PLAXIS/V22"),
                host="localhost", port=10000,
                credential_target=credential_target_for(Path("C:/PLAXIS/V22"), "input"),
            )

    def test_profile_credential_target_must_be_derived_from_installation_root(self):
        """A hand-edited profile must not keep one install's password target."""

        with self.assertRaises(ProfileError):
            EndpointProfile(
                role="input", runtime_profile="legacy-38", worker_python=Path("C:/python.exe"),
                installation_root=Path("C:/PLAXIS/V22"),
                host="127.0.0.1", port=10000,
                credential_target=credential_target_for(Path("C:/PLAXIS/OTHER"), "input"),
            )

    def test_worker_python_must_be_linked_to_the_installation_root(self):
        """The stored password must not reach an arbitrary executable."""

        with tempfile.TemporaryDirectory() as temporary:
            installation = self._fake_installation(Path(temporary) / "V22")
            written = write_role_profiles(Path(temporary), installation)

            attacker = Path(temporary) / "evil.exe"
            attacker.touch()
            profile_file = written["input"]
            redirected = profile_file.read_text(encoding="utf-8").replace(
                json.dumps(str(installation.worker_python)), json.dumps(str(attacker))
            )
            profile_file.write_text(redirected, encoding="utf-8")

            # The credential target is untouched, so only the interpreter
            # allowlist can catch this.
            with self.assertRaises(ProfileError):
                load_profile(profile_file)

    def test_profile_with_secret_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile_file = Path(temporary) / "bad.toml"
            profile_file.write_text(
                """schema_version = 1
role = \"input\"
runtime_profile = \"legacy-38\"
worker_python = \"C:/python.exe\"
host = \"127.0.0.1\"
port = 10000
credential_target = \"Caros/PLAXIS-MCP/test/input\"
password = \"do-not-store\"
""",
                encoding="utf-8",
            )
            with self.assertRaises(ProfileError):
                load_profile(profile_file)

    def test_legacy_37_requires_explicit_security_acceptance(self):
        common = {
            "role": "input",
            "runtime_profile": "legacy-37",
            "worker_python": Path("C:/python.exe"),
            "installation_root": Path("C:/PLAXIS/V21"),
            "host": "127.0.0.1",
            "port": 10000,
            "credential_target": credential_target_for(Path("C:/PLAXIS/V21"), "input"),
        }
        with self.assertRaises(ProfileError):
            EndpointProfile(**common)
        accepted = EndpointProfile(**common, legacy_security_accepted=True)
        self.assertTrue(accepted.legacy_security_accepted)

    def test_discovery_accepts_only_certified_python_and_requires_choice_for_multiple(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "python").mkdir()
            (root / "python" / "python.exe").touch()
            discovered = discover_plaxis_installations(
                registry_roots=[(root, "HKLM\\Test", "PLAXIS test")],
                python_version_probe=lambda _: (3, 12, 3),
            )
            self.assertEqual(select_installation(discovered).runtime_profile, CURRENT_312)
            with self.assertRaises(DeploymentError):
                select_installation([discovered[0], discovered[0]])

    def test_hidden_input_credential_command_does_not_take_password_argument(self):
        target = "Caros/PLAXIS-MCP/test/input"
        store = MemoryStore()
        with patch("plaxis_mcp.deployment.getpass", side_effect=["secret-value", "secret-value"]):
            self.assertEqual(credentials_main(["set", "--target", target], store=store), 0)
        self.assertEqual(store.read(target), "secret-value")

    def test_credential_targets_are_role_specific(self):
        root = Path("C:/PLAXIS/V22")
        self.assertNotEqual(credential_target_for(root, "input"), credential_target_for(root, "output"))

    def test_packaging_foundations_are_present(self):
        root = Path(__file__).resolve().parents[1]
        spec = (root / "packaging" / "plaxis-mcp.spec").read_text(encoding="utf-8")
        build = (root / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
        installer = (root / "scripts" / "install_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("COLLECT(", spec)
        self.assertIn("worker_runtime.py", spec)
        self.assertIn("signtool", build.lower())
        self.assertIn("Caros\\PLAXIS-MCP", installer)
        self.assertIn("Get-AuthenticodeSignature", installer)
        self.assertIn("artifact-manifest.json", installer)
        self.assertIn("accept-legacy-37", installer)


if __name__ == "__main__":
    unittest.main()
