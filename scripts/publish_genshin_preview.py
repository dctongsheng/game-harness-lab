"""Publish only the already verified Windows installer to the requested branch."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = "StartLux-Genshin-0.2.0-preview-win-x64.exe"
SHA256 = "897c75e01f0a898051fda54aa665ffab49734f0f4b0418baffb8ad3bad0cdad1"


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REF") != "refs/heads/hhr-yuanshen":
        raise SystemExit("Run this publication job on the hhr-yuanshen GitHub Actions branch")
    artifact = Path(os.environ["STARTLUX_PREVIEW_ARTIFACT"])
    installer = artifact / INSTALLER
    with installer.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    verification = json.loads((artifact / "verification.json").read_text(encoding="utf-8-sig"))
    if digest != SHA256 or verification.get("installer_sha256") != SHA256:
        raise ValueError("Verified installer hash mismatch")
    for field in ("installation_verified", "uninstall_verified", "frozen_runtime_verified",
                  "chinese_decoder_verified", "desktop_controls_verified"):
        if verification.get(field) is not True:
            raise ValueError(f"Missing Windows verification: {field}")
    if verification.get("genshin_live_verified") is not False:
        raise ValueError("Publication must retain the actual live-game verification boundary")
    destination = ROOT / "windows"
    destination.mkdir(exist_ok=True)
    shutil.copyfile(installer, destination / INSTALLER)
    shutil.copyfile(artifact / "desktop.png", destination / "desktop-preview.png")
    shutil.copyfile(artifact / "verification.json", destination / "windows-verification.json")
    (destination / (INSTALLER + ".sha256")).write_text(f"{SHA256}  {INSTALLER}\n", encoding="ascii")
    (destination / "installer-source.json").write_text(json.dumps({
        "source_commit": "60755a863781fe392b296a73ca397223e81eb4b3",
        "build_run": "https://github.com/dctongsheng/game-harness-lab/actions/runs/39118291723",
        "artifact_id": 12133336963, "installer_sha256": SHA256,
        "release_status": "development", "genshin_live_verified": False,
    }, indent=2) + "\n", encoding="utf-8")
    subprocess.run(["git", "lfs", "install", "--local"], cwd=ROOT, check=True)
    subprocess.run(["git", "add", "windows/"], cwd=ROOT, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        print("Verified Windows preview is already published")
        return
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=ROOT, check=True)
    subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-m", "Publish verified Windows Genshin preview installer"], cwd=ROOT, check=True)
    subprocess.run(["git", "pull", "--rebase", "origin", "hhr-yuanshen"], cwd=ROOT, check=True)
    subprocess.run(["git", "push", "origin", "HEAD:hhr-yuanshen"], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
