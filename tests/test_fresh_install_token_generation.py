"""Regression coverage for the fresh-install experience and token generation.

setup.sh and setup.ps1:
1. Create required persistent data directories (`config`, `data/music`, `data/downloads`, `web-manager-data`).
2. Initialize default `config/config.yaml` from `config.yaml.example` if not present.
3. Generate cryptographically strong non-placeholder tokens for `BEETS_API_TOKEN` and `BEETS_WEB_AUTH_TOKEN`.
4. Set `BEETS_EXPECT_EXISTING_LIBRARY=0` on fresh install when no database exists, and `1` when `config/musiclibrary.blb` exists.
5. Leave browser password unconfigured in `.env` so the browser first-run wizard is triggered without requiring a complex 32-char CLI password prompt.
6. Support idempotent re-runs preserving existing configuration.

These tests execute the real setup.sh (bash) and setup.ps1 (pwsh) scripts end-to-end
in an isolated temporary directory with a stub `docker` on PATH.
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

    def test_fresh_install_creates_directories_and_tokens_without_cli_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            result = self._run_setup_sh(workdir)
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

            # Persistent directories created
            self.assertTrue((workdir / "config").is_dir())
            self.assertTrue((workdir / "data" / "music").is_dir())
            self.assertTrue((workdir / "data" / "downloads").is_dir())
            self.assertTrue((workdir / "web-manager-data").is_dir())
            self.assertTrue((workdir / "config" / "config.yaml").is_file())

            env_path = workdir / ".env"
            self.assertTrue(env_path.exists())
            api_token = _read_env_value(env_path, "BEETS_API_TOKEN")
            web_token = _read_env_value(env_path, "BEETS_WEB_AUTH_TOKEN")
            expect_lib = _read_env_value(env_path, "BEETS_EXPECT_EXISTING_LIBRARY")
            web_pass = _read_env_value(env_path, "BEETS_WEB_PASSWORD")

            self.assertNotIn(api_token.lower(), _PLACEHOLDER_TOKENS)
            self.assertGreaterEqual(len(api_token), _MIN_TOKEN_LENGTH)
            self.assertNotEqual(api_token, "changeme")

            self.assertNotIn(web_token.lower(), _PLACEHOLDER_TOKENS)
            self.assertGreaterEqual(len(web_token), _MIN_TOKEN_LENGTH)

            self.assertNotEqual(api_token, web_token)
            self.assertEqual(expect_lib, "0")
            self.assertEqual(web_pass, "")

    def test_fresh_install_with_existing_database_sets_expect_existing_library_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            config_dir = workdir / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "musiclibrary.blb").write_bytes(b"existing-db")

            result = self._run_setup_sh(workdir)
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

            env_path = workdir / ".env"
            expect_lib = _read_env_value(env_path, "BEETS_EXPECT_EXISTING_LIBRARY")
            self.assertEqual(expect_lib, "1")

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
                "if [ \"$1\" = \"compose\" ]; then\n"
                "  for arg in \"$@\"; do\n"
                "    if [ \"$arg\" = \"ps\" ]; then echo healthy; exit 0; fi\n"
                "  done\n"
                "fi\n"
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

    def test_fresh_install_creates_directories_and_tokens_without_cli_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            result = self._run_setup_ps1(workdir)
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

            # Persistent directories created
            self.assertTrue((workdir / "config").is_dir())
            self.assertTrue((workdir / "data" / "music").is_dir())
            self.assertTrue((workdir / "data" / "downloads").is_dir())
            self.assertTrue((workdir / "web-manager-data").is_dir())
            self.assertTrue((workdir / "config" / "config.yaml").is_file())

            env_path = workdir / ".env"
            self.assertTrue(env_path.exists())
            api_token = _read_env_value(env_path, "BEETS_API_TOKEN")
            web_token = _read_env_value(env_path, "BEETS_WEB_AUTH_TOKEN")
            expect_lib = _read_env_value(env_path, "BEETS_EXPECT_EXISTING_LIBRARY")
            web_pass = _read_env_value(env_path, "BEETS_WEB_PASSWORD")

            self.assertNotIn(api_token.lower(), _PLACEHOLDER_TOKENS)
            self.assertGreaterEqual(len(api_token), _MIN_TOKEN_LENGTH)
            self.assertNotEqual(api_token, "changeme")

            self.assertNotIn(web_token.lower(), _PLACEHOLDER_TOKENS)
            self.assertGreaterEqual(len(web_token), _MIN_TOKEN_LENGTH)

            self.assertNotEqual(api_token, web_token)
            self.assertEqual(expect_lib, "0")
            self.assertEqual(web_pass, "")

    def test_fresh_install_with_existing_database_sets_expect_existing_library_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            config_dir = workdir / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "musiclibrary.blb").write_bytes(b"existing-db")

            result = self._run_setup_ps1(workdir)
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

            env_path = workdir / ".env"
            expect_lib = _read_env_value(env_path, "BEETS_EXPECT_EXISTING_LIBRARY")
            self.assertEqual(expect_lib, "1")


if __name__ == "__main__":
    unittest.main()
