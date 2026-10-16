"""Bounded JSON IPC for deterministic, network-free bundled rules engines."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import threading
import time


class RulesBridgeError(RuntimeError):
    pass


class NodeBridge:
    def __init__(self, script_name: str, *, timeout: float = 10, node_binary: str | None = None):
        if Path(script_name).name != script_name or not script_name.endswith('.cjs'):
            raise ValueError('Rules bridge must be a bundled .cjs filename')
        script = Path(__file__).with_name(script_name)
        if not script.is_file():
            raise RulesBridgeError('Bundled rules bridge is missing')
        node = node_binary or shutil.which('node')
        if not node:
            raise RulesBridgeError('这些游戏需要 Node.js 18 或更新版本')
        self.timeout = timeout
        self._closed = False
        self._serial = 0
        self._buffer = b''
        self._lock = threading.Lock()
        try:
            # Do not pass model credentials or the service environment to rules code.
            self._process = subprocess.Popen(
                [node, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0, cwd=script.parent,
                env={'PATH': os.environ.get('PATH', ''), 'LANG': 'C.UTF-8'},
            )
            self._selector = selectors.DefaultSelector()
            self._selector.register(self._process.stdout, selectors.EVENT_READ)
        except Exception:
            self.close()
            raise

    def request(self, payload: dict):
        with self._lock:
            if self._closed:
                raise RulesBridgeError('Rules bridge is closed')
            self._serial += 1
            encoded = json.dumps({**payload, 'id': self._serial}, allow_nan=False).encode() + b'\n'
            if len(encoded) > 2_000_000:
                raise RulesBridgeError('Rules request exceeds size limit')
            try:
                self._process.stdin.write(encoded)
                self._process.stdin.flush()
                deadline = time.monotonic() + self.timeout
                while b'\n' not in self._buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not self._selector.select(remaining):
                        raise RulesBridgeError('Rules bridge response timed out')
                    chunk = os.read(self._process.stdout.fileno(), 65536)
                    if not chunk:
                        raise RulesBridgeError('Rules bridge exited unexpectedly')
                    self._buffer += chunk
                    if len(self._buffer) > 2_000_000:
                        raise RulesBridgeError('Rules response exceeds size limit')
                line, self._buffer = self._buffer.split(b'\n', 1)
                response = json.loads(line)
                if not isinstance(response, dict) or response.get('id') != self._serial:
                    raise RulesBridgeError('Rules response sequence mismatch')
                if response.get('ok') is not True:
                    # Never echo private engine state or an untrusted error payload.
                    raise RulesBridgeError('Rules engine rejected the operation')
                if 'value' not in response:
                    raise RulesBridgeError('Rules response omitted value')
                return response['value']
            except (BrokenPipeError, OSError, ValueError) as exc:
                self.close()
                raise RulesBridgeError('Rules bridge communication failed') from exc
            except RulesBridgeError:
                self.close()
                raise

    def close(self):
        if getattr(self, '_closed', True):
            return
        self._closed = True
        selector = getattr(self, '_selector', None)
        if selector:
            selector.close()
        process = getattr(self, '_process', None)
        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()
