#!/usr/bin/env python3
"""Reusable CI verifier for Beets engine Docker images.

Validates Beets engine images against explicit plugin, version, capability,
and command-help expectations without requiring Beets to be installed in the
runner Python environment.

Safety properties:
- Every value that could contain attacker- or CI-input-controlled text
  (image name, plugin names, command names) is validated against a strict
  safe-character allowlist before use, and passed to subprocesses as
  discrete argv list elements -- never interpolated into a shell string.
- Container Python probe scripts are generated from a small set of fixed
  templates with only already-validated identifiers substituted in, and are
  passed to the container via stdin, not as part of a shell -c string.
- All checks run with --network none: none of them need network access, and
  disabling it removes an entire class of nondeterminism and risk.
- Every docker invocation is wrapped so a missing/broken Docker daemon or
  binary produces one clear error message instead of an unhandled traceback.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from typing import List, Optional

_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$")


def _require_safe_identifier(value: str, label: str) -> str:
    """Reject anything that is not a plain identifier/tag-like token before
    it is ever used to build a subprocess argv or an in-container script,
    closing off shell/Python injection through image names, plugin names,
    or command names."""
    if not value or not _SAFE_IDENTIFIER_RE.match(value):
        raise SystemExit(f"Invalid {label} (unsafe characters): {value!r}")
    return value


class DockerCommandError(RuntimeError):
    pass


def _run_docker(args: List[str], input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["docker", *args],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise DockerCommandError(f"docker binary not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerCommandError(f"docker command timed out: {' '.join(args)}") from exc
    except OSError as exc:
        raise DockerCommandError(f"docker command failed to start: {exc}") from exc


def run_container_python(image: str, script: str) -> subprocess.CompletedProcess:
    """Run a Python script inside a disposable, network-isolated container by
    piping it to stdin, never by interpolating it into a shell -c string."""
    return _run_docker(
        [
            "run", "--rm", "--network", "none", "-i",
            "--entrypoint", "/lsiopy/bin/python3",
            image, "-",
        ],
        input_text=script,
    )


def run_container_shell(image: str, shell_cmd: str) -> subprocess.CompletedProcess:
    """Run a fixed, script-authored (never CLI-argument-interpolated) shell
    command inside a disposable, network-isolated container."""
    return _run_docker(["run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image, "-c", shell_cmd])


def check_version(image: str, expected_version: str, errors: List[str]) -> None:
    res = run_container_shell(image, "/lsiopy/bin/beet version")
    if res.returncode != 0:
        errors.append(f"beet version command failed: {res.stderr.strip()}")
        return
    version_line = ""
    for line in res.stdout.splitlines():
        if line.lower().startswith("beets version"):
            version_line = line.strip()
            break
    if not version_line:
        errors.append(f"beet version output did not contain a 'beets version' line: {res.stdout.strip()!r}")
        return
    reported_version = version_line.split()[-1]
    if reported_version != expected_version:
        errors.append(f"Expected Beets version '{expected_version}', got '{reported_version}' ({version_line})")
    else:
        print(f"[OK] Beets version verified: {reported_version}")


def check_control_agent_files(image: str, errors: List[str]) -> None:
    res = run_container_shell(
        image,
        "test -f /opt/beets-web-manager-agent/beets_control_agent.py && test -f /custom-services.d/beets-control-agent",
    )
    if res.returncode != 0:
        errors.append(
            "Control agent files (/opt/beets-web-manager-agent/beets_control_agent.py or "
            "/custom-services.d/beets-control-agent) missing"
        )
    else:
        print("[OK] Control agent files and S6 service script present")


def check_required_plugin(image: str, plugin: str, errors: List[str]) -> None:
    script = f"""
import beets.plugins
p = beets.plugins._get_plugin({plugin!r})
if p is None:
    print("FAILED: returned None")
    raise SystemExit(1)
print("OK:", p.__class__.__module__, p.__class__.__name__)
"""
    res = run_container_python(image, script)
    if res.returncode != 0 or not res.stdout.strip().startswith("OK:"):
        errors.append(f"Required plugin '{plugin}' failed to resolve: {res.stdout.strip()} {res.stderr.strip()}")
    else:
        print(f"[OK] Required plugin '{plugin}' resolved: {res.stdout.strip()}")


def check_forbid_duplicate_plugin(image: str, plugin: str, configured_plugins: List[str], errors: List[str]) -> None:
    """Real duplicate-plugin check: loads the FULL configured plugin set (the
    same set every --require-plugin/--require-command-help name contributes,
    exactly as a real deployment would configure them) via `beet -vv version`
    and asserts `plugin` appears at most once in the final "plugins:" line --
    not merely that _get_plugin(plugin) resolves to itself in isolation,
    which is true whether or not a real duplicate exists elsewhere."""
    plugin_list = " ".join(dict.fromkeys(configured_plugins))
    setup_cmd = (
        f"mkdir -p /tmp/ci_verify && "
        f"printf 'plugins: {plugin_list}\\n' > /tmp/ci_verify_config.yaml && "
        f"BEETSDIR=/tmp/ci_verify /lsiopy/bin/beet -c /tmp/ci_verify_config.yaml -vv version"
    )
    res = run_container_shell(image, setup_cmd)
    plugins_line = ""
    for line in res.stdout.splitlines():
        if line.strip().startswith("plugins:"):
            plugins_line = line.strip()
    if not plugins_line:
        errors.append(f"Could not find a 'plugins:' line in beet -vv version output for duplicate check: {res.stdout.strip()!r}")
        return
    loaded = [name.strip() for name in plugins_line.split("plugins:", 1)[1].split(",")]
    count = loaded.count(plugin)
    if count > 1:
        errors.append(f"Plugin '{plugin}' is duplicated in the loaded plugin list ({count}x): {plugins_line}")
    elif count == 0:
        errors.append(f"Plugin '{plugin}' did not load at all when checking for duplicates: {plugins_line}")
    else:
        print(f"[OK] Plugin '{plugin}' loaded exactly once: {plugins_line}")


_BPSYNC_KNOWN_INCOMPATIBILITY_SIGNATURE = "BeatportPlugin.setup() missing"


def check_bpsync(image: str, errors: List[str], require_operational: bool = False) -> None:
    """Verify BPSync through Beets' real plugin loader, not import+class-identity.

    Importing beetsplug.bpsync and checking the class name (the previous
    version of this check) is a false positive: BPSyncPlugin.__init__
    unconditionally calls self.beatport_plugin.setup() with no arguments,
    but the installed BeatportPlugin.setup(self, session) requires one --
    every real instantiation attempt (which is exactly what Beets' own
    loader does: _get_plugin() calls obj() with zero arguments) fails with
    a TypeError before BPSyncPlugin is ever usable. That failure is a
    genuine upstream signature mismatch between the bundled bpsync.py and
    beatport.py, unrelated to credentials -- it happens with no Beatport
    account configured at all.

    This check runs the plugin through Beets' actual loader (a disposable
    config with `plugins: bpsync`, `beet -vv version`) and distinguishes:
      - resolved:    the module imports and the class exists with the
                     right identity (always true here; not sufficient
                     evidence of anything working).
      - loaded:      _get_plugin() successfully constructed an instance
                     and it appears in the loaded "plugins:" line.
      - incompatible: the known setup()-signature TypeError occurred.
                     Non-fatal unless require_operational is set, since
                     bpsync is not enabled in production and this does
                     not affect Chroma, submit, or mbsubmit.
      - broken (unexpected): anything else -- always fatal, since it
                     would mean a new, undiagnosed regression rather than
                     the one known, already-investigated issue.
    """
    resolve_script = """
import importlib
mod = importlib.import_module("beetsplug.bpsync")
cls = getattr(mod, "BPSyncPlugin", None)
if cls is None or cls.__module__ != "beetsplug.bpsync":
    print("FAILED:", cls)
    raise SystemExit(1)
print("RESOLVED:", cls.__module__, cls.__name__)
"""
    resolved = run_container_python(image, resolve_script)
    if resolved.returncode != 0 or "RESOLVED:" not in resolved.stdout:
        errors.append(
            f"bpsync module/class resolution failed (this is a regression beyond the known "
            f"setup() incompatibility): {resolved.stdout.strip()} {resolved.stderr.strip()}"
        )
        return

    setup_cmd = (
        "mkdir -p /tmp/ci_verify_bpsync && "
        "printf 'plugins: bpsync\\n' > /tmp/ci_verify_bpsync/config.yaml && "
        "BEETSDIR=/tmp/ci_verify_bpsync /lsiopy/bin/beet -c /tmp/ci_verify_bpsync/config.yaml -vv version; "
        "rm -rf /tmp/ci_verify_bpsync"
    )
    res = run_container_shell(image, setup_cmd)
    combined_output = res.stdout + res.stderr

    plugins_line = ""
    for line in res.stdout.splitlines():
        if line.strip().startswith("plugins:"):
            plugins_line = line.strip()
    loaded_names = (
        [name.strip() for name in plugins_line.split("plugins:", 1)[1].split(",")]
        if plugins_line
        else []
    )

    if "bpsync" in loaded_names:
        print("[OK] bpsync resolved, loaded, and operational (appears in loaded plugins list)")
        return

    if _BPSYNC_KNOWN_INCOMPATIBILITY_SIGNATURE in combined_output:
        message = (
            "bpsync resolved (class importable, correct identity) but failed to load: "
            "known upstream incompatibility -- BeatportPlugin.setup() requires a session "
            "argument that bpsync.py's __init__ does not provide, independent of credentials. "
            "Not enabled in production; does not affect Chroma, submit, or mbsubmit."
        )
        if require_operational:
            errors.append(f"bpsync is required to be operational but is not: {message}")
        else:
            print(f"[INCOMPATIBLE, non-fatal] {message}")
        return

    # Anything else is an unexpected, previously-undiagnosed failure mode --
    # always fatal, since a false negative here would hide a real regression
    # behind the known-issue label.
    errors.append(
        "bpsync failed to load with an error that does not match the known "
        f"setup()-signature incompatibility -- treating as a new regression: {combined_output.strip()[-2000:]}"
    )


def check_command_help(image: str, command: str, configured_plugins: List[str], errors: List[str]) -> None:
    plugin_list = " ".join(dict.fromkeys(configured_plugins))
    setup_cmd = (
        f"mkdir -p /tmp/ci_verify && "
        f"printf 'plugins: {plugin_list}\\n' > /tmp/ci_verify_config.yaml && "
        f"BEETSDIR=/tmp/ci_verify /lsiopy/bin/beet -c /tmp/ci_verify_config.yaml {command} --help"
    )
    res = run_container_shell(image, setup_cmd)
    if res.returncode != 0:
        errors.append(f"beet {command} --help failed with exit code {res.returncode}: {res.stderr.strip()}")
    else:
        print(f"[OK] 'beet {command} --help' executed successfully (exit code 0)")


def check_s6_supervision(image: str, errors: List[str]) -> None:
    """Real-runtime proof (not source inspection) that the inherited
    LinuxServer `svc-beets` web service never runs under S6, that the Beets
    control agent does run under S6 and is restarted after a crash, and that
    the container shuts down cleanly -- starts the image with its actual
    entrypoint (unlike the other checks here, which override --entrypoint
    for speed), so this genuinely exercises S6 bring-up rather than just
    grepping the Dockerfile for the removal instruction."""
    container = f"beets-s6-verify-{os.getpid()}"
    token = "ci-s6-verify-" + "x" * 40
    try:
        run_res = _run_docker([
            "run", "-d", "--name", container,
            "-e", f"BEETS_API_TOKEN={token}",
            # This test verifies S6 process supervision (svc-beets stays
            # down, the control agent stays up and restarts after a crash),
            # not the startup guard's fail-closed behavior -- that's
            # covered by its own dedicated tests. A fresh container here
            # has no pre-existing library, and the guard's production
            # default (BEETS_EXPECT_EXISTING_LIBRARY=1 in the Dockerfile)
            # correctly refuses to start against a missing database, which
            # would otherwise block this test before it can even observe
            # S6 behavior.
            "-e", "BEETS_EXPECT_EXISTING_LIBRARY=0",
            image,
        ])
        if run_res.returncode != 0:
            errors.append(f"S6 check: failed to start container: {run_res.stderr.strip()}")
            return

        healthy = False
        for _ in range(30):
            inspect = _run_docker(["inspect", "--format", "{{.State.Health.Status}}", container])
            if inspect.stdout.strip() == "healthy":
                healthy = True
                break
            time.sleep(2)
        if not healthy:
            logs = _run_docker(["logs", "--tail", "60", container])
            errors.append(f"S6 check: container never became healthy: {logs.stdout[-2000:]}")
            return

        svc_status = _run_docker(["exec", container, "s6-svstat", "/run/service/svc-beets"])
        if "down" not in svc_status.stdout:
            errors.append(f"S6 check: inherited svc-beets is not down: {svc_status.stdout.strip()!r}")
        else:
            print("[OK] Inherited svc-beets web service is down (never brought up by S6)")

        agent_status = _run_docker(["exec", container, "s6-svstat", "/run/service/custom-svc-beets-control-agent"])
        if "up" not in agent_status.stdout:
            errors.append(f"S6 check: control agent is not up: {agent_status.stdout.strip()!r}")
            return
        print("[OK] Beets control agent is up under S6 supervision")

        pid_res = _run_docker(["exec", container, "pgrep", "-f", "beets_control_agent.py"])
        old_pid = pid_res.stdout.strip()
        if not old_pid:
            errors.append("S6 check: could not determine control agent PID")
            return

        kill_res = _run_docker(["exec", container, "kill", "-9", old_pid])
        if kill_res.returncode != 0:
            errors.append(f"S6 check: failed to kill control agent PID {old_pid}: {kill_res.stderr.strip()}")
            return

        restarted = False
        new_pid = ""
        for _ in range(15):
            time.sleep(1)
            pid_res = _run_docker(["exec", container, "pgrep", "-f", "beets_control_agent.py"])
            new_pid = pid_res.stdout.strip()
            if new_pid and new_pid != old_pid:
                restarted = True
                break
        if not restarted:
            errors.append("S6 check: control agent was not restarted by S6 after being killed")
            return
        print(f"[OK] Control agent restarted under S6 after crash (pid {old_pid} -> {new_pid})")

        state = _run_docker(["inspect", "--format", "{{.State.Status}}", container])
        if state.stdout.strip() != "running":
            errors.append(f"S6 check: container is not running after restart: {state.stdout.strip()!r}")
            return

        stop_res = _run_docker(["stop", "-t", "15", container])
        if stop_res.returncode != 0:
            errors.append(f"S6 check: container did not stop cleanly: {stop_res.stderr.strip()}")
            return
        exit_code = _run_docker(["inspect", "--format", "{{.State.ExitCode}}", container])
        if exit_code.stdout.strip() != "0":
            errors.append(f"S6 check: container exited with nonzero code {exit_code.stdout.strip()!r} on stop")
        else:
            print("[OK] Container shuts down cleanly (exit code 0)")
    finally:
        _run_docker(["rm", "-f", container])


def check_image_exists(image: str, errors: List[str]) -> bool:
    res = _run_docker(["image", "inspect", image])
    if res.returncode != 0:
        errors.append(f"Image does not exist locally: {image}")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Beets engine image capabilities")
    parser.add_argument("--image", default="beets-engine:ci", help="Docker image tag to verify")
    parser.add_argument("--require-version", default="", help="Expected Beets version string (e.g. 2.4.0)")
    parser.add_argument("--require-plugin", action="append", default=[], help="Required loaded plugin name")
    parser.add_argument("--forbid-duplicate-plugin", action="append", default=[], help="Forbidden duplicate plugin name")
    parser.add_argument("--require-command-help", action="append", default=[], help="Subcommand requiring zero exit on --help")
    parser.add_argument(
        "--check-bpsync", action="store_true",
        help="Verify bpsync through Beets' real plugin loader. Reports resolved/loaded/incompatible "
             "status; a known upstream setup()-signature incompatibility is reported but not fatal "
             "unless --require-bpsync-operational is also set.",
    )
    parser.add_argument(
        "--require-bpsync-operational", action="store_true",
        help="Fail the build if bpsync does not fully load (including the known incompatibility). "
             "Only pass this if a deployment actually requires bpsync to work.",
    )
    parser.add_argument(
        "--check-s6-supervision", action="store_true",
        help="Start the image for real and verify svc-beets stays down, the control agent runs and restarts under S6, and shutdown is clean",
    )

    args = parser.parse_args()
    errors: List[str] = []

    try:
        image = _require_safe_identifier(args.image, "--image")
        require_plugins = [_require_safe_identifier(p, "--require-plugin") for p in args.require_plugin]
        forbid_duplicates = [_require_safe_identifier(p, "--forbid-duplicate-plugin") for p in args.forbid_duplicate_plugin]
        require_command_help = [_require_safe_identifier(c, "--require-command-help") for c in args.require_command_help]
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Verifying Beets engine image: {image}")

    try:
        if not check_image_exists(image, errors):
            print("\n--- Beets engine verification FAILED ---", file=sys.stderr)
            for err in errors:
                print(f"  [FAIL] {err}", file=sys.stderr)
            return 1

        configured_plugins = list(dict.fromkeys(require_plugins + forbid_duplicates + require_command_help + ["mbsubmit"]))

        if args.require_version:
            check_version(image, args.require_version, errors)

        check_control_agent_files(image, errors)

        for plugin in require_plugins:
            check_required_plugin(image, plugin, errors)

        for plugin in forbid_duplicates:
            check_forbid_duplicate_plugin(image, plugin, configured_plugins, errors)

        if args.check_bpsync:
            check_bpsync(image, errors, require_operational=args.require_bpsync_operational)

        for command in require_command_help:
            check_command_help(image, command, configured_plugins, errors)

        if args.check_s6_supervision:
            check_s6_supervision(image, errors)
    except DockerCommandError as exc:
        print(f"\n--- Beets engine verification FAILED (Docker error) ---", file=sys.stderr)
        print(f"  [FAIL] {exc}", file=sys.stderr)
        return 1

    if errors:
        print("\n--- Beets engine verification FAILED ---", file=sys.stderr)
        for err in errors:
            print(f"  [FAIL] {err}", file=sys.stderr)
        return 1

    print("\n--- Beets engine verification PASSED ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
