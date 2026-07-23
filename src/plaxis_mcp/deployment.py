"""Windows deployment primitives for a split-runtime PLAXIS-MCP install.

The module has no MCP-SDK or vendor-package dependency. It discovers a
PLAXIS-owned interpreter, generates secret-free role profiles, and validates
the ABI attestation that the host receives before it sends a password.
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
from getpass import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


PROFILE_SCHEMA_VERSION = 1
WORKER_PROTOCOL_VERSION = 1
_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}
_SECRET_FIELD_MARKERS = ("password", "secret", "token", "credential_blob")

# One per-user location holds the generated profiles for every client on the
# machine, so Caros, Claude Code, Codex, Cursor and VS Code all launch from the
# same installation binding and the same Credential Manager entries. The name
# is historical -- it is where the first supported installer wrote -- and is
# kept because ``credential_target`` is derived from it.
INSTALL_DIRECTORY_PARTS = ("Caros", "PLAXIS-MCP")
PROFILE_DIRECTORY_NAME = "profiles"


class DeploymentError(RuntimeError):
    """A safe deployment or configuration failure."""


class RuntimeProfileError(DeploymentError):
    """The worker interpreter is not a certified PLAXIS runtime."""


class ProfileError(DeploymentError):
    """A role profile is malformed, incomplete, or contains a secret."""


class CredentialStoreUnavailable(DeploymentError):
    """Windows Credential Manager cannot be used in this environment."""


@dataclass(frozen=True)
class RuntimeProfile:
    """One exact supported PLAXIS bundle Python ABI."""

    name: str
    python_version: tuple[int, int, int]
    product_generation: str
    security_acceptance_required: bool = False

    @property
    def python_version_text(self) -> str:
        return ".".join(str(part) for part in self.python_version)


CURRENT_312 = RuntimeProfile("current-312", (3, 12, 3), "PLAXIS 2024.2 and newer")
LEGACY_38 = RuntimeProfile(
    "legacy-38", (3, 8, 10), "PLAXIS CONNECT Edition V22 through early 2024"
)
LEGACY_37 = RuntimeProfile(
    # V20 and V21 both bundle a Python 3.7 series interpreter and share this
    # profile. The version match stays exact: another 3.7 patch level is a
    # different, uncertified ABI and must fail closed rather than be assumed
    # compatible.
    "legacy-37", (3, 7, 4), "PLAXIS CONNECT Edition V20 and V21", security_acceptance_required=True
)
SUPPORTED_RUNTIME_PROFILES: tuple[RuntimeProfile, ...] = (CURRENT_312, LEGACY_38, LEGACY_37)


def _normalise_role(value: object) -> str:
    """Return a role value without importing the server's role enum."""

    candidate = getattr(value, "value", value)
    if not isinstance(candidate, str):
        raise DeploymentError("PLAXIS role must be input or output.")
    role = candidate.strip().lower()
    if role not in {"input", "output"}:
        raise DeploymentError("PLAXIS role must be input or output.")
    return role


def parse_python_version(value: str | Sequence[int]) -> tuple[int, int, int]:
    """Parse exactly a major/minor/patch version reported by a worker."""

    if isinstance(value, str):
        match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", value)
        if not match:
            raise RuntimeProfileError("Worker did not report a three-part Python version.")
        return tuple(int(part) for part in match.groups())  # type: ignore[return-value]
    if not isinstance(value, Sequence) or len(value) != 3 or any(isinstance(part, bool) for part in value):
        raise RuntimeProfileError("Worker did not report a three-part Python version.")
    try:
        return tuple(int(part) for part in value)  # type: ignore[return-value]
    except (TypeError, ValueError) as exc:
        raise RuntimeProfileError("Worker did not report a numeric Python version.") from exc


def runtime_profile_for_python(value: str | Sequence[int]) -> RuntimeProfile:
    """Return a certified profile, failing closed for an unrecognised ABI."""

    version = parse_python_version(value)
    for profile in SUPPORTED_RUNTIME_PROFILES:
        if profile.python_version == version:
            return profile
    supported = ", ".join(profile.python_version_text for profile in SUPPORTED_RUNTIME_PROFILES)
    raise RuntimeProfileError(
        "Unsupported PLAXIS bundle Python %s; certified versions are %s."
        % (".".join(str(part) for part in version), supported)
    )


def validate_worker_attestation(
    attestation: Mapping[str, Any],
    expected_role: object,
    *,
    expected_runtime_profile: str | None = None,
    protocol: int = WORKER_PROTOCOL_VERSION,
) -> RuntimeProfile:
    """Validate worker ABI/role evidence before the host sends credentials.

    The bridge calls this after checking JSON framing. An old worker that
    omits its ABI evidence is rejected rather than silently accepted.
    """

    if not isinstance(attestation, Mapping):
        raise RuntimeProfileError("PLAXIS worker attestation was not an object.")
    if attestation.get("protocol") != protocol:
        raise RuntimeProfileError("PLAXIS worker protocol did not match the host.")
    try:
        actual_role = _normalise_role(attestation.get("role"))
        selected_role = _normalise_role(expected_role)
    except DeploymentError as exc:
        raise RuntimeProfileError("PLAXIS worker role did not match the selected profile.") from exc
    if actual_role != selected_role:
        raise RuntimeProfileError("PLAXIS worker role did not match the selected profile.")
    version = attestation.get("worker_python_version", attestation.get("python_version"))
    if version is None:
        raise RuntimeProfileError("PLAXIS worker did not attest its bundled Python version.")
    profile = runtime_profile_for_python(version)
    declared = attestation.get("runtime_profile")
    if declared is not None and declared != profile.name:
        raise RuntimeProfileError("Worker runtime profile conflicts with its Python ABI.")
    if expected_runtime_profile is not None and expected_runtime_profile != profile.name:
        raise RuntimeProfileError("Worker Python ABI did not match the selected runtime profile.")
    product_version = attestation.get("product_version")
    if product_version is not None and not isinstance(product_version, str):
        raise RuntimeProfileError("PLAXIS worker product version was not text.")
    return profile


@dataclass(frozen=True)
class PlaxisInstallation:
    """A discovered product root linked to a certified bundle interpreter."""

    root: Path
    worker_python: Path
    runtime_profile: RuntimeProfile
    product_name: str = "PLAXIS"
    registry_key: str | None = None

    @property
    def installation_id(self) -> str:
        return installation_id_for(self.root)


@dataclass(frozen=True)
class EndpointProfile:
    """A pinned, secret-free Input or Output endpoint configuration.

    ``installation_root`` binds the profile to one discovered PLAXIS product
    root. ``credential_target`` is derived from that root, so a hand-edited
    profile cannot keep one installation's stored password while pointing
    ``worker_python`` somewhere else.
    """

    role: str
    runtime_profile: str
    worker_python: Path
    installation_root: Path
    host: str
    port: int
    credential_target: str
    connect_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 240.0
    legacy_security_accepted: bool = False
    schema_version: int = PROFILE_SCHEMA_VERSION

    @property
    def installation_id(self) -> str:
        return installation_id_for(self.installation_root)

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _normalise_role(self.role))
        try:
            object.__setattr__(self, "installation_root", Path(self.installation_root).expanduser().resolve())
            object.__setattr__(self, "worker_python", Path(self.worker_python).expanduser())
        except (OSError, TypeError, ValueError) as exc:
            raise ProfileError("PLAXIS profile paths are invalid.") from exc
        if not str(self.installation_root) or not str(self.worker_python):
            raise ProfileError("PLAXIS profile must name an installation root and a worker interpreter.")
        if self.host.strip().lower() not in _LOOPBACK_HOSTS:
            raise ProfileError("PLAXIS profiles may only use loopback endpoints.")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ProfileError("PLAXIS profile port must be an integer from 1 through 65535.")
        if not self.credential_target.startswith("Caros/PLAXIS-MCP/"):
            raise ProfileError("PLAXIS profile credential target is invalid.")
        # The credential target is derived, never free-form: this is what stops a
        # profile from redirecting one installation's stored password elsewhere.
        expected_target = "Caros/PLAXIS-MCP/%s/%s" % (self.installation_id, self.role)
        if self.credential_target != expected_target:
            raise ProfileError(
                "PLAXIS profile credential target is not bound to its installation root."
            )
        if self.connect_timeout_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise ProfileError("PLAXIS profile timeouts must be positive.")
        if self.runtime_profile not in {item.name for item in SUPPORTED_RUNTIME_PROFILES}:
            raise ProfileError("PLAXIS profile runtime is not certified.")
        if not isinstance(self.legacy_security_accepted, bool):
            raise ProfileError("PLAXIS legacy security acceptance must be true or false.")
        if self.runtime_profile == LEGACY_37.name and not self.legacy_security_accepted:
            raise ProfileError("The legacy-37 profile requires explicit organizational security acceptance.")
        if self.schema_version != PROFILE_SCHEMA_VERSION:
            raise ProfileError("PLAXIS profile schema version is not supported.")


def credential_target_for(installation: PlaxisInstallation | Path | str, role: object) -> str:
    """Create an installation- and role-specific target without a password."""

    normalised_role = _normalise_role(role)
    if isinstance(installation, PlaxisInstallation):
        installation_id = installation.installation_id
    else:
        installation_id = installation_id_for(installation)
    return "Caros/PLAXIS-MCP/%s/%s" % (installation_id, normalised_role)


def installation_id_for(root: Path | str) -> str:
    """Hash a product root the one way every caller must agree on."""

    normalised = str(Path(root).expanduser().resolve()).replace("/", "\\").lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:20]


def endpoint_profile_for(
    installation: PlaxisInstallation,
    role: object,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    connect_timeout_seconds: float = 5.0,
    request_timeout_seconds: float = 240.0,
    legacy_security_accepted: bool = False,
) -> EndpointProfile:
    normalised_role = _normalise_role(role)
    if port is None:
        port = 10000 if normalised_role == "input" else 10001
    return EndpointProfile(
        role=normalised_role,
        runtime_profile=installation.runtime_profile.name,
        worker_python=installation.worker_python,
        installation_root=installation.root,
        host=host,
        port=port,
        credential_target=credential_target_for(installation, normalised_role),
        connect_timeout_seconds=connect_timeout_seconds,
        request_timeout_seconds=request_timeout_seconds,
        legacy_security_accepted=legacy_security_accepted,
    )


def _toml_quote(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def profile_to_toml(profile: EndpointProfile) -> str:
    """Serialise the fixed schema. No profile field can be a password."""

    values = (
        ("schema_version", profile.schema_version),
        ("role", profile.role),
        ("runtime_profile", profile.runtime_profile),
        ("worker_python", profile.worker_python),
        ("installation_root", profile.installation_root),
        ("host", profile.host),
        ("port", profile.port),
        ("credential_target", profile.credential_target),
        ("connect_timeout_seconds", profile.connect_timeout_seconds),
        ("request_timeout_seconds", profile.request_timeout_seconds),
        ("legacy_security_accepted", profile.legacy_security_accepted),
    )
    lines = ["# Generated by plaxis-mcp-profiles. This file intentionally contains no password."]
    for key, value in values:
        if isinstance(value, (str, Path)):
            rendered = _toml_quote(value)
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = value
        lines.append("%s = %s" % (key, rendered))
    return "\n".join(lines) + "\n"


def write_profile(path: Path | str, profile: EndpointProfile) -> Path:
    """Atomically write a profile without widening the file target."""

    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=destination.parent, delete=False) as handle:
        handle.write(profile_to_toml(profile))
        temporary = Path(handle.name)
    try:
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _parse_toml_file(path: Path) -> Mapping[str, Any]:
    try:
        import tomllib
    except ImportError as exc:  # Host runtime is Python 3.13.
        raise ProfileError("Profile parsing requires the CPython 3.13 host runtime.") from exc
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError) as exc:
        raise ProfileError("PLAXIS profile could not be read.") from exc
    if not isinstance(data, Mapping):
        raise ProfileError("PLAXIS profile root must be an object.")
    return data


def validate_installation_binding(
    profile: EndpointProfile,
    *,
    candidate_pythons: Callable[[Path], Iterable[Path]] | None = None,
) -> None:
    """Prove the profile's interpreter really belongs to its installation root.

    ``EndpointProfile`` already derives ``credential_target`` from the root, so
    the remaining redirect is a hand-edited ``worker_python``. Without this
    check a profile could keep a valid credential target while naming an
    arbitrary executable, and the host would hand that process a real PLAXIS
    password. The allowlist is exactly what discovery would have linked to the
    same root, so an unreadable or non-PLAXIS root fails closed.
    """

    root = profile.installation_root
    if not root.is_dir():
        raise ProfileError("The PLAXIS installation root named by this profile does not exist.")
    try:
        interpreter = profile.worker_python.resolve()
    except OSError as exc:
        raise ProfileError("The PLAXIS worker interpreter path could not be resolved.") from exc
    if not interpreter.is_file():
        raise ProfileError("The PLAXIS worker interpreter named by this profile does not exist.")
    if interpreter not in linked_worker_pythons(root, candidate_pythons=candidate_pythons):
        raise ProfileError(
            "The PLAXIS worker interpreter is not linked to this profile's installation root."
        )


def linked_worker_pythons(
    root: Path | str,
    *,
    candidate_pythons: Callable[[Path], Iterable[Path]] | None = None,
) -> frozenset[Path]:
    """Return the resolved interpreters discovery would link to one product root.

    This is the interpreter allowlist. The bridge re-checks against it at every
    spawn, not only at profile load, because a worker is respawned after a fault
    long after the profile was read.
    """

    enumerate_candidates = candidate_pythons or _candidate_worker_pythons
    resolved: set[Path] = set()
    for candidate in enumerate_candidates(Path(root).expanduser()):
        try:
            resolved.add(Path(candidate).resolve())
        except OSError:
            continue
    return frozenset(resolved)


def load_profile(path: Path | str, *, verify_installation: bool = True) -> EndpointProfile:
    """Load generated TOML and refuse passwords, unknown fields, or redirects."""

    values = _parse_toml_file(Path(path).expanduser())
    for key in values:
        if any(marker in str(key).lower() for marker in _SECRET_FIELD_MARKERS):
            raise ProfileError("PLAXIS profile files must not contain credentials or secrets.")
    allowed = {
        "schema_version", "role", "runtime_profile", "worker_python", "installation_root", "host", "port",
        "credential_target", "connect_timeout_seconds", "request_timeout_seconds", "legacy_security_accepted",
    }
    if set(values) - allowed:
        raise ProfileError("PLAXIS profile contains unsupported settings.")
    required = {
        "schema_version", "role", "runtime_profile", "worker_python", "installation_root",
        "host", "port", "credential_target",
    }
    if required - set(values):
        raise ProfileError("PLAXIS profile is missing required settings.")
    try:
        profile = EndpointProfile(
            role=values["role"], runtime_profile=values["runtime_profile"],
            worker_python=Path(values["worker_python"]),
            installation_root=Path(values["installation_root"]),
            host=values["host"], port=values["port"],
            credential_target=values["credential_target"],
            connect_timeout_seconds=values.get("connect_timeout_seconds", 5.0),
            request_timeout_seconds=values.get("request_timeout_seconds", 240.0),
            legacy_security_accepted=values.get("legacy_security_accepted", False),
            schema_version=values["schema_version"],
        )
    except (TypeError, ValueError) as exc:
        raise ProfileError("PLAXIS profile has invalid setting types.") from exc
    if verify_installation:
        validate_installation_binding(profile)
    return profile


def write_role_profiles(
    directory: Path | str, installation: PlaxisInstallation, *,
    input_host: str = "127.0.0.1", input_port: int = 10000,
    output_host: str = "127.0.0.1", output_port: int = 10001,
    legacy_security_accepted: bool = False,
) -> dict[str, Path]:
    """Generate the two non-secret profile files for a selected install."""

    output = Path(directory).expanduser()
    return {
        "input": write_profile(output / "input.toml", endpoint_profile_for(installation, "input", host=input_host, port=input_port, legacy_security_accepted=legacy_security_accepted)),
        "output": write_profile(output / "output.toml", endpoint_profile_for(installation, "output", host=output_host, port=output_port, legacy_security_accepted=legacy_security_accepted)),
    }


def default_profile_directory(environment: Mapping[str, str] | None = None) -> Path:
    """Return the shared per-user profiles directory every client launches from."""

    values = os.environ if environment is None else environment
    local_app_data = values.get("LOCALAPPDATA")
    if not local_app_data:
        raise ProfileError(
            "LOCALAPPDATA is not set, so the default PLAXIS-MCP profiles directory "
            "cannot be located. Pass an explicit profiles directory instead."
        )
    return Path(local_app_data).joinpath(*INSTALL_DIRECTORY_PARTS) / PROFILE_DIRECTORY_NAME


def profile_path_for_role(
    role: object,
    directory: Path | str | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve one role's generated profile inside a profiles directory."""

    normalised = _normalise_role(role)
    base = (
        Path(directory).expanduser()
        if directory is not None
        else default_profile_directory(environment)
    )
    return base / ("%s.toml" % normalised)


_REGISTRY_VENDOR_ROOTS = (
    r"SOFTWARE\Bentley\PLAXIS",
    r"SOFTWARE\Bentley\Geotechnical",
    r"SOFTWARE\Seequent\PLAXIS",
    r"SOFTWARE\WOW6432Node\Bentley\PLAXIS",
    r"SOFTWARE\WOW6432Node\Bentley\Geotechnical",
    r"SOFTWARE\WOW6432Node\Seequent\PLAXIS",
)
_INSTALL_VALUE_NAMES = {"installpath", "installdir", "installationpath", "location", "path"}


def _registry_install_roots() -> Iterable[tuple[Path, str, str]]:
    """Yield roots advertised by common Bentley/Seequent registry keys.

    This is deliberately read-only and bounded to four subkey levels. An
    installation is still rejected unless its linked interpreter has one of
    the three exact certified versions.
    """

    if os.name != "nt":
        return ()
    try:
        import winreg
    except ImportError:
        return ()
    found: list[tuple[Path, str, str]] = []

    def walk(hive: Any, key_path: str, hive_name: str, depth: int = 0) -> None:
        if depth > 4:
            return
        try:
            key = winreg.OpenKey(hive, key_path)
        except OSError:
            return
        with key:
            try:
                index = 0
                while True:
                    name, value, _ = winreg.EnumValue(key, index)
                    index += 1
                    if name.lower() in _INSTALL_VALUE_NAMES and isinstance(value, str):
                        root = Path(value).expanduser()
                        if root.is_dir():
                            # The value usually hangs off a generic subkey such
                            # as ...\PLAXIS 2D CONNECT Edition V22\Install, so
                            # the leaf key name is "Install" rather than a
                            # product. The install directory carries the name a
                            # user would recognise.
                            product = root.name or key_path.rsplit("\\", 1)[-1]
                            found.append((root, hive_name + "\\" + key_path, product))
            except OSError:
                pass
            try:
                index = 0
                while True:
                    child = winreg.EnumKey(key, index)
                    index += 1
                    walk(hive, key_path + "\\" + child, hive_name, depth + 1)
            except OSError:
                pass

    for hive_name, hive in (("HKLM", winreg.HKEY_LOCAL_MACHINE), ("HKCU", winreg.HKEY_CURRENT_USER)):
        for key_path in _REGISTRY_VENDOR_ROOTS:
            walk(hive, key_path, hive_name)
    return tuple(found)


def _candidate_worker_pythons(root: Path) -> Iterable[Path]:
    """Yield linked Python executables without copying or modifying them."""

    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    local_candidates = (
        root / "python" / "python.exe",
        root / "Python" / "python.exe",
        root / "PLAXIS Python Distribution V2" / "python" / "python.exe",
    )
    shared_candidates = (
        program_data / "Bentley" / "Geotechnical" / "PLAXIS Python Distribution V2" / "python" / "python.exe",
        program_data / "Seequent" / "PLAXIS Python Distribution V2" / "python" / "python.exe",
    )
    seen: set[Path] = set()
    resolved_local: list[Path] = []
    for candidate in local_candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved.is_file():
            resolved_local.append(resolved)
    # A product-local distribution is an unambiguous link. Fall back to the
    # common ProgramData distribution only when the product root has none.
    candidates = resolved_local or shared_candidates
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved not in seen:
            seen.add(resolved)
            yield resolved


def probe_worker_python_version(worker_python: Path | str, timeout_seconds: float = 5.0) -> tuple[int, int, int]:
    """Read an interpreter ABI with no site packages or vendor imports."""

    executable = Path(worker_python).expanduser()
    try:
        result = subprocess.run(
            [str(executable), "-I", "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeProfileError("Could not read the selected PLAXIS Python version.") from exc
    if result.returncode != 0:
        raise RuntimeProfileError("Could not read the selected PLAXIS Python version.")
    return parse_python_version(result.stdout.strip())


def discover_plaxis_installations(
    *,
    registry_roots: Iterable[tuple[Path | str, str, str]] | None = None,
    python_version_probe: Callable[[Path], tuple[int, int, int]] = probe_worker_python_version,
) -> list[PlaxisInstallation]:
    """Discover only registry product roots linked to exact supported ABIs."""

    roots = registry_roots if registry_roots is not None else _registry_install_roots()
    found: list[PlaxisInstallation] = []
    seen: set[tuple[Path, Path]] = set()
    for raw_root, registry_key, product_name in roots:
        root = Path(raw_root).expanduser()
        for worker_python in _candidate_worker_pythons(root):
            if not worker_python.is_file():
                continue
            try:
                runtime = runtime_profile_for_python(python_version_probe(worker_python))
            except RuntimeProfileError:
                continue
            key = (root.resolve(), worker_python.resolve())
            if key in seen:
                continue
            seen.add(key)
            found.append(
                PlaxisInstallation(
                    root=root.resolve(), worker_python=worker_python.resolve(), runtime_profile=runtime,
                    product_name=product_name, registry_key=registry_key,
                )
            )
    return sorted(found, key=lambda item: (str(item.root).lower(), str(item.worker_python).lower()))


def select_installation(
    installations: Sequence[PlaxisInstallation], selected_root: Path | str | None = None
) -> PlaxisInstallation:
    """Select one supported install, or require an explicit choice if many."""

    if selected_root is not None:
        selected = Path(selected_root).expanduser().resolve()
        matches = [item for item in installations if item.root == selected]
        if len(matches) == 1:
            return matches[0]
        raise DeploymentError("The requested PLAXIS installation is not a certified discovery result.")
    if len(installations) == 1:
        return installations[0]
    if not installations:
        raise DeploymentError("No supported PLAXIS installation was discovered.")
    raise DeploymentError("Multiple supported PLAXIS installations were discovered; choose one explicitly.")


class CredentialStore(Protocol):
    """A small password boundary that does not expose secrets to profiles."""

    def read(self, target: str) -> str: ...

    def write(self, target: str, password: str) -> None: ...

    def delete(self, target: str) -> None: ...


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", ctypes.c_byte * 8),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class WindowsCredentialStore:
    """Windows Generic Credential implementation using CredWriteW/CredDeleteW."""

    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2

    @staticmethod
    def _validate_target(target: str) -> None:
        if not isinstance(target, str) or not target.startswith("Caros/PLAXIS-MCP/"):
            raise CredentialStoreUnavailable("Credential target is not a PLAXIS-MCP installation target.")

    @staticmethod
    def _api() -> Any:
        if os.name != "nt" or not hasattr(ctypes, "windll"):
            raise CredentialStoreUnavailable("Windows Credential Manager is available only on Windows.")
        api = ctypes.windll.advapi32
        api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), ctypes.c_uint32]
        api.CredWriteW.restype = ctypes.c_int
        api.CredReadW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.POINTER(_Credential)),
        ]
        api.CredReadW.restype = ctypes.c_int
        api.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        api.CredDeleteW.restype = ctypes.c_int
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None
        return api

    def read(self, target: str) -> str:
        self._validate_target(target)
        api = self._api()
        credential_pointer = ctypes.POINTER(_Credential)()
        if not api.CredReadW(target, self._CRED_TYPE_GENERIC, 0, ctypes.byref(credential_pointer)):
            raise CredentialStoreUnavailable("Windows Credential Manager has no PLAXIS password for this role.")
        try:
            credential = credential_pointer.contents
            blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            try:
                password = blob.decode("utf-16-le")
            except UnicodeDecodeError as exc:
                raise CredentialStoreUnavailable("Stored PLAXIS password is malformed.") from exc
            if not password:
                raise CredentialStoreUnavailable("Stored PLAXIS password is empty.")
            return password
        finally:
            api.CredFree(credential_pointer)

    def write(self, target: str, password: str) -> None:
        self._validate_target(target)
        if not isinstance(password, str) or not password:
            raise CredentialStoreUnavailable("PLAXIS password must not be empty.")
        blob = password.encode("utf-16-le")
        blob_buffer = ctypes.create_string_buffer(blob)
        credential = _Credential()
        credential.Type = self._CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self._CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "PLAXIS-MCP"
        if not self._api().CredWriteW(ctypes.byref(credential), 0):
            raise CredentialStoreUnavailable("Windows Credential Manager rejected the PLAXIS password.")

    def delete(self, target: str) -> None:
        self._validate_target(target)
        if not self._api().CredDeleteW(target, self._CRED_TYPE_GENERIC, 0):
            raise CredentialStoreUnavailable("Windows Credential Manager could not remove the PLAXIS password.")


def _target_from_args(parser: argparse.ArgumentParser, namespace: argparse.Namespace) -> str:
    if namespace.profile:
        # Removing a stored password must keep working after PLAXIS has been
        # moved or uninstalled, otherwise the operator is locked out of the
        # cleanup and the secret stays behind. Only storing one needs a
        # verified installation.
        return load_profile(
            namespace.profile,
            verify_installation=namespace.action != "delete",
        ).credential_target
    if namespace.target:
        WindowsCredentialStore._validate_target(namespace.target)
        return namespace.target
    parser.error("Specify exactly one of --profile or --target.")
    raise AssertionError("argparse.error did not exit")  # pragma: no cover


def credentials_main(argv: Sequence[str] | None = None, *, store: CredentialStore | None = None) -> int:
    """Store/delete a role password without accepting plaintext CLI input.

    The command has no password option. ``getpass`` keeps the entered value
    out of command history, agent configuration, and normal terminal output.
    """

    parser = argparse.ArgumentParser(prog="plaxis-mcp-credentials")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("set", "delete"):
        command = actions.add_parser(action)
        target_group = command.add_mutually_exclusive_group(required=True)
        target_group.add_argument("--profile", type=Path, help="Generated input.toml or output.toml")
        target_group.add_argument("--target", help="Installation- and role-specific credential target")
    namespace = parser.parse_args(argv)
    target = _target_from_args(parser, namespace)
    credential_store = store or WindowsCredentialStore()
    if namespace.action == "set":
        _store_role_password(parser, target, credential_store)
        return 0
    credential_store.delete(target)
    print("PLAXIS-MCP password removed from Windows Credential Manager.")
    return 0


def _store_role_password(
    parser: argparse.ArgumentParser, target: str, store: CredentialStore
) -> None:
    """Prompt twice with hidden input and store one role password."""

    label = "Input" if target.endswith("/input") else "Output"
    password = getpass("PLAXIS %s password: " % label)
    confirmation = getpass("Confirm password: ")
    if not password:
        parser.error("Password must not be empty.")
    if password != confirmation:
        parser.error("Passwords did not match.")
    store.write(target, password)
    print("PLAXIS-MCP password stored in Windows Credential Manager.")


def _add_profile_generation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--installation-root", type=Path, help="Required when discovery finds multiple installs")
    parser.add_argument("--input-host", default="127.0.0.1")
    parser.add_argument("--input-port", type=int, default=10000)
    parser.add_argument("--output-host", default="127.0.0.1")
    parser.add_argument("--output-port", type=int, default=10001)
    parser.add_argument(
        "--accept-legacy-37",
        action="store_true",
        help="Record the required organizational security acceptance for PLAXIS V20/V21 Python 3.7.4.",
    )


def _generate_role_profiles(
    parser: argparse.ArgumentParser, namespace: argparse.Namespace, directory: Path
) -> dict[str, Path]:
    installation = select_installation(
        discover_plaxis_installations(), selected_root=namespace.installation_root
    )
    if installation.runtime_profile.security_acceptance_required and not namespace.accept_legacy_37:
        parser.error(
            "legacy-37 is unsupported without explicit organizational security acceptance; "
            "rerun with --accept-legacy-37 only after that approval."
        )
    return write_role_profiles(
        directory,
        installation,
        input_host=namespace.input_host,
        input_port=namespace.input_port,
        output_host=namespace.output_host,
        output_port=namespace.output_port,
        legacy_security_accepted=namespace.accept_legacy_37,
    )


def profiles_main(argv: Sequence[str] | None = None) -> int:
    """Discover one certified installation and generate its two TOML profiles."""

    parser = argparse.ArgumentParser(prog="plaxis-mcp-profiles")
    parser.add_argument("--output-dir", required=True, type=Path, help="Target profiles directory")
    _add_profile_generation_arguments(parser)
    namespace = parser.parse_args(argv)
    paths = _generate_role_profiles(parser, namespace, namespace.output_dir)
    print(json.dumps({role: str(path) for role, path in paths.items()}, sort_keys=True))
    return 0


def setup_main(argv: Sequence[str] | None = None, *, store: CredentialStore | None = None) -> int:
    """Provision this machine once: generate both profiles, then store both passwords.

    This is the whole per-machine setup a client needs. Every client then
    launches ``serve --role input`` / ``serve --role output`` against the same
    profiles directory, so the installation binding and the stored passwords
    are shared rather than duplicated per client.
    """

    parser = argparse.ArgumentParser(prog="plaxis-mcp setup")
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
        help="Profiles directory (default: %%LOCALAPPDATA%%\\Caros\\PLAXIS-MCP\\profiles)",
    )
    _add_profile_generation_arguments(parser)
    parser.add_argument(
        "--skip-credentials",
        action="store_true",
        help="Generate the profiles only; store the passwords later with `credentials set`.",
    )
    namespace = parser.parse_args(argv)
    directory = namespace.profile_dir or default_profile_directory()
    paths = _generate_role_profiles(parser, namespace, directory)
    for role in ("input", "output"):
        print("Generated %s profile: %s" % (role, paths[role]))
    if namespace.skip_credentials:
        print("Store both passwords with `plaxis-mcp credentials set --profile <profile.toml>`.")
        return 0

    credential_store = store or WindowsCredentialStore()
    for role in ("input", "output"):
        _store_role_password(
            parser, load_profile(paths[role]).credential_target, credential_store
        )
    print("PLAXIS-MCP is ready. Launch a role with `plaxis-mcp serve --role input`.")
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry points cover these branches
    raise SystemExit(profiles_main())
