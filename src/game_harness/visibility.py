"""Separate private evaluation evidence from public UI and published artifacts."""
from copy import deepcopy


PRIVATE_KEYS = {'seed', 'evaluation_seed', 'train_seeds', 'validation_seeds', 'promotion_seeds',
                'criteria', 'raw_response', 'raw_request', 'request_payload', 'request_evidence',
                'rejected_or_pending_receipt', 'probabilities'}


def strip_private_fields(value):
    if isinstance(value, dict):
        return {key: strip_private_fields(item) for key, item in value.items() if key not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [strip_private_fields(item) for item in value]
    return deepcopy(value)


def public_snapshot(snapshot):
    """Use only the adapter's common-knowledge projection, including at game end."""
    view = deepcopy(snapshot['public_view'])
    result = {key: deepcopy(snapshot[key]) for key in (
        'game_id', 'rules_version', 'step', 'max_steps', 'score', 'done', 'reason', 'result',
        'winner', 'truncated', 'score_kind', 'turn', 'candidate_seat', 'policy_role',
        'actor', 'phase', 'trial_design', 'applied_action', 'native_turn',
        'native_terminal', 'format', 'format_label') if key in snapshot}
    result.update(public_view=view, render=view, cells=view.get('cells', []),
                  rows=view.get('rows'), cols=view.get('cols'), visibility='public_common_knowledge')
    if snapshot.get('game_id') == 'pokemon':
        result.pop('applied_action', None)
    return result


def public_payload(value, restricted_games, private=False, game_id=None):
    """Redact snapshots and source seeds across nested state, event, and replay APIs."""
    if isinstance(value, list):
        return [public_payload(item, restricted_games, private, game_id) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    game_id = value.get('game_id', game_id)
    private = private or game_id in restricted_games
    if 'public_view' in value and isinstance(value['public_view'], dict):
        return strip_private_fields(public_snapshot(value))
    if private:
        # A missing projection must not silently fall back to either player's
        # private board, even for a historical or malformed snapshot.
        private_observations = {'cells', 'observation', 'render', 'public_view'}
        if game_id == 'pokemon':
            private_observations.update({'team', 'opponent_team', 'stats', 'moves', 'request',
                                         'requests', 'pending_action', 'pending_choice', 'pending_choices'})
        value = {key: item for key, item in value.items() if key not in private_observations}
    banned = PRIVATE_KEYS | ({'action', 'applied_action', 'choice_id', 'last_action',
                              'candidate_hash', 'observation_hash'} if game_id == 'pokemon' else set())
    return {key: public_payload(item, restricted_games, private, game_id) for key, item in value.items()
            if not private or key not in banned}
