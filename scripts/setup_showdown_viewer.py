#!/usr/bin/env python3
"""Build a pinned, local, read-only Pokémon Showdown battle renderer.

Only the animation/replay subset is served. No login, room, socket, or full client
application script is included. Third-party outputs stay in ignored .runtime/.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CLIENT_COMMIT = "74056bbbb55d6ce15f9dd254186b94a51fe94b14"
ENGINE_COMMIT = "7244fe8e28fa8bc4d0809ae30d183c608b62bb2b"
CLIENT_REPO = "https://github.com/smogon/pokemon-showdown-client"
ENGINE_REPO = "https://github.com/smogon/pokemon-showdown"
CLIENT = ROOT / ".runtime/pokemon-showdown-client"
ENGINE = ROOT / ".runtime/pokemon-showdown"
OUTPUT = ROOT / ".runtime/showdown-viewer"
SITE = "play.pokemonshowdown.com"
METADATA = {
    "pokedex-mini.js": "108fd89933128e0384fea0c57d61bfec3ce233716f4b491120eb420d9ee9feeb",
    "pokedex-mini-bw.js": "41b951a54eb8cd9fd3fad42610c9405ab80c33aacab90abc8601d63737586e4a",
}
SOURCES = {
    "js/battledata.js": [
        "src/battle-dex.ts", "src/battle-teams.ts", "src/battle-dex-data.ts",
        "src/battle-log.ts", "src/battle-log-misc.js",
        "../caches/pokemon-showdown/server/chat-formatter.ts",
        "data/text/en.js", "src/battle-text-parser.ts",
    ],
    "js/battle-sound.js": ["src/battle-sound.ts"],
    "js/battle-scene-stub.js": ["src/battle-scene-stub.ts"],
    "js/battle-tooltips.js": ["src/battle-tooltips.ts"],
    "js/battle.js": ["src/battle.ts"],
    "data/graphics.js": ["src/battle-animations.ts", "src/battle-animations-moves.ts"],
}
DATA_FILES = [
    "pokedex.js", "moves.js", "abilities.js", "items.js",
    "teambuilder-tables.js", "typechart.js", "aliases.js",
]
STYLE_FILES = ["battle.css", "battle-log.css", "utilichart.css"]


def run(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd or ROOT, check=True)


def git_head(path: Path) -> str:
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def verify_checkout(path: Path, expected: str) -> None:
    if git_head(path) != expected:
        raise SystemExit(f"Refusing a different source checkout at {path}; expected {expected}")
    if subprocess.check_output(["git", "-C", str(path), "diff", "HEAD", "--name-only"], text=True).strip():
        raise SystemExit(f"Refusing modified tracked upstream source: {path}")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_url(relative: str) -> str:
    return f"{CLIENT_REPO}/blob/{CLIENT_COMMIT}/{SITE}/{relative}"


def main() -> None:
    if not (ENGINE / "dist/sim/index.js").is_file():
        raise SystemExit("Install the pinned Pokemon simulator first: scripts/setup_pokemon.sh")
    verify_checkout(ENGINE, ENGINE_COMMIT)
    if not CLIENT.exists():
        CLIENT.mkdir(parents=True)
        run("git", "init", str(CLIENT))
        run("git", "-C", str(CLIENT), "remote", "add", "origin", CLIENT_REPO + ".git")
        run("git", "-C", str(CLIENT), "fetch", "--depth", "1", "origin", CLIENT_COMMIT)
        run("git", "-C", str(CLIENT), "checkout", "--detach", "FETCH_HEAD")
    verify_checkout(CLIENT, CLIENT_COMMIT)
    npm_stamp = CLIENT / "node_modules/.viewer-package-lock.sha256"
    lock_hash = digest(CLIENT / "package-lock.json")
    if not npm_stamp.is_file() or npm_stamp.read_text().strip() != lock_hash:
        run("npm", "ci", "--ignore-scripts", cwd=CLIENT)
        npm_stamp.write_text(lock_hash + "\n")

    cache = CLIENT / "caches/pokemon-showdown"
    if cache.exists() and cache.resolve() != ENGINE.resolve():
        raise SystemExit("Refusing an unexpected simulator cache; expected the pinned local engine")
    if not cache.exists():
        cache.symlink_to(ENGINE, target_is_directory=True)
    run("node", "build-tools/build-indexes", "--no-update", cwd=CLIENT)
    site = CLIENT / SITE
    for name, expected in METADATA.items():
        target = site / "data" / name
        if not target.is_file() or digest(target) != expected:
            with tempfile.TemporaryDirectory(prefix="showdown-metadata-") as temp:
                download = Path(temp) / name
                run("curl", "--fail", "--location", "--silent", "--show-error", "--max-time", "30",
                    "--output", str(download), f"https://{SITE}/data/{name}")
                if digest(download) != expected:
                    raise SystemExit(f"Upstream sprite metadata changed: {name}; review a new hash before rebuilding")
                shutil.copyfile(download, target)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    compilation = {str(OUTPUT / dest): [str(site / source) for source in sources] for dest, sources in SOURCES.items()}
    compiler = r"""
const fs = require('node:fs'), path = require('node:path'), babel = require('@babel/core');
const jobs = JSON.parse(process.argv[1]);
const opts = {...eval('(' + fs.readFileSync('.babelrc', 'utf8') + ')'), babelrc:false,
  comments:true, sourceMaps:false, ignore:[], compact:true};
for (const [destination, sources] of Object.entries(jobs)) {
  fs.mkdirSync(path.dirname(destination), {recursive:true});
  const contents = sources.map(source => babel.transformFileSync(source, opts).code).join('\n');
  fs.writeFileSync(destination, contents + '\n');
}
"""
    run("node", "-e", compiler, json.dumps(compilation), cwd=CLIENT)
    manifest: dict = {
        "schema_version": 1,
        "client": {"commit": CLIENT_COMMIT, "repository": CLIENT_REPO,
                   "license_url": f"{CLIENT_REPO}/blob/{CLIENT_COMMIT}/LICENSE"},
        "engine": {"commit": ENGINE_COMMIT, "repository": ENGINE_REPO},
        "purpose": "Local read-only Battle renderer; no login, websocket or action controls",
        "remote_artwork": f"https://{SITE}/sprites/",
        "license_note": "Client overall AGPL-3.0; battle animation JS declares MIT; battle.css GPL-2.0; other styles retain upstream terms; artwork has separate rights. See each source.",
        "files": {},
    }

    def record(relative: str, sources: list[str], license_name: str) -> None:
        target = OUTPUT / relative
        mime = "text/javascript" if target.suffix == ".js" else mimetypes.guess_type(relative)[0] or "application/octet-stream"
        manifest["files"][relative] = {
            "sha256": digest(target), "mime": mime, "bytes": target.stat().st_size,
            "source_url": sources[0], "sources": sources, "license": license_name,
        }

    for dest, sources in SOURCES.items():
        links = [
            f"{ENGINE_REPO}/blob/{ENGINE_COMMIT}/server/chat-formatter.ts" if "chat-formatter" in source
            else f"{CLIENT_REPO}/blob/{CLIENT_COMMIT}/build-tools/build-translations" if source == "data/text/en.js"
            else source_url(source)
            for source in sources
        ]
        record(dest, links, "MIT (battle replay/animation subset); retain upstream source notices")

    def copy(relative: str, license_name: str, source: str | None = None) -> None:
        target = OUTPUT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(site / relative, target)
        record(relative, [source or source_url(relative)], license_name)

    for name in DATA_FILES:
        copy("data/" + name, "MIT simulator data / upstream client generator",
             f"{CLIENT_REPO}/blob/{CLIENT_COMMIT}/build-tools/build-indexes")
    for name in METADATA:
        copy("data/" + name, "Upstream generated sprite dimensions; hash pinned, no artwork",
             f"https://{SITE}/data/{name}")
    copy("js/lib/jquery-1.11.0.min.js", "MIT; jQuery Foundation")
    copy("js/lib/html-sanitizer-minified.js", "Upstream bundled Caja sanitizer; Apache-2.0")
    manifest["files"]["js/lib/html-sanitizer-minified.js"]["sources"].extend([
        "https://github.com/googlearchive/caja/blob/master/src/com/google/caja/plugin/html-sanitizer.js",
        "https://www.apache.org/licenses/LICENSE-2.0",
    ])
    for name in STYLE_FILES:
        copy("style/" + name, "GPL-2.0" if name == "battle.css" else "Upstream client terms (AGPL-3.0 unless specified)")
    # Only CSS-relative images are local. Move effects and Pokemon art remain
    # direct official-site image requests; no remote scripts are executed.
    import re
    for name in STYLE_FILES:
        css = (site / "style" / name).read_text()
        for relative in re.findall(r"url\(['\"]?(\.\./fx/[^)'\"?]+)", css):
            asset = relative.removeprefix("../")
            if asset not in manifest["files"]:
                copy(asset, "Upstream FX artwork: see battle-animations.ts per-file notices")
    license_dest = OUTPUT / "licenses/upstream-client-LICENSE.txt"
    license_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CLIENT / "LICENSE", license_dest)
    record("licenses/upstream-client-LICENSE.txt", [manifest["client"]["license_url"]], "AGPL-3.0 license text")
    shutil.copyfile(ENGINE / "LICENSE", OUTPUT / "licenses/upstream-server-MIT.txt")
    record("licenses/upstream-server-MIT.txt", [f"{ENGINE_REPO}/blob/{ENGINE_COMMIT}/LICENSE"],
           "MIT license text, copyright and disclaimer for the simulator/chat formatter")
    apache_license = CLIENT / "node_modules/google-auth-library/LICENSE"
    if apache_license.is_file() and "END OF TERMS AND CONDITIONS" in apache_license.read_text():
        shutil.copyfile(apache_license, OUTPUT / "licenses/Apache-2.0.txt")
        record("licenses/Apache-2.0.txt", ["https://www.apache.org/licenses/LICENSE-2.0"],
               "Apache-2.0 license text; copied from the lockfile-installed google-auth-library/LICENSE")
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"Built {len(manifest['files'])} pinned viewer assets in {OUTPUT}")


if __name__ == "__main__":
    main()
