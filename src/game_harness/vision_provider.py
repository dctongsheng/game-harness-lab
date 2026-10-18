"""Optional Tetris images on the real Decision transport, with exact evidence.

The ordinary provider still builds and validates the text request and response.
This wrapper adds one current-observation PNG to the official top-level images
field. It never edits state, instructions, legal choices, or the selected action.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
from pathlib import Path
import struct

import httpx

from .providers import DecisionProvider, ProviderError, digest
from .store import atomic_json


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_thinking_off(receipt):
    """Require explicit thinking-off evidence and reject contradictory telemetry."""
    if receipt.get('think_used') is not False or type(receipt.get('reasoning_tokens')) is not int or receipt['reasoning_tokens'] != 0:
        raise ProviderError('Vision experiment requires explicit thinking-off telemetry')
    raw = receipt['raw_response']
    details = raw['answers']['primary_action']['details']
    thought = details.get('thought', {})
    if thought.get('used') is not False or type(thought.get('tokens')) is not int or thought['tokens'] != 0:
        raise ProviderError('Raw response contradicts thinking-off receipt')
    checks = [(raw.get('usage', {}), 'output_tokens'), (thought, 'ms'),
              (raw.get('timing_ms', {}), 'thought_span'),
              (raw.get('timing_ms', {}), 'after_thought_sum')]
    for telemetry, key in checks:
        if key in telemetry and (type(telemetry[key]) not in (int, float) or telemetry[key] != 0):
            raise ProviderError('Response contains contradictory thinking telemetry')
    if ('ended' in thought and thought['ended'] != 'skipped') or details.get('after_thought') is not None:
        raise ProviderError('Response contains a reasoning pass despite thinking disabled')


def restore_payload(record, image_store):
    """Recreate the exact outgoing JSON value from hash-addressed local PNGs."""
    result = deepcopy(record)
    if 'images' not in result:
        return result
    restored = []
    for reference in result['images']:
        identity = reference.get('sha256', '') if isinstance(reference, dict) else ''
        if (len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity)
                or reference.get('mime') != 'image/png' or reference.get('path') != identity + '.png'):
            raise ProviderError('Invalid archived image reference')
        raw = (Path(image_store) / (identity + '.png')).read_bytes()
        if sha256(raw) != identity:
            raise ProviderError('Archived image hash mismatch')
        restored.append('data:image/png;base64,' + base64.b64encode(raw).decode('ascii'))
    result['images'] = restored
    return result


class _ImageArchiveClient:
    def __init__(self, client):
        self.client = client
        self.pending = None

    def get(self, *args, **kwargs):
        return self.client.get(*args, **kwargs)

    def close(self):
        self.client.close()

    def post(self, url, *, json, headers):
        if self.pending is None:
            raise ProviderError('Missing archived request identity')
        directory, descriptor, raw = self.pending
        payload = deepcopy(json)
        if 'images' in payload:
            raise ProviderError('Unexpected pre-existing image input')
        if descriptor['enabled']:
            payload['images'] = ['data:image/png;base64,' + base64.b64encode(raw).decode('ascii')]
        record = deepcopy(payload)
        if descriptor['enabled']:
            record['images'] = [{'sha256': descriptor['sha256'], 'mime': 'image/png',
                                 'path': descriptor['sha256'] + '.png'}]
        # Save the attempted input before sending; never archive HTTP headers.
        atomic_json(directory / 'payload.json', record)
        meta = {'schema': 'decision-vision-payload-v1', 'payload_sha256': digest(payload),
                'vision': deepcopy(descriptor), 'status': 'pending'}
        atomic_json(directory / 'payload_meta.json', meta)
        response = self.client.post(url, json=payload, headers=headers)
        # This digest covers the actual bytes serialized by httpx, not just a
        # separately reconstructed request object.
        meta.update(status='responded', http_status=response.status_code,
                    wire_json_sha256=sha256(response.request.content))
        atomic_json(directory / 'payload_meta.json', meta)
        try:
            body = response.json()
        except ValueError:
            # Arbitrary error bodies can include proxy internals; retain only
            # the status here. No invalid response is accepted as a game action.
            body = {'invalid_json_response': True, 'http_status': response.status_code}
        atomic_json(directory / 'response.json', body)
        return response


class VisionDecisionProvider(DecisionProvider):
    """One provider per serial runner; both arms keep identical text inputs."""
    def __init__(self, settings, *, enabled, image_store, archive_dir, client=None):
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean')
        self.enabled = enabled
        self.image_store = Path(image_store)
        self.archive_dir = Path(archive_dir)
        transport = _ImageArchiveClient(client or httpx.Client(timeout=settings.decision_timeout, trust_env=False))
        super().__init__(settings, client=transport)

    def prepare_input(self, state):
        """Save deterministic pixels; no API request and no policy decision."""
        if not self.enabled:
            return {'enabled': False}
        from .tetris_vision import render_tetris_png
        raw = render_tetris_png(state)
        if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ProviderError('Renderer did not produce PNG')
        width, height = struct.unpack('>II', raw[16:24])
        identity = sha256(raw)
        self.image_store.mkdir(parents=True, exist_ok=True)
        path = self.image_store / (identity + '.png')
        try:
            with path.open('xb') as output:
                output.write(raw)
        except FileExistsError:
            if sha256(path.read_bytes()) != identity:
                raise ProviderError('Existing image evidence is corrupt') from None
        return {'enabled': True, 'sha256': identity, 'mime': 'image/png',
                'width': width, 'height': height,
                'source_observation_hash': digest(state['observation'])}

    def decide(self, state, instructions, criteria, request_id):
        directory = self.archive_dir / digest(request_id)[:32]
        directory.mkdir(parents=True, exist_ok=True)
        if (directory / 'payload.json').exists():
            raise ProviderError('Refusing to repeat or overwrite an archived request')
        descriptor = self.prepare_input(state)
        raw = ((self.image_store / (descriptor['sha256'] + '.png')).read_bytes()
               if descriptor['enabled'] else None)
        self.client.pending = (directory, descriptor, raw)
        try:
            receipt = super().decide(state, instructions, criteria, request_id)
            validate_thinking_off(receipt)
            receipt['vision'] = descriptor
            atomic_json(directory / 'receipt.json', receipt)
            return receipt
        finally:
            self.client.pending = None
