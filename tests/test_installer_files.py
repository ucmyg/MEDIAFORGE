"""Static checks for the Windows setup launcher (PowerShell is not available in CI here; behaviour is unverified)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BATS = ["Install ClipForge.bat", "Launch ClipForge.bat", "Uninstall ClipForge.bat"]
PS1S = ["installer/setup.ps1", "installer/uninstall.ps1"]


def test_windows_scripts_exist_with_crlf_and_no_secrets():
    for rel in BATS + PS1S:
        raw = (ROOT / rel).read_bytes()
        assert raw, rel
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), f"{rel} must use CRLF line endings"
        text = raw.decode("utf-8")
        assert not re.search(r"(?i)(password|client_secret\s*=|token\s*=)\s*['\"]\S", text), f"{rel} must not embed credentials"


def test_bat_files_call_the_scripts_they_ship_with():
    install = (ROOT / "Install ClipForge.bat").read_text(encoding="utf-8")
    assert "installer\\setup.ps1" in install and "-ExecutionPolicy Bypass" in install and "Set-ExecutionPolicy" not in install
    uninstall = (ROOT / "Uninstall ClipForge.bat").read_text(encoding="utf-8")
    assert "installer\\uninstall.ps1" in uninstall
    launch = (ROOT / "Launch ClipForge.bat").read_text(encoding="utf-8")
    assert '.venv\\Scripts\\clipforge.exe" ui' in launch and "pause" in launch


def test_setup_script_guards_user_data_and_uses_exit_codes():
    setup = (ROOT / "installer/setup.ps1").read_text(encoding="utf-8")
    assert "workspace" not in setup.split("# ---- 2.")[1].split("# ---- 3.")[0]  # the install step never touches user data
    assert "$LASTEXITCODE" in setup and 'ErrorActionPreference = "Continue"' in setup
    assert "--scope" in setup and "user" in setup and "runas" not in setup.lower()  # per-user Python, no elevation
    uninstall = (ROOT / "installer/uninstall.ps1").read_text(encoding="utf-8")
    assert "Delete them too" in uninstall  # explicit consent before deleting workspace/config


def test_install_guide_covers_download_install_open_and_logs():
    guide = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    for needle in ("Download", "Install ClipForge.bat", "shortcut", "setup.log", "Uninstall", "not** been run"):
        assert needle in guide, needle
