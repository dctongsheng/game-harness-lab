"""Adapter-declared evidence requirements before a numeric score is consumed."""


class IncompleteGameResult(ValueError):
    """A budget cutoff is not a native outcome or an infrastructure failure."""


def require_evaluable_result(result, game_context):
    if not game_context.get('evaluation', {}).get('require_native_terminal'):
        return
    final = result.get('final_state', {})
    if (not isinstance(final, dict) or final.get('done') is not True
            or final.get('native_terminal') is not True or final.get('truncated') is not False
            or not isinstance(final.get('result'), str)
            or final.get('result') not in {'win', 'loss', 'draw'}):
        raise IncompleteGameResult('对局未取得原生胜负/和局结果；预算截断不计分，不参与搜索或发布验收')
    expected = game_context.get('score_spec', {}).get(final['result'])
    if (type(expected) not in (int, float) or type(result.get('score')) not in (int, float)
            or type(final.get('score')) not in (int, float)
            or result['score'] != expected or final['score'] != expected):
        raise IncompleteGameResult('原生结果与冻结的胜负评分不一致，不参与搜索或发布验收')
