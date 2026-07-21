"""Host-side bridge for the Python runtime bundled with PLAXIS.

The MCP host deliberately keeps the PLAXIS SDK out of its interpreter.  It
starts a small worker with the Python executable that ships with the selected
PLAXIS installation and exchanges private JSON-lines messages over stdio.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from typing import Any, Mapping

from .deployment import RuntimeProfileError, linked_worker_pythons, validate_worker_attestation


PROTOCOL_VERSION = 1


class BridgeError(RuntimeError):
    """Base error raised by the host/worker bridge."""


class WorkerStartupError(BridgeError):
    """Raised when the PLAXIS worker cannot be started or handshaken."""


class WorkerProtocolError(BridgeError):
    """Raised when a worker emits an invalid protocol record."""


class WorkerTimeoutError(BridgeError):
    """Raised when a worker operation does not finish before its deadline."""


class WorkerOperationError(BridgeError):
    """An operational error returned by a healthy worker."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        state: str | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.state = state
        super().__init__(message)


@dataclass
class BridgeSessionConfig:
    """Pinned settings for one PLAXIS Input or Output worker process.

    ``worker_python`` must point at the selected PLAXIS bundle interpreter.
    The password is intentionally excluded from ``repr`` and is passed only in
    the ``connect`` request after the worker has been started.
    """

    worker_python: str
    role: str
    host: str = "127.0.0.1"
    port: int = 10000
    password: str = field(default="", repr=False)
    connect_timeout: float = 5.0
    request_timeout: float = 240.0
    worker_script: str | None = None
    worker_args: tuple[str, ...] = ()
    startup_timeout: float = 10.0
    environment: Mapping[str, str] | None = field(default=None, repr=False)
    runtime_profile: str | None = None
    installation_root: str | None = None
    worker_script_sha256: str | None = None
    trust_worker_script_path: bool = False

    def resolved_worker_script(self) -> Path:
        if self.worker_script:
            return Path(self.worker_script).expanduser().resolve()
        frozen_root = getattr(sys, "_MEIPASS", None)
        if frozen_root:
            packaged_worker = Path(frozen_root) / "worker" / "worker.py"
            if packaged_worker.is_file():
                return packaged_worker.resolve()
        return Path(__file__).with_name("worker.py").resolve()

    def trusted_script_roots(self) -> tuple[Path, ...]:
        """Directories a worker script may legitimately be loaded from."""

        roots = [Path(__file__).parent.resolve()]
        frozen_root = getattr(sys, "_MEIPASS", None)
        if frozen_root:
            roots.append(Path(frozen_root).resolve())
        return tuple(roots)


_SAFE_ENVIRONMENT_KEYS = (
    "ALLUSERSPROFILE",
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)
_FORBIDDEN_ENVIRONMENT_KEYS = {"PYTHONHOME", "PYTHONPATH"}
_SECRET_MARKERS = ("PASSWORD", "SECRET", "TOKEN", "CREDENTIAL", "API_KEY")
_STREAM_CLOSED = object()


def _is_secret_name(name: str) -> bool:
    upper_name = name.upper()
    return any(marker in upper_name for marker in _SECRET_MARKERS)


def _stream_lines(stream: Any, destination: queue.Queue[Any]) -> None:
    try:
        for line in iter(stream.readline, ""):
            destination.put(line)
    finally:
        destination.put(_STREAM_CLOSED)


class WorkerBridge:
    """Low-level, serialized JSON-lines transport for a PLAXIS worker."""

    def __init__(self, config: BridgeSessionConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[str] | None = None
        self._stdout: queue.Queue[Any] = queue.Queue()
        self._stderr: queue.Queue[Any] = queue.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=20)
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._next_id = 1
        self._handshake: dict[str, Any] | None = None
        self._uncertain = False
        self._secret_values: set[str] = set()
        if config.password:
            self._secret_values.add(config.password)

    @property
    def uncertain(self) -> bool:
        return self._uncertain

    @property
    def started(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def command(self) -> tuple[str, ...]:
        script = self.config.resolved_worker_script()
        return (
            str(Path(self.config.worker_python).expanduser()),
            "-I",
            "-u",
            str(script),
            *self.config.worker_args,
        )

    @property
    def handshake(self) -> dict[str, Any] | None:
        return self._handshake

    def start(self) -> dict[str, Any]:
        """Start the worker and validate its initial handshake."""
        with self._lock:
            if self.started:
                return dict(self._handshake or {})

            script = self.config.resolved_worker_script()
            interpreter = Path(self.config.worker_python).expanduser()
            self._validate_worker_integrity(script, interpreter)

            self._uncertain = False
            self._stdout = queue.Queue()
            self._stderr = queue.Queue()
            self._stderr_lines.clear()
            try:
                self._process = subprocess.Popen(
                    self.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    cwd=str(script.parent),
                    env=self._worker_environment(),
                )
            except OSError as exc:
                raise WorkerStartupError("Unable to start the PLAXIS worker.") from exc

            assert self._process.stdout is not None
            assert self._process.stderr is not None
            self._stdout_thread = threading.Thread(
                target=_stream_lines,
                args=(self._process.stdout, self._stdout),
                name="plaxis-worker-stdout",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=_stream_lines,
                args=(self._process.stderr, self._stderr),
                name="plaxis-worker-stderr",
                daemon=True,
            )
            self._stdout_thread.start()
            self._stderr_thread.start()

            try:
                result = self._request_started("handshake", {}, self.config.startup_timeout)
            except BridgeError:
                self._terminate()
                raise

            if not isinstance(result, dict):
                self._fault(WorkerProtocolError("Worker handshake was not an object."))
            if result.get("protocol") != PROTOCOL_VERSION:
                self._fault(WorkerProtocolError("Worker protocol version did not match the host."))
            if result.get("role") != self.config.role:
                self._fault(WorkerProtocolError("Worker role did not match the configured role."))
            try:
                runtime_profile = validate_worker_attestation(
                    result,
                    self.config.role,
                    expected_runtime_profile=(
                        self.config.runtime_profile
                        if self.config.runtime_profile not in {None, "auto", "unconfigured"}
                        else None
                    ),
                )
            except RuntimeProfileError as exc:
                self._fault(
                    WorkerStartupError(
                        "PLAXIS worker did not match a certified runtime profile."
                    ),
                    exc,
                )
            expected_profile = self.config.runtime_profile
            if expected_profile and expected_profile not in {"auto", "unconfigured"}:
                if runtime_profile.name != expected_profile:
                    self._fault(
                        WorkerStartupError(
                            "PLAXIS worker runtime did not match the selected profile."
                        )
                    )
            self._handshake = dict(result)
            self._handshake["runtime_profile"] = runtime_profile.name
            return dict(self._handshake)

    def _validate_worker_integrity(self, script: Path, interpreter: Path) -> None:
        """Check what we are about to execute, before we execute it.

        Attestation in ``start`` only proves what the interpreter reported
        *after* it ran our script. These checks run first, and they run on every
        respawn — a worker is replaced after each fault, long after the profile
        was loaded and validated.
        """

        if not script.is_file():
            raise WorkerStartupError("PLAXIS worker script was not found.")
        if not interpreter.is_file():
            raise WorkerStartupError("PLAXIS bundle Python executable was not found.")

        if not self.config.trust_worker_script_path:
            roots = self.config.trusted_script_roots()
            if not any(script == root or root in script.parents for root in roots):
                raise WorkerStartupError(
                    "PLAXIS worker script is outside the installed PLAXIS-MCP directory."
                )

        expected_digest = self.config.worker_script_sha256
        if expected_digest:
            try:
                actual_digest = hashlib.sha256(script.read_bytes()).hexdigest()
            except OSError as exc:
                raise WorkerStartupError("PLAXIS worker script could not be read.") from exc
            if actual_digest.lower() != expected_digest.strip().lower():
                raise WorkerStartupError("PLAXIS worker script failed its integrity check.")

        if self.config.installation_root:
            try:
                resolved_interpreter = interpreter.resolve()
            except OSError as exc:
                raise WorkerStartupError("PLAXIS bundle Python path could not be resolved.") from exc
            if resolved_interpreter not in linked_worker_pythons(self.config.installation_root):
                raise WorkerStartupError(
                    "PLAXIS bundle Python is not linked to the selected installation."
                )

    def request(
        self,
        operation: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Send a single private worker operation and return its result."""
        if not isinstance(operation, str) or not operation:
            raise ValueError("Worker operation must be a non-empty string.")
        with self._lock:
            if not self.started:
                self.start()
            return self._request_started(
                operation,
                dict(params or {}),
                self.config.request_timeout if timeout is None else timeout,
            )

    def close(self) -> None:
        """Stop the worker without leaking its output to the MCP host stdout."""
        with self._lock:
            if self.started and not self._uncertain:
                try:
                    self._request_started("shutdown", {}, min(self.config.startup_timeout, 2.0))
                except BridgeError:
                    pass
            self._terminate()

    stop = close

    def _request_started(self, operation: str, params: Mapping[str, Any], timeout: float) -> Any:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            self._fault(WorkerStartupError("PLAXIS worker exited before responding."))

        self._remember_secrets(params)
        request_id = self._next_id
        self._next_id += 1
        record = {
            "protocol": PROTOCOL_VERSION,
            "id": request_id,
            "operation": operation,
            "params": dict(params),
        }
        try:
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Worker request parameters must be JSON serializable.") from exc

        try:
            process.stdin.write(encoded + "\n")
            process.stdin.flush()
        except OSError as exc:
            self._fault(WorkerStartupError("PLAXIS worker pipe is no longer available."), exc)

        return self._read_response(request_id, timeout)

    def _read_response(self, request_id: int, timeout: float) -> Any:
        try:
            line = self._stdout.get(timeout=max(float(timeout), 0.0))
        except queue.Empty as exc:
            self._fault(WorkerTimeoutError("PLAXIS worker operation timed out."), exc)
        if line is _STREAM_CLOSED:
            self._fault(WorkerStartupError("PLAXIS worker exited before responding."))
        if not isinstance(line, str):
            self._fault(WorkerProtocolError("PLAXIS worker emitted an invalid stream record."))
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            self._fault(WorkerProtocolError("PLAXIS worker emitted invalid JSON."), exc)
        if not isinstance(response, dict):
            self._fault(WorkerProtocolError("PLAXIS worker response was not an object."))
        if response.get("protocol") != PROTOCOL_VERSION:
            self._fault(WorkerProtocolError("PLAXIS worker response protocol did not match."))
        if response.get("id") != request_id:
            self._fault(WorkerProtocolError("PLAXIS worker response id did not match its request."))
        if not isinstance(response.get("ok"), bool):
            self._fault(WorkerProtocolError("PLAXIS worker response omitted its success flag."))
        if response["ok"]:
            return response.get("result")

        error = response.get("error")
        if not isinstance(error, dict):
            self._fault(WorkerProtocolError("PLAXIS worker error response was malformed."))
        code = error.get("code")
        message = error.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            self._fault(WorkerProtocolError("PLAXIS worker error response was incomplete."))
        state = error.get("state")
        if state is not None and not isinstance(state, str):
            self._fault(WorkerProtocolError("PLAXIS worker error response had an invalid state."))
        raise WorkerOperationError(
            code=code,
            message=self._redact(message),
            retryable=bool(error.get("retryable", False)),
            state=state,
        )

    def _fault(self, error: BridgeError, cause: BaseException | None = None) -> None:
        self._uncertain = True
        self._terminate()
        if cause is None:
            raise error
        raise error from cause

    def _terminate(self) -> None:
        process = self._process
        self._process = None
        self._handshake = None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _worker_environment(self) -> dict[str, str]:
        environment = {
            key: os.environ[key]
            for key in _SAFE_ENVIRONMENT_KEYS
            if key in os.environ
        }
        for key, value in (self.config.environment or {}).items():
            upper_key = key.upper()
            if upper_key in _FORBIDDEN_ENVIRONMENT_KEYS or _is_secret_name(key):
                continue
            environment[str(key)] = str(value)
        environment["PLAXIS_ROLE"] = self.config.role
        return environment

    def _remember_secrets(self, value: Any, key: str = "") -> None:
        if _is_secret_name(key) and isinstance(value, str) and value:
            self._secret_values.add(value)
        if isinstance(value, Mapping):
            for item_key, item_value in value.items():
                self._remember_secrets(item_value, str(item_key))
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._remember_secrets(item, key)

    def _redact(self, message: str) -> str:
        redacted = message
        for secret in sorted(self._secret_values, key=len, reverse=True):
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted


class BridgeSession:
    """Session-shaped façade for a role-pinned PLAXIS worker.

    This class is intentionally independent of ``core.py``.  A future server
    session factory can use ``invoke`` to map its domain methods to worker
    operations without importing PLAXIS libraries in the MCP host.
    """

    def __init__(self, config: BridgeSessionConfig) -> None:
        self.config = config
        self._bridge = WorkerBridge(config)

    @property
    def uncertain(self) -> bool:
        return self._bridge.uncertain

    @property
    def handshake(self) -> dict[str, Any] | None:
        return self._bridge.handshake

    def connect(self) -> dict[str, Any]:
        self._bridge.start()
        result = self._bridge.request(
            "connect",
            {
                "host": self.config.host,
                "port": self.config.port,
                "password": self.config.password,
                "timeout": self.config.connect_timeout,
                "request_timeout": self.config.request_timeout,
            },
            timeout=self.config.startup_timeout,
        )
        return self._require_object_result(result, "connect")

    def disconnect(self) -> dict[str, Any]:
        if not self._bridge.started:
            return self._idle_status()
        return self._require_object_result(self._bridge.request("disconnect"), "disconnect")

    def status(self) -> dict[str, Any]:
        if not self._bridge.started:
            return self._idle_status()
        return self._require_object_result(self._bridge.request("status"), "status")

    def invoke(self, operation: str, timeout: float | None = None, **params: Any) -> Any:
        """Dispatch a named PLAXIS domain operation through the worker."""
        if not isinstance(operation, str) or not operation:
            raise ValueError("PLAXIS operation must be a non-empty string.")
        self._bridge.start()
        return self._bridge.request(
            "call",
            {"method": operation, "params": params},
            timeout=timeout,
        )

    def close(self) -> None:
        self._bridge.close()

    def __enter__(self) -> "BridgeSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _idle_status(self) -> dict[str, Any]:
        return {
            "role": self.config.role,
            "state": "uncertain" if self._bridge.uncertain else "disconnected",
            "connected": False,
            "host": self.config.host,
            "port": self.config.port,
            "current_operation": None,
        }

    @staticmethod
    def _require_object_result(result: Any, operation: str) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise WorkerProtocolError("PLAXIS worker returned an invalid %s result." % operation)
        return result
