"""Regression coverage for the fresh-install BEETS_API_TOKEN generation gap.

setup.sh and setup.ps1 already generated a random BEETS_WEB_AUTH_TOKEN when
creating .env from .env.example, but left BEETS_API_TOKEN untouched --
.env.example ships BEETS_API_TOKEN=changeme, which
backend.beets_control_agent.beets_api_token_is_usable() rejects as a known
placeholder at startup (RuntimeError). A clean install following the
documented ./setup.sh or .\\setup.ps1 path would therefore build both images
successfully but have the beets engine container fail to start.

These tests actually execute the real setup.sh (bash) and setup.ps1 (pwsh)
scripts end-to-end in an isolated temporary directory, with a stub `docker`
on PATH so no real image build/daemon is required, and assert the resulting
.env contains a strong, non-placeholder BEETS_API_TOKEN alongside
BEETS_WEB_AUTH_TOKEN. They skip (not fail) when bash/pwsh aren't available.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_PLACEHOLDER_TOKENS = {"changeme", "", None}
_MIN_TOKEN_LENGTH = 32


def _read_env_value(env_path: Path, key: str) -> str:
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{key} not found in {env_path}")


def _which(name: str) -> str | None:
    return shutil.which(name)


def _find_working_bash() -> str | None:
    """Locate a real, runnable bash. On Windows, `shutil.which("bash")` can
    resolve to the WSL launcher stub (System32\\bash.exe), which fails with
    no distro registered instead of running a shell -- prefer a genuine
    Git-for-Windows bash when present, and verify whatever is chosen can
    actually execute before trusting it."""
    candidates = []
    on_path = _which("bash")
    if on_path:
        candidates.append(on_path)
    candidates.extend([
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ])
    for candidate in candidates:
        try:
            proc = subprocess.run(
                [candidate, "-c", "exit 0"], capture_output=True, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return candidate
    return None


class SetupShTokenGenerationTests(unittest.TestCase):
    """Runs the real setup.sh with a stub `docker` on PATH."""

    BASH = None

    @classmethod
    def setUpClass(cls):
        cls.BASH = _find_working_bash()
        if cls.BASH is None:
            raise unittest.SkipTest("no working bash found on this system")

    def _run_setup_sh(self, workdir: Path, stdin_text: str = "\n\n") -> subprocess.CompletedProcess:
        bin_dir = workdir / "stubbin"
        bin_dir.mkdir(exist_ok=True)
        docker_stub = bin_dir / "docker"
        docker_stub.write_text(
            "#!/usr/bin/env bash\n"
            "# 'ps' may not be $2 -- setup.sh's health-check loop runs\n"
            "# 'docker compose -f <file> ps ...', inserting -f/<file> before ps.\n"
            "if [ \"$1\" = \"compose\" ]; then\n"
            "  for arg in \"$@\"; do\n"
            "    if [ \"$arg\" = \"ps\" ]; then echo healthy; exit 0; fi\n"
            "  done\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        docker_stub.chmod(docker_stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        shutil.copy(ROOT / "setup.sh", workdir / "setup.sh")
        shutil.copy(ROOT / ".env.example", workdir / ".env.example")
        shutil.copy(ROOT / "config.yaml.example", workdir / "config.yaml.example")

        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        return subprocess.run(
            [self.BASH, "setup.sh"],
            cwd=str(workdir),
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def test_generates_strong_non_placeholder_api_and_web_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            result = self._run_setup_sh(workdir)
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

            env_path = workdir / ".env"
            self.assertTrue(env_path.exists())
            api_token = _read_env_value(env_path, "BEETS_API_TOKEN")
            web_token = _read_env_value(env_path, "BEETS_WEB_AUTH_TOKEN")

            self.assertNotIn(api_token.lower(), _PLACEHOLDER_TOKENS)
            self.assertGreaterEqual(len(api_token), _MIN_TOKEN_LENGTH)
            self.assertNotEqual(api_token, "changeme")

            self.assertNotIn(web_token.lower(), _PLACEHOLDER_TOKENS)
            self.assertGreaterEqual(len(web_token), _MIN_TOKEN_LENGTH)

            self.assertNotEqual(api_token, web_token)

    def test_rerun_is_idempotent_and_preserves_existing_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            first = self._run_setup_sh(workdir)
            self.assertEqual(first.returncode, 0, msg=first.stdout + first.stderr)
            env_path = workdir / ".env"
            api_token_first = _read_env_value(env_path, "BEETS_API_TOKEN")
            web_token_first = _read_env_value(env_path, "BEETS_WEB_AUTH_TOKEN")

            second = self._run_setup_sh(workdir)
            self.assertEqual(second.returncode, 0, msg=second.stdout + second.stderr)
            self.assertIn(".env already exists", second.stdout)

            api_token_second = _read_env_value(env_path, "BEETS_API_TOKEN")
            web_token_second = _read_env_value(env_path, "BEETS_WEB_AUTH_TOKEN")
            self.assertEqual(api_token_first, api_token_second)
            self.assertEqual(web_token_first, web_token_second)


class SetupPs1TokenGenerationTests(unittest.TestCase):
    """Runs the real setup.ps1 with a stub `docker` on PATH."""

    @classmethod
    def setUpClass(cls):
        if _which("pwsh") is None:
            raise unittest.SkipTest("pwsh is not available on PATH")

    def _run_setup_ps1(self, workdir: Path) -> subprocess.CompletedProcess:
        bin_dir = workdir / "stubbin"
        bin_dir.mkdir(exist_ok=True)
        if os.name == "nt":
            docker_stub = bin_dir / "docker.cmd"
            docker_stub.write_text(
                "@echo off\r\n"
                "if not \"%1\"==\"compose\" exit /b 0\r\n"
                "echo %* | findstr /C:\" ps \" >nul && (echo healthy & exit /b 0)\r\n"
                "echo %* | findstr /R /C:\" ps$\" >nul && (echo healthy & exit /b 0)\r\n"
                "exit /b 0\r\n",
                encoding="utf-8",
            )
        else:
            docker_stub = bin_dir / "docker"
            docker_stub.write_text(
                "#!/usr/bin/env bash\n"
                "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"ps\" ]; then echo healthy; exit 0; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            docker_stub.chmod(docker_stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        shutil.copy(ROOT / "setup.ps1", workdir / "setup.ps1")
        shutil.copy(ROOT / ".env.example", workdir / ".env.example")
        shutil.copy(ROOT / "config.yaml.example", workdir / "config.yaml.example")

        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        return subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-File", "setup.ps1"],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def test_generates_strong_non_placeholder_api_and_web_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            result = self._run_setup_ps1(workdir)
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

            env_path = workdir / ".env"
            self.assertTrue(env_path.exists())
            api_token = _read_env_value(env_path, "BEETS_API_TOKEN")
            web_token = _read_env_value(env_path, "BEETS_WEB_AUTH_TOKEN")

            self.assertNotIn(api_token.lower(), _PLACEHOLDER_TOKENS)
            self.assertGreaterEqual(len(api_token), _MIN_TOKEN_LENGTH)
            self.assertNotEqual(api_token, "changeme")

            self.assertNotIn(web_token.lower(), _PLACEHOLDER_TOKENS)
            self.assertGreaterEqual(len(web_token), _MIN_TOKEN_LENGTH)

            self.assertNotEqual(api_token, web_token)


if __name__ == "__main__":
    unittest.main()
