from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackagingContractTests(unittest.TestCase):
    def test_host_runtime_metadata_is_pinned(self):
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('version = "0.2.0"', metadata)
        self.assertIn('requires-python = ">=3.13,<3.14"', metadata)
        self.assertIn('"mcp[cli]==1.26.0"', metadata)
        self.assertNotIn("plxscripting", metadata.lower())
        package_init = (ROOT / "src" / "plaxis_mcp" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('__version__ = "0.2.0"', package_init)

    def test_client_examples_are_present_and_secret_free(self):
        example_names = {
            "caros-extensions.yaml",
            "codex-config.toml",
            "claude-code-mcp.json",
            "cursor-mcp.json",
            "vscode-mcp.json",
        }
        example_names.add("mcp-client-config.json")
        examples = ROOT / "examples"

        self.assertTrue(all((examples / name).is_file() for name in example_names))
        for name in example_names:
            content = (examples / name).read_text(encoding="utf-8")
            # Every client launches the packaged executable against a generated
            # profile. Endpoint configuration must not travel through the agent
            # config at all -- `serve --config` rejects those overrides.
            self.assertIn("plaxis-mcp.exe", content)
            self.assertIn("serve", content)
            self.assertIn("--config", content)
            self.assertNotIn("plaxis_mcp.server", content)
            self.assertNotIn("host.docker.internal", content)
            for forbidden in ("PLAXIS_ROLE", "PLAXIS_BUNDLE_PYTHON", "PLAXIS_INPUT_PASSWORD", "PLAXIS_OUTPUT_PASSWORD", "PLAXIS_PASSWORD"):
                self.assertNotIn(forbidden, content, "%s must not appear in %s" % (forbidden, name))

    def test_docker_is_not_a_supported_artifact(self):
        self.assertFalse((ROOT / "Dockerfile").exists())
        self.assertFalse((ROOT / "docker-compose.yml").exists())

    def test_smoke_diagnostic_does_not_load_vendor_sdk_in_host(self):
        smoke_test = (ROOT / "scripts" / "smoke_test.py").read_text(encoding="utf-8")

        self.assertIn("PLAXIS_BUNDLE_PYTHON", smoke_test)
        self.assertNotIn("PLAXIS_SCRIPTING_PATH", smoke_test)
        self.assertNotIn("load_new_server", smoke_test)


if __name__ == "__main__":
    unittest.main()
