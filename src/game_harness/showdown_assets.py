"""Serve only the pinned, locally built upstream battle renderer assets."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

from .providers import ROOT

VIEWER_ROOT = ROOT / '.runtime/showdown-viewer'
CLIENT_COMMIT = '74056bbbb55d6ce15f9dd254186b94a51fe94b14'


def manifest(root=None):
    root = Path(root or VIEWER_ROOT)
    data = json.loads((root / 'manifest.json').read_text())
    if data.get('schema_version') != 1 or data.get('client', {}).get('commit') != CLIENT_COMMIT:
        raise ValueError('Showdown viewer version mismatch; run scripts/setup_showdown_viewer.py')
    if not isinstance(data.get('files'), dict) or not data['files']:
        raise ValueError('Showdown viewer asset manifest is empty')
    return data


def asset(name, root=None):
    root = Path(root or VIEWER_ROOT).resolve()
    if (not isinstance(name, str) or '\\' in name or PurePosixPath(name).is_absolute()
            or any(part in {'.', '..'} for part in name.split('/'))):
        raise ValueError('Invalid viewer asset path')
    item = manifest(root)['files'].get(name)
    if not isinstance(item, dict):
        raise FileNotFoundError(name)
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise FileNotFoundError(name)
    if hashlib.sha256(path.read_bytes()).hexdigest() != item.get('sha256'):
        raise ValueError('Showdown viewer asset integrity check failed')
    mime = item.get('mime')
    if mime not in {'text/javascript', 'application/javascript', 'text/css', 'application/json',
                    'text/plain', 'image/png', 'image/gif', 'image/svg+xml', 'image/webp'}:
        raise ValueError('Unsupported viewer asset type')
    return path, mime
