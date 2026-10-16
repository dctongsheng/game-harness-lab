"""Immutable approved Harness packages. No credentials or raw provider traces."""
from __future__ import annotations
import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .providers import validate_candidate
from .store import atomic_json, utcnow


def _safe_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}',value):
        raise ValueError('Invalid package identifier')
    return value


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(root, path):
    path.resolve().relative_to(root.resolve())
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError('Symlinks are not allowed in exported packages')
        current = current.parent


def _clean(value):
    """Portable public metadata only; remove nested transport credentials/paths."""
    if isinstance(value, dict):
        banned = ('api_key','authorization','password','secret','token','base_url','decision_url','coach_url','endpoint','local_path','run_dir','credential','raw_response','raw_request','trace')
        return {k:_clean(v) for k,v in value.items() if isinstance(k,str)
                and not any(word in k.lower().replace('-','_') for word in banned)}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, str):
        if re.search(r'\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{30,})\b', value):
            return '[credential removed]'
        if re.search(r'(?:/Users/|/home/|/tmp/|[A-Z]:\\)',value):
            return '[local path removed]'
    return value


def read_bundle(root, game_id, version_id):
    path = root / _safe_id(game_id) / _safe_id(version_id)
    _inside(root,path)
    filenames = {'manifest.json','game.json','strategy.json','evaluation.json','README.md'}
    if {p.name for p in path.iterdir()} != filenames or any(p.is_symlink() or not p.is_file() for p in path.iterdir()):
        raise ValueError('Package must contain exactly five regular files')
    manifest = json.loads((path / 'manifest.json').read_text())
    if manifest.get('game_id') != game_id or manifest.get('version_id') != version_id:
        raise ValueError('Package identity mismatch')
    expected = {'game.json','strategy.json','evaluation.json','README.md'}
    if set(manifest.get('files',{})) != expected:
        raise ValueError('Package file manifest mismatch')
    for filename, checksum in manifest['files'].items():
        if _hash(path / filename) != checksum:
            raise ValueError('Package integrity mismatch')
    return {'manifest':manifest,'game':json.loads((path/'game.json').read_text()),
            'strategy':json.loads((path/'strategy.json').read_text()),
            'evaluation':json.loads((path/'evaluation.json').read_text())}


def export_approved(root: Path, version: dict, job: dict, game_context: dict, brief: dict):
    if job.get('status') != 'promoted' or job.get('candidate_id') != version['id'] or job.get('scope') != version['scope']:
        raise ValueError('Only a matching approved evaluation can be exported')
    pairs=job.get('pairs',[])
    for pair in pairs:
        for key in ('candidate','baseline'):
            value = pair.get(key)
            if type(value) not in (int,float) or not math.isfinite(value):
                raise ValueError('Evaluation scores must be finite numbers')
    threshold=job.get('gate',{}).get('minimum_mean_gain',.5)
    if type(threshold) not in (int,float) or not math.isfinite(threshold) or threshold <= 0:
        raise ValueError('Evaluation threshold must be finite and positive')
    minimum_pairs = job.get('gate',{}).get('minimum_pairs',4)
    if type(minimum_pairs) is not int or minimum_pairs < 4:
        raise ValueError('Export gate requires at least four pairs')
    if len(pairs)<minimum_pairs or any(p['candidate']<p['baseline'] for p in pairs):
        raise ValueError('Export requires at least four successful non-regressing evaluation pairs')
    mean_gain=sum(p['candidate']-p['baseline'] for p in pairs)/len(pairs)
    if mean_gain < threshold:
        raise ValueError('Export evidence does not meet the recorded gate')
    candidate=validate_candidate(version['candidate'])
    game_id=_safe_id(game_context['game_id']);version_id=_safe_id(version['id'])
    parent=root/game_id;target=parent/version_id
    _inside(root,target)
    if target.exists():
        bundle=read_bundle(root,game_id,version_id)
        if bundle['strategy'] != candidate or bundle['manifest']['scope'] != version['scope']:
            raise ValueError('Existing immutable package differs from approved version')
        return bundle['manifest']
    parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='.export-',dir=parent))
    try:
        context_keys = ('game_id','name','rules_version','rules','score_spec','config','opponent_policy',
                        'scenario_policy','action_policy','evaluation','trial_design','opponent','limitations')
        game=_clean({'context':{k:game_context[k] for k in context_keys if k in game_context},
                     'brief':{k:brief[k] for k in ('background','goal','rules','precautions','strategy') if k in brief}})
        keys=('id','status','scope','parent_id','candidate_id','created_at','finished_at','gate',
              'baseline_score','candidate_score','mean_gain','pairs','decision','config')
        evaluation=_clean({key:job[key] for key in keys if key in job})
        gepa=job.get('gepa',{})
        evaluation['search']={key:gepa[key] for key in ('engine','version','best_idx','best_score','val_aggregate_scores','candidate_count','total_metric_calls') if key in gepa}
        evaluation['evidence_type']='empirical paired evaluation; not a generalization or significance claim'
        evaluation['raw_traces_included']=False
        atomic_json(temporary/'game.json',game)
        if _clean(candidate) != candidate:
            raise ValueError('Strategy contains credentials or local paths and cannot be exported')
        atomic_json(temporary/'strategy.json',candidate)
        atomic_json(temporary/'evaluation.json',evaluation)
        readme=(f"# {game_context['name']} Harness · {version_id}\n\n"
                f"此包已通过记录在 evaluation.json 的配对门槛；不是长期泛化证明。\n\n"
                f"- 游戏：`{game_id}`；规则：`{game_context['rules_version']}`。\n"
                f"- 原版本均分：{job.get('baseline_score')}；候选均分：{job.get('candidate_score')}；配对数：{len(pairs)}。\n"
                f"- `game.json`：固定规则、评分、配置、对手策略和初始化资料。\n"
                f"- `strategy.json`：可供 Decision 使用的两段策略文字。\n"
                f"- `evaluation.json`：训练选优摘要与独立于本次搜索的配对验收。\n"
                f"- `manifest.json`：版本血缘、模型身份与文件 SHA-256。\n\n"
                f"本包需由兼容游戏适配器运行，不包含模型权重或原始 API 轨迹。\n")
        (temporary/'README.md').write_text(readme,encoding='utf-8')
        manifest={'schema_version':1,'game_id':game_id,'version_id':version_id,'parent_id':version.get('parent_id'),
                  'scope':version['scope'],'rules_version':game_context['rules_version'],
                  'decision_model':version['decision_model'],'think_mode':'never',
                  'identity_verification':version.get('identity_verification','service_reported_name'),
                  'approval_job_id':job['id'],'created_at':utcnow(),
                  'score_summary':{'baseline':job.get('baseline_score'),'candidate':job.get('candidate_score'),'pairs':len(pairs)},
                  'files':{name:_hash(temporary/name) for name in ('game.json','strategy.json','evaluation.json','README.md')}}
        atomic_json(temporary/'manifest.json',manifest)
        temporary.rename(target)
        return manifest
    finally:
        if temporary.exists():shutil.rmtree(temporary)


def sync_bundle(repo: Path, bundle_path: Path):
    """Commit only this approved package; never stage other user work or credentials."""
    relative=bundle_path.resolve().relative_to(repo.resolve())
    if len(relative.parts)!=3 or relative.parts[0]!='harnesses':
        raise ValueError('Git sync is restricted to one harnesses/game/version package')
    for part in relative.parts[1:]:_safe_id(part)
    read_bundle(repo / 'harnesses',relative.parts[1],relative.parts[2])
    def git(*args,ok=(0,)):
        result=subprocess.run(['git',*args],cwd=repo,capture_output=True,text=True,timeout=90)
        if result.returncode not in ok:
            raise RuntimeError(f'Git {args[0]} failed (exit {result.returncode}); package remains local')
        return result
    if Path(git('rev-parse','--show-toplevel').stdout.strip()).resolve()!=repo.resolve():
        raise ValueError('Git root does not match the project')
    git('symbolic-ref','--short','HEAD')
    git('add','--',str(relative))
    changed=git('diff','--cached','--quiet','--',str(relative),ok=(0,1)).returncode==1
    if changed:
        git('commit','--only','-m',f'Publish evaluated {relative.parts[1]} Harness {relative.parts[2]}','--',str(relative))
    git('push','origin','HEAD')
    return {'status':'synced','commit':git('rev-parse','HEAD').stdout.strip()}
