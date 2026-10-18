"""Images use the real provider contract, never an alternate action picker."""
from copy import deepcopy
import hashlib
import json

import httpx
import pytest

pytest.importorskip('PIL', reason='Install .[vision] to run visual tests')

from game_harness.games import get_game
from game_harness.providers import DecisionProvider, ProviderError, Settings, digest
from game_harness.vision_provider import VisionDecisionProvider, restore_payload


@pytest.fixture
def state():
    session = get_game('tetris').create(1337, {'max_steps': 80, 'board_size': 8})
    try:
        value = session.snapshot()
        value['episode_id'] = 'ep-fixture'
        return value
    finally:
        session.close()


CHOICES = [{'id': 'left:100', 'label': 'left'}, {'id': 'right:100', 'label': 'right'}]


def settings(key_file=''):
    return Settings('http://127.0.0.1:1/v1/systemone', 'fixture-model', 1,
                    'http://127.0.0.1:1', 'fixture-coach', '', 1, str(key_file))


def answer(thought=None):
    return {'model': 'fixture-model', 'answers': {'primary_action': {
        'choice': 'right:100', 'confidence': .8,
        'probabilities': {'left:100': .1, 'right:100': .9},
        'details': {'thought': thought if thought is not None else {'used': False, 'tokens': 0}}}},
        'usage': {'input_tokens': 123, 'output_tokens': 0}}


def test_vision_is_only_outgoing_difference_and_pixels_match_archive(tmp_path, state):
    sent = []
    fake_secret = 'unit-test-not-a-real-secret'
    key_file = tmp_path / 'api.key'
    key_file.write_text(fake_secret)
    def handler(request):
        assert request.headers['authorization'] == 'Bearer ' + fake_secret
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=answer())
    original = deepcopy(state)
    receipts = []
    for arm, enabled in [('text', False), ('image', True)]:
        client = httpx.Client(transport=httpx.MockTransport(handler))
        p = VisionDecisionProvider(settings(key_file), enabled=enabled,
                                   archive_dir=tmp_path/arm, image_store=tmp_path/'images', client=client)
        receipts.append(p.decide(state, 'same instructions', CHOICES, 'same-request'))
        p.close()
    assert state == original
    assert {k: v for k, v in sent[1].items() if k != 'images'} == sent[0]
    assert 'images' not in sent[0] and len(sent[1]['images']) == 1
    assert [r['choice_id'] for r in receipts] == ['right:100', 'right:100']
    assert receipts[0]['vision'] == {'enabled': False}
    meta = receipts[1]['vision']
    assert meta['enabled'] and meta['source_observation_hash'] == digest(state['observation'])
    for arm, payload in zip(['text', 'image'], sent):
        directory = tmp_path/arm/digest('same-request')[:32]
        record = json.loads((directory/'payload.json').read_text())
        archive_meta = json.loads((directory/'payload_meta.json').read_text())
        restored = restore_payload(record, tmp_path/'images')
        assert restored == payload
        assert archive_meta['payload_sha256'] == digest(payload)
        wire = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        assert archive_meta['wire_json_sha256'] == hashlib.sha256(wire).hexdigest()
        assert fake_secret not in ''.join(p.read_text() for p in directory.glob('*.json'))


def test_disabled_arm_is_exact_ordinary_provider_payload(tmp_path, state):
    requests = []
    def handler(request):
        requests.append(request.content)
        return httpx.Response(200, json=answer())
    normal = DecisionProvider(settings(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    normal.decide(state, 'same policy', CHOICES, 'same-id')
    disabled = VisionDecisionProvider(settings(), enabled=False, archive_dir=tmp_path/'calls',
                image_store=tmp_path/'images', client=httpx.Client(transport=httpx.MockTransport(handler)))
    disabled.decide(state, 'same policy', CHOICES, 'same-id')
    assert requests[0] == requests[1]
    assert not (tmp_path/'images').exists()
    normal.close(); disabled.close()


def test_same_pixels_are_deduplicated_and_duplicate_request_is_not_resent(tmp_path, state):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=answer())
    provider = VisionDecisionProvider(settings(), enabled=True, archive_dir=tmp_path/'calls',
                image_store=tmp_path/'images', client=httpx.Client(transport=httpx.MockTransport(handler)))
    provider.decide(state, 'policy', CHOICES, 'goal')
    provider.decide(state, 'policy', CHOICES, 'action')
    assert len(list((tmp_path/'images').glob('*.png'))) == 1
    with pytest.raises(ProviderError, match='overwrite'):
        provider.decide(state, 'policy', CHOICES, 'action')
    assert len(calls) == 2
    provider.close()


@pytest.mark.parametrize('thought', [{}, {'used': True, 'tokens': 3}, {'used': False, 'tokens': None}])
def test_unverified_or_active_thinking_never_yields_accepted_receipt(tmp_path, state, thought):
    provider = VisionDecisionProvider(settings(), enabled=False, archive_dir=tmp_path/'calls',
                image_store=tmp_path/'images', client=httpx.Client(transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json=answer(thought)))))
    with pytest.raises(ProviderError):
        provider.decide(state, 'policy', CHOICES, 'bad-think')
    directory = tmp_path/'calls'/digest('bad-think')[:32]
    assert (directory/'response.json').is_file() and not (directory/'receipt.json').exists()
    provider.close()


@pytest.mark.parametrize('mutate', [
    lambda raw: raw['usage'].update(output_tokens=1),
    lambda raw: raw['usage'].update(output_tokens=False),
    lambda raw: raw['answers']['primary_action']['details'].update(after_thought={}),
    lambda raw: raw['answers']['primary_action']['details']['thought'].update(ms=1),
    lambda raw: raw['answers']['primary_action']['details']['thought'].update(ended='complete'),
    lambda raw: raw.update(timing_ms={'thought_span': 1}),
    lambda raw: raw.update(timing_ms={'after_thought_sum': 1}),
])
def test_contradictory_telemetry_cannot_be_accepted_as_thinking_off(tmp_path, state, mutate):
    raw = answer()
    mutate(raw)
    provider = VisionDecisionProvider(settings(), enabled=False, archive_dir=tmp_path/'calls',
                image_store=tmp_path/'images', client=httpx.Client(transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json=raw))))
    with pytest.raises(ProviderError):
        provider.decide(state, 'policy', CHOICES, 'contradiction')
    assert not (tmp_path/'calls'/digest('contradiction')[:32]/'receipt.json').exists()
    provider.close()


def test_archive_rejects_tampered_image_or_reference_escape(tmp_path, state):
    provider = VisionDecisionProvider(settings(), enabled=True, archive_dir=tmp_path/'calls',
                image_store=tmp_path/'images', client=httpx.Client(transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json=answer()))))
    provider.decide(state, 'policy', CHOICES, 'test')
    record = json.loads((tmp_path/'calls'/digest('test')[:32]/'payload.json').read_text())
    altered = deepcopy(record); altered['images'][0]['path'] = '../api.key'
    with pytest.raises(ProviderError): restore_payload(altered, tmp_path/'images')
    (tmp_path/'images'/record['images'][0]['path']).write_bytes(b'corrupt')
    with pytest.raises(ProviderError, match='hash mismatch'): restore_payload(record, tmp_path/'images')
    provider.close()
