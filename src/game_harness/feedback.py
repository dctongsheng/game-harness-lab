"""Complete, bounded coaching evidence; never a player or an action filter.

Known games use lossless compact views of their relevant observable state.
Budgets remove whole events, never substrings of serialized JSON. Full source
traces remain with the episode store; omission ranges refer to those traces.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re


COMPLETE_FORMAT = 'complete_events_v1'
LEGACY_FORMAT = 'legacy_excerpt_v1'
FORMATS = {COMPLETE_FORMAT, LEGACY_FORMAT}
_PRIVATE_KEYS = {'seed', 'evaluation_seed', 'train_seeds', 'validation_seeds',
                 'promotion_seeds', 'raw_request', 'raw_response', 'request_payload', 'request_evidence', 'probabilities'}
_COMMON = ('step', 'score', 'done', 'reason', 'result', 'winner', 'truncated',
           'turn', 'candidate_seat', 'candidate_color', 'viewer_seat', 'tick')
_ARMY_RANKS = dict(zip(('司令', '军长', '师长', '旅长', '团长', '营长', '连长', '排长', '工兵'),
                       ('commander', 'general', 'division', 'brigade', 'regiment', 'battalion', 'company', 'platoon', 'engineer')))
_ARMY_BOUND = re.compile(r'^>(司令|军长|师长|旅长|团长|营长|连长|排长|工兵)(或雷)?$')


def chars(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _clean(value):
    """Omit credential-shaped keys, without cutting strings or arrays."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if isinstance(k, str)
                and k not in _PRIVATE_KEYS
                and not any(word in k.lower().replace('-', '_')
                            for word in ('api_key', 'authorization', 'password', 'secret', 'token'))}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return '[unsupported value omitted]'


def _pick(value, names):
    return {key: _clean(value[key]) for key in names if key in value}


def _ranges(indices):
    ranges = []
    for index in sorted(set(indices)):
        if ranges and index == ranges[-1][1] + 1:
            ranges[-1][1] = index
        else:
            ranges.append([index, index])
    return ranges


def _board_rows(state, game):
    cells, columns = state.get('cells'), state.get('cols')
    if not isinstance(cells, list) or type(columns) is not int or columns < 1:
        return None
    values = []
    for piece in cells:
        if piece is None:
            values.append('.')
        elif not isinstance(piece, dict):
            values.append(str(piece))
        elif game == 'gomoku':
            values.append({0: 'X', 1: 'O'}.get(piece.get('seat'), '?'))
        else:
            # Even a malformed hidden piece must not expose its kind or label.
            kind = '?' if piece.get('hidden') or piece.get('kind') == 'unknown' else piece.get('kind', '?')
            values.append(f"{piece.get('seat', '?')}:{kind}")
    return [' '.join(values[i:i + columns]) for i in range(0, len(values), columns)]


def _army_visible_bounds(state):
    """Keep only the official observable inference grammar, never hidden kinds."""
    bounds = []
    for index, piece in enumerate(state.get('cells', [])):
        if not isinstance(piece, dict) or not (piece.get('hidden') or piece.get('kind') == 'unknown'):
            continue
        label = piece.get('label')
        match = _ARMY_BOUND.fullmatch(label) if isinstance(label, str) else None
        if match:
            bounds.append({'index': index, 'rank_above': _ARMY_RANKS[match[1]],
                           'or_mine': bool(match[2])})
    return bounds


_POKEMON_MOVE_COLUMNS = ('type', 'category', 'base_power', 'accuracy', 'priority')
_POKEMON_MOVE_EFFECTS = ('boosts', 'status', 'heal', 'drain', 'recoil', 'protect')


def _pokemon_move_context(frames):
    """Deduplicate rule facts from authorized candidate views only.

    These are a move dictionary, not a catalogue of the opponent's unobserved
    moves. Variable facts stay in the corresponding event instead of becoming
    a misleading episode-wide constant.
    """
    facts, effects, variable = {}, {}, set()
    for frame in frames:
        for pokemon in [*frame.get('team', []), *frame.get('opponent_team', [])]:
            for move in pokemon.get('moves', []):
                identity = move.get('id')
                if not isinstance(identity, str):
                    continue
                rule = [_clean(move.get(key)) for key in _POKEMON_MOVE_COLUMNS]
                effect = {key: _clean(move[key]) for key in _POKEMON_MOVE_EFFECTS
                          if move.get(key) not in (None, False, 0, {}, [])}
                if identity in facts and (facts[identity] != rule or effects[identity] != effect):
                    variable.add(identity)
                facts[identity], effects[identity] = rule, effect
    return {'move_rule_columns': list(_POKEMON_MOVE_COLUMNS),
            'move_rules': {key: value for key, value in facts.items() if key not in variable},
            'move_effects': {key: value for key, value in effects.items() if value and key not in variable},
            'pokemon_encoding': 'hp is [current,max], exact only for own team and public percentage otherwise; '
                'omitted boost entries are zero, missing status/item/ability are unknown or absent as in source, missing active means false; hp=0 means fainted. '
                'Species is omitted only when it equals the normalized ident suffix. Move rules are facts, not opponent move inventories. '
                'Own move pp/max_pp and own team slot remain per-event; opponent slots and unobserved moves are absent.'}


def _pokemon_mon(pokemon, private, context):
    result = _pick(pokemon, ('ident', 'species', 'types'))
    if isinstance(pokemon.get('ident'), str) and isinstance(pokemon.get('species'), str):
        suffix = re.sub(r'[^a-z0-9]', '', pokemon['ident'].split(':', 1)[-1].lower())
        if suffix == re.sub(r'[^a-z0-9]', '', pokemon['species'].lower()):
            result.pop('species', None)
    result.update({key: _clean(pokemon[key]) for key in ('active', 'status', 'ability', 'item')
                   if pokemon.get(key) not in (None, False)})
    result['hp'] = [_clean(pokemon.get('current_hp')), _clean(pokemon.get('max_hp'))]
    boosts = {key: _clean(value) for key, value in pokemon.get('boosts', {}).items() if value != 0}
    if boosts:
        result['boosts'] = boosts
    if private:
        result.update(_pick(pokemon, ('stats', 'slot')))
    moves = []
    for move in pokemon.get('moves', []):
        record = _pick(move, ('id', 'pp', 'max_pp') if private else ('id',))
        if move.get('id') not in context.get('move_rules', {}):
            record.update(_pick(move, (*_POKEMON_MOVE_COLUMNS, *_POKEMON_MOVE_EFFECTS)))
        moves.append(record)
    result['moves'] = moves
    return result


def _static_context(trace, game, private):
    rows = [row for row in trace if isinstance(row, dict)
            and (not private or row.get('policy_role') == 'candidate')]
    frames = [row.get('before', {}) for row in rows]
    if not frames:
        return {}
    context = {}
    catalogs = [[item['id'] for item in row.get('criteria', [])
                 if isinstance(item, dict) and 'id' in item] for row in rows]
    if catalogs[0] and all(catalog == catalogs[0] for catalog in catalogs):
        context['legal_actions'] = _clean(catalogs[0])
    if game in ('gomoku', 'army'):
        context.update(_pick(frames[0], ('rows', 'cols')))
        context['board_encoding'] = 'Rows top to bottom, cells left to right, . empty; ' + (
            'X black, O white, matching runtime observation.board characters; runtime cells[*].seat separately uses 0 black/1 white.' if game == 'gomoku' else 'seat:kind, ? unknown; no hidden identities. '
            'visible_bounds uses zero-based flat cell index: rank strictly above rank_above, OR mine if or_mine is true.')
    if game == 'army' and all(f.get('terrain') == frames[0].get('terrain') for f in frames):
        context.update(_pick(frames[0], ('terrain',)))
    if game == 'pokemon':
        context.update(_pick(frames[0], ('format', 'format_label')))
        context.update(_pokemon_move_context(frames))
    if game == 'overcooked':
        for key in ('layout', 'rows', 'cols', 'terrain', 'terrain_legend', 'orders', 'max_ticks'):
            if key in frames[0] and all(f.get(key) == frames[0][key] for f in frames):
                context[key] = _clean(frames[0][key])
    observations = [f.get('observation', {}) for f in frames]
    if game in ('platformer', 'tetris') and all(isinstance(o, dict) for o in observations):
        keys = ('width', 'height', 'level', 'platforms', 'hazards', 'exit', 'physics',
                'coordinates', 'units', 'geometry', 'tickRate') if game == 'platformer' else (
                    'width', 'height', 'hiddenRows', 'coordinates', 'tickRate')
        for key in keys:
            if key in observations[0] and all(o.get(key) == observations[0][key] for o in observations):
                context[key] = _clean(observations[0][key])
    return context


def compact_state(state, game, context=None):
    """Project one already-authorized view, retaining complete compact geometry."""
    context = context or {}
    if not isinstance(state, dict):
        return {'unavailable': True}
    result = _pick(state, _COMMON)
    if game == 'snake':
        result.update(_pick(state, ('size', 'snake', 'head', 'food', 'direction')))
    elif game == 'xiangqi':
        result.update(_pick(state, ('fen', 'in_check', 'material', 'candidate_material_advantage')))
    elif game in ('army', 'gomoku'):
        rows = _board_rows(state, game)
        if rows is not None:
            result['board_rows'] = rows
        if game == 'army':
            bounds = _army_visible_bounds(state)
            if bounds:
                result['visible_bounds'] = bounds
        result.update(_pick(state, ('winning_line',)))
        if 'terrain' not in context:
            result.update(_pick(state, ('terrain',)))
    elif game == 'pokemon':
        result.update(_pick(state, ('actor', 'viewer_seat', 'native_turn', 'native_terminal',
            'opponent_unrevealed_count', 'force_switch', 'trapped',
            'weather', 'fields', 'side_conditions', 'opponent_side_conditions',
            'recent_events')))
        if 'team' in state:
            result['team'] = [_pokemon_mon(p, True, context) for p in state['team']]
        if 'opponent_team' in state:
            result['opponent_team'] = [_pokemon_mon(p, False, context) for p in state['opponent_team']]
        if 'players' in state:
            result['players'] = [{**_pick(p, ('seat', 'unrevealed_count')),
                                  'team': [_pokemon_mon(mon, False, context) for mon in p.get('team', [])]}
                                 for p in state['players']]
    elif game == 'overcooked':
        result.update(_pick(state, ('actor', 'acting_player', 'native_tick', 'players', 'pots',
                                   'objects', 'deliveries', 'delivery_counts', 'last_joint_actions',
                                   'last_transition')))
        result.update(_pick(state, (key for key in ('layout', 'terrain', 'orders', 'max_ticks')
                                   if key not in context)))
    elif game in ('platformer', 'tetris'):
        observation = state.get('observation', {})
        if not isinstance(observation, dict):
            observation = {}
        if game == 'platformer':
            names = ('player', 'coins', 'enemies', 'maxX', 'currentButtons', 'ticksRemaining',
                     'width', 'height', 'level', 'platforms', 'hazards', 'exit', 'physics',
                     'coordinates', 'units', 'geometry', 'tickRate')
            result.update(_pick(observation, (key for key in names if key not in context)))
        else:
            result.update(_pick(observation, ('active', 'next', 'hold', 'holdAvailable', 'pieceId',
                'gravityEveryTicks', 'fallTicks', 'effectiveFallEveryTicks', 'grounded',
                'lockTicks', 'lockLimitTicks', 'lockResets', 'maxLockResets',
                'horizontalDirection', 'horizontalHeldTicks', 'ticksRemaining')))
            if isinstance(result.get('active'), dict):
                result['active'] = _pick(result['active'], ('type', 'rotation', 'x', 'y', 'cells'))
            board = observation.get('board', {})
            if isinstance(board, dict) and isinstance(board.get('rows'), list):
                rows = board['rows']
                coordinates = [row.get('y') for row in rows]
                if coordinates and type(coordinates[0]) is int and coordinates == list(range(coordinates[0], coordinates[0] + len(rows))):
                    result['board_first_y'] = coordinates[0]
                    result['board_rows'] = [_clean(row.get('cells')) for row in rows]
                else:
                    result['board_rows'] = [_pick(row, ('y', 'cells')) for row in rows]
        result.update(_pick(state, ('metrics',)))
    else:
        # Unknown adapters retain their observations as whole values. Oversized
        # events are omitted by the budget, never turned into misleading slices.
        result = _clean(state)
    return result


def _public_result(state, game, context):
    if not isinstance(state, dict) or not isinstance(state.get('public_view'), dict):
        return {'unavailable': True, 'reason': 'no_public_projection'}
    view = state['public_view']
    # Only public_view supplies board data. Top-level scalar outcomes are public;
    # never copy either player's observation/cells or candidate-specific values.
    public = _pick(state, ('step', 'score', 'done', 'reason', 'result', 'winner', 'truncated', 'turn'))
    if game == 'pokemon':
        public.update(_pick(view, ('players', 'recent_events', 'native_turn', 'weather', 'fields')))
    else:
        public.update(_pick(view, ('cells', 'rows', 'cols', 'turn', 'terrain')))
    result = compact_state(public, game, context)
    result['visibility'] = 'public_common_knowledge'
    return result


def _pokemon_public_transition(before, after, context):
    """Keep the public result without repeating privately staged no-op views."""
    result = _public_result(after, 'pokemon', context)
    if result.get('unavailable') or not isinstance(before, dict):
        return result
    old, new = before.get('public_view'), after.get('public_view')
    if not isinstance(old, dict) or not isinstance(new, dict):
        return result
    if old.get('players') == new.get('players'):
        result.pop('players', None)
        result['players_unchanged'] = True
    previous, current = old.get('recent_events', []), new.get('recent_events', [])
    overlap = 0
    if isinstance(previous, list) and isinstance(current, list):
        for count in range(1, min(len(previous), len(current)) + 1):
            if previous[-count:] == current[:count]:
                overlap = count
        result['new_public_events'] = _clean(current[overlap:])
        result.pop('recent_events', None)
    return result


def _winning_cells(state, seat):
    cells, size = state.get('cells'), state.get('cols')
    if not isinstance(cells, list) or type(size) is not int or size < 1 or len(cells) != size * size:
        return []
    wins = []
    for index, item in enumerate(cells):
        if item is not None:
            continue
        row, col = divmod(index, size)
        for dr, dc in ((1, 0), (0, 1), (1, 1), (1, -1)):
            count = 1
            for direction in (-1, 1):
                r, c = row + direction * dr, col + direction * dc
                while 0 <= r < size and 0 <= c < size:
                    piece = cells[r * size + c]
                    if not isinstance(piece, dict) or piece.get('seat') != seat:
                        break
                    count += 1
                    r, c = r + direction * dr, c + direction * dc
            if count >= 5:
                wins.append('place:' + str(index))
                break
    return wins


def _tags(row, game, previous):
    before, after = row.get('before', {}), row.get('after', {})
    tags = []
    if after.get('done'):
        tags.append('terminal')
    if before.get('score') != after.get('score'):
        tags.append('score_changed')
    if before.get('in_check') != after.get('in_check'):
        tags.append('check_changed')
    if before.get('material') != after.get('material'):
        tags.append('material_changed')
    if game == 'overcooked' and before.get('native_tick') != after.get('native_tick'):
        if before.get('pots') != after.get('pots') or before.get('objects') != after.get('objects'):
            tags.append('kitchen_state_changed')
        old_hands = [p.get('held_object') for p in before.get('players', [])]
        new_hands = [p.get('held_object') for p in after.get('players', [])]
        if old_hands != new_hands:
            tags.append('held_object_changed')
    if game == 'gomoku' and row.get('policy_role', 'candidate') == 'candidate':
        seat = before.get('turn')
        if seat in (0, 1):
            mine, theirs = _winning_cells(before, seat), _winning_cells(before, 1 - seat)
            if mine and row.get('action') not in mine:
                tags.append('missed_immediate_win')
            elif not mine and len(theirs) == 1 and row.get('action') != theirs[0]:
                tags.append('missed_unique_block')
    if previous is not None:
        if row.get('action') == previous.get('action'):
            tags.append('repeated_action')
        if game == 'tetris':
            current = before.get('observation', {})
            prior = previous.get('before', {}).get('observation', {})
            directions = {str(row.get('action', '')).split(':')[0], str(previous.get('action', '')).split(':')[0]}
            if current.get('pieceId') == prior.get('pieceId') and directions == {'left', 'right'}:
                tags.append('same_piece_reversal')
        if game == 'platformer':
            player = before.get('observation', {}).get('player', {})
            prior = previous.get('before', {}).get('observation', {}).get('player', {})
            if player and (player.get('x'), player.get('y')) == (prior.get('x'), prior.get('y')):
                tags.append('position_stalled')
    return tags


def _priority(event, first, last):
    index, tags = event['trace_index'], event.get('selection_reasons', [])
    if index == last:
        return 100
    if 'terminal' in tags or 'public_terminal_followup' in tags:
        return 99
    if any(t in tags for t in ('missed_immediate_win', 'missed_unique_block')):
        return 90
    if 'first_recurrence' in tags or 'first_loop' in tags:
        return 85
    if index == first:
        return 80
    if any(t in tags for t in ('score_changed', 'material_changed', 'check_changed')):
        return 70
    if any(t in tags for t in ('kitchen_state_changed', 'held_object_changed')):
        return 68
    if index >= last - 3:
        return 65
    return 10


def _refresh(result, total, eligible, retained, budget):
    omitted = sorted(set(eligible) - set(retained))
    sampling = result['trace_sampling']
    sampling.update(total_steps=total, eligible_steps=len(eligible), retained_indices=sorted(retained),
                    omitted_count=len(omitted), omitted_ranges=_ranges(omitted),
                    all_steps_sampled=len(retained) == total,
                    all_eligible_steps_sampled=len(retained) == len(eligible),
                    privacy_excluded_count=total - len(eligible),
                    empty_event_evidence=bool(eligible) and not retained,
                    serialized_sample_truncated=False, character_budget=budget)
    if eligible and not retained:
        sampling['causal_evidence_limit'] = 'No complete event fits this budget; aggregate scores alone do not establish causes.'
    else:
        sampling.pop('causal_evidence_limit', None)
    public_indices = sorted({followup['trace_index'] for event in result['trace_sample']
                             for followup in event.get('public_followups', [])})
    sampling['public_followup_indices'] = public_indices
    sampling['last_source_step_covered'] = total > 0 and total - 1 in set(retained + public_indices)
    sampling['serialized_chars'] = 0
    for _ in range(4):
        size = chars(result)
        if sampling['serialized_chars'] == size:
            break
        sampling['serialized_chars'] = size


def _fit(result, events, eligible, total, budget):
    result = deepcopy(result)
    result['trace_sample'] = []
    _refresh(result, total, eligible, [], budget)
    if chars(result) > budget:
        raise ValueError('Feedback metadata exceeds the complete-event budget')
    ranked = sorted(events, key=lambda e: (-_priority(e, eligible[0], eligible[-1]), e['trace_index'])) if eligible else []
    for event in ranked:
        proposed = deepcopy(result)
        proposed['trace_sample'] = sorted([*result['trace_sample'], event], key=lambda e: e['trace_index'])
        _refresh(proposed, total, eligible, [e['trace_index'] for e in proposed['trace_sample']], budget)
        if chars(proposed) <= budget:
            result = proposed
    return result


def complete_episode(episode, summary, game_context=None, budget=8000):
    trace = episode.get('trace', [])
    context = game_context or {}
    game = context.get('game_id', episode.get('game_id', 'snake'))
    private = context.get('information_policy', {}).get('feedback_visibility') == 'candidate_before_only'
    eligible = [i for i, row in enumerate(trace) if isinstance(row, dict)
                and (not private or row.get('policy_role') == 'candidate')]
    observation_context = _static_context(trace, game, private)
    result = deepcopy(summary)
    result['feedback_format'] = COMPLETE_FORMAT
    result['feedback_view_note'] = 'Offline coaching projection, not the player input schema; use immutable_runtime_contract for actual fields and action semantics.'
    result['observation_context'] = observation_context
    result['trace_sampling'] = {
        'format': COMPLETE_FORMAT, 'visibility': 'candidate_before_and_public_results' if private else 'full_information',
        'method': 'whole events: terminal, tactical misses, first loop, initial, changes, then remaining',
        'projection_omissions': ['duplicate views/rules', 'receipts and probabilities', 'action labels (IDs retained)',
                                 'display piece IDs/labels; board identity and observable Army rank bounds retained'],
        'omission_semantics': 'inclusive trace-index ranges; retained indices contain complete projected events',
        'privacy_exclusion_scope': 'opponent private views only; public followups are counted separately',
    }
    if game == 'pokemon':
        result['trace_sampling']['projection_omissions'].append(
            'repeated move rule facts use observation_context; public result players_unchanged means same public party state as preceding view; new_public_events removes the exact overlapping history prefix')
    events, previous, seen, first_tags = [], None, set(), set()
    for index in eligible:
        row = trace[index]
        event = {'trace_index': index, 'policy_role': row.get('policy_role', 'candidate'),
                 'actor': row.get('actor', row.get('before', {}).get('turn')),
                 'action': _clean(row.get('action')), 'before': compact_state(row.get('before', {}), game, observation_context)}
        if 'legal_actions' in observation_context:
            event['legal_actions_ref'] = 'observation_context.legal_actions'
        else:
            event['legal_actions'] = _clean([item['id'] for item in row.get('criteria', []) if isinstance(item, dict) and 'id' in item])
        tag_row = {**row, 'after': _public_result(row.get('after'), game, observation_context)} if private else row
        event['selection_reasons'] = _tags(tag_row, game, previous)
        if private:
            event['public_result'] = (_pokemon_public_transition(row.get('before'), row.get('after'), observation_context)
                                      if game == 'pokemon' else _public_result(row.get('after'), game, observation_context))
            followups = []
            for followup_index in range(index + 1, len(trace)):
                followup = trace[followup_index]
                if not isinstance(followup, dict) or followup.get('policy_role') == 'candidate':
                    break
                public_after = (_pokemon_public_transition(followup.get('before'), followup.get('after'), observation_context)
                                if game == 'pokemon' else _public_result(followup.get('after'), game, observation_context))
                if not public_after.get('unavailable'):
                    item = {'trace_index': followup_index, 'policy_role': 'opponent', 'public_result': public_after}
                    if game != 'pokemon':
                        item['action'] = _clean(followup.get('action'))
                    followups.append(item)
                    if public_after.get('done'):
                        event['selection_reasons'].append('public_terminal_followup')
            event['public_followups'] = followups
        else:
            event['after'] = compact_state(row.get('after', {}), game, observation_context)
        position = {k: v for k, v in event['before'].items() if k not in ('step', 'score', 'tick')}
        key = json.dumps(position, sort_keys=True, ensure_ascii=False)
        if key in seen and 'first_recurrence' not in first_tags:
            event['selection_reasons'].append('first_recurrence')
            first_tags.add('first_recurrence')
        seen.add(key)
        for tag in ('same_piece_reversal', 'position_stalled'):
            if tag in event['selection_reasons'] and tag not in first_tags:
                event['selection_reasons'].append('first_loop')
                first_tags.add(tag)
        events.append(event)
        previous = row
    return _fit(result, events, eligible, len(trace), budget)


def fit_pack(evidence, budget=32000, episode_budget=8000, max_episodes=9):
    """Bound the entire serialized feedback list, including omission metadata."""
    chosen = list(evidence[:max_episodes])
    if not chosen:
        return []
    per_episode = min(episode_budget, (budget - 2 - 2 * (len(chosen) - 1)) // len(chosen))
    result, source_indices = [], []
    for source in chosen:
        sampling = source['trace_sampling']
        eligible = list(sampling['retained_indices'])
        for start, end in sampling['omitted_ranges']:
            eligible.extend(range(start, end + 1))
        source_indices.append(sorted(eligible))
        result.append(_fit(source, source['trace_sample'], sorted(eligible), sampling['total_steps'], per_episode))
    # Equal shares are a first pass, not hard partitions: a complete event may
    # be larger than one share while the entire pack still has ample room.
    # Spend that remainder on whole events, terminal events across episodes
    # first. The <=4-episode case keeps its original 8k result unchanged.
    if per_episode < episode_budget:
        pending = []
        for index, source in enumerate(chosen):
            eligible = source_indices[index]
            retained = set(result[index]['trace_sampling']['retained_indices'])
            for event in source['trace_sample']:
                if event['trace_index'] not in retained:
                    pending.append((-_priority(event, eligible[0], eligible[-1]), index, event))
        for _, index, event in sorted(pending, key=lambda item: (item[0], item[1], item[2]['trace_index'])):
            proposed = deepcopy(result[index])
            proposed['trace_sample'] = sorted([*proposed['trace_sample'], event], key=lambda item: item['trace_index'])
            _refresh(proposed, chosen[index]['trace_sampling']['total_steps'], source_indices[index],
                     [item['trace_index'] for item in proposed['trace_sample']], episode_budget)
            if chars(proposed) <= episode_budget and chars(result) - chars(result[index]) + chars(proposed) <= budget:
                result[index] = proposed
    if chars(result) > budget:
        raise ValueError('Complete feedback pack exceeds the declared character budget')
    return result
