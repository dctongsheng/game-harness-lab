"""Separate private evaluation evidence from public UI and published artifacts."""
from copy import deepcopy


PRIVATE_KEYS = {'seed', 'evaluation_seed', 'train_seeds', 'validation_seeds', 'promotion_seeds',
                'criteria', 'raw_response', 'raw_request', 'rejected_or_pending_receipt', 'probabilities'}


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
        'actor', 'phase', 'trial_design', 'applied_action') if key in snapshot}
    result.update(public_view=view, render=view, cells=view.get('cells', []),
                  rows=view.get('rows'), cols=view.get('cols'), visibility='public_common_knowledge')
    return result


def public_payload(value, restricted_games, private=False):
    """Redact snapshots and source seeds across nested state, event, and replay APIs."""
    if isinstance(value, list):
        return [public_payload(item, restricted_games, private) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    private = private or value.get('game_id') in restricted_games
    if 'public_view' in value and isinstance(value['public_view'], dict):
        return strip_private_fields(public_snapshot(value))
    if private:
        # A missing projection must not silently fall back to either player's
        # private board, even for a historical or malformed snapshot.
        value = {key: item for key, item in value.items()
                 if key not in {'cells', 'observation', 'render', 'public_view'}}
    return {key: public_payload(item, restricted_games, private) for key, item in value.items()
            if not private or key not in PRIVATE_KEYS}
