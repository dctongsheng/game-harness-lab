"""Publish a successful Windows build, bound to its exact source and verification."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PATHS = [
    "platform", "integrations", "desktop", "scripts/build_genshin_desktop.py",
    "scripts/genshin_desktop_entry.py", "scripts/setup_genshin_bridge.py",
    "scripts/test_genshin_installer.ps1", "requirements-desktop.lock",
    "requirements-genshin.lock", ".gitattributes", ".github/workflows/genshin.yml",
]


def verified_source(run, verification, request, digest, repository):
    if (run.get("conclusion") != "success" or run.get("status") != "completed" or
            run.get("head_branch") != "hhr-yuanshen" or
            run.get("path") != ".github/workflows/genshin.yml" or
            run.get("repository", {}).get("full_name") != repository or
            run.get("id") != request["build_run_id"]):
        raise ValueError("Publication requires the successful requested Windows build in this repository")
    source = run.get("head_sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", source) or verification.get("source_commit") != source:
        raise ValueError("Installer verification belongs to a different source commit")
    if verification.get("platform_version") != request["platform_version"]:
        raise ValueError("Installer runtime version mismatch")
    if verification.get("installer_sha256") != digest:
        raise ValueError("Verified installer hash mismatch")
    for field in ("installation_verified", "uninstall_verified", "frozen_runtime_verified",
                  "chinese_decoder_verified", "desktop_controls_verified"):
        if verification.get(field) is not True:
            raise ValueError(f"Missing Windows verification: {field}")
    for field in ("genshin_live_verified", "microphone_verified", "asr_accuracy_verified"):
        if verification.get(field) is not False:
            raise ValueError(f"Publication must retain the actual verification boundary: {field}")
    return source


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REF") != "refs/heads/hhr-yuanshen":
        raise SystemExit("Run this publication job on the hhr-yuanshen GitHub Actions branch")
    repository = os.environ["GITHUB_REPOSITORY"]
    request = json.loads((ROOT / "windows/publish-request.json").read_text())
    if type(request["build_run_id"]) is not int or request["build_run_id"] <= 0:
        raise ValueError("Invalid build run")
    if not re.fullmatch(r"\d+\.\d+\.\d+", request["platform_version"]):
        raise ValueError("Invalid platform version")
    run_id = request["build_run_id"]
    run = json.loads(subprocess.check_output(["gh", "api", f"repos/{repository}/actions/runs/{run_id}"]))
    artifacts = json.loads(subprocess.check_output(["gh", "api", f"repos/{repository}/actions/runs/{run_id}/artifacts"]))
    matching = [item for item in artifacts["artifacts"]
                if item["name"] == "StartLux-Genshin-Windows-preview" and not item["expired"]]
    if len(matching) != 1:
        raise ValueError("Expected one unexpired verified Windows artifact")
    filename = f'StartLux-Genshin-{request["platform_version"]}-preview-win-x64.exe'
    artifact = Path(os.environ["STARTLUX_PREVIEW_ARTIFACT"])
    installer = artifact / filename
    with installer.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("Downloaded artifact is not a Windows executable")
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    verification = json.loads((artifact / "verification.json").read_text(encoding="utf-8-sig"))
    source = verified_source(run, verification, request, digest, repository)
    subprocess.run(["git", "merge-base", "--is-ancestor", source, "HEAD"], cwd=ROOT, check=True)
    # Publishing documentation may be newer, but the shipped runtime must match the branch.
    subprocess.run(["git", "diff", "--quiet", source, "HEAD", "--", *RUNTIME_PATHS], cwd=ROOT, check=True)
    destination = ROOT / "windows"
    shutil.copyfile(installer, destination / filename)
    shutil.copyfile(artifact / "desktop.png", destination / "desktop-preview.png")
    shutil.copyfile(artifact / "verification.json", destination / "windows-verification.json")
    (destination / (filename + ".sha256")).write_text(f"{digest}  {filename}\n", encoding="ascii")
    build_url = f"https://github.com/{repository}/actions/runs/{run_id}"
    (destination / "installer-source.json").write_text(json.dumps({
        "source_commit": source, "build_run": build_url,
        "artifact_id": matching[0]["id"], "installer_sha256": digest,
        "platform_version": request["platform_version"],
        "release_status": "development", "genshin_live_verified": False,
    }, indent=2) + "\n", encoding="utf-8")
    for old in destination.glob("StartLux-Genshin-*-preview-win-x64.exe*"):
        if old.name not in (filename, filename + ".sha256"):
            old.unlink()
    launcher = ROOT / "Try-Genshin.cmd"
    content = launcher.read_text(encoding="utf-8")
    content = re.sub(r"StartLux-Genshin-\d+\.\d+\.\d+-preview-win-x64\.exe", filename, content)
    content, count = re.subn(r"-ne '[0-9a-f]{64}'", f"-ne '{digest}'", content)
    if count != 1:
        raise ValueError("Could not update the installer launcher hash")
    launcher.write_text(content, encoding="utf-8")
    for document in (ROOT / "README.md", destination / "README.md"):
        content = document.read_text(encoding="utf-8")
        content = re.sub(r"StartLux-Genshin-\d+\.\d+\.\d+-preview-win-x64\.exe", filename, content)
        if document.parent == destination:
            content = re.sub(r"本安装包来自已通过验证的源码版本 `[^`]+`", f"本安装包来自已通过验证的源码版本 `{source}`", content)
            content = re.sub(r"https://github.com/[^)]+/actions/runs/\d+", build_url, content)
        document.write_text(content, encoding="utf-8")
    subprocess.run(["git", "lfs", "install", "--local"], cwd=ROOT, check=True)
    subprocess.run(["git", "add", "windows/", "Try-Genshin.cmd", "README.md"], cwd=ROOT, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        print("Verified Windows preview is already published")
        return
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=ROOT, check=True)
    subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-m", f'Publish verified StartLux {request["platform_version"]} Windows preview'], cwd=ROOT, check=True)
    subprocess.run(["git", "pull", "--rebase", "origin", "hhr-yuanshen"], cwd=ROOT, check=True)
    subprocess.run(["git", "diff", "--quiet", source, "HEAD", "--", *RUNTIME_PATHS], cwd=ROOT, check=True)
    subprocess.run(["git", "push", "origin", "HEAD:hhr-yuanshen"], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
