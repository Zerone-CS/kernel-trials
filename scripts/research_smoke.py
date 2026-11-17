#!/usr/bin/env python3
"""Bounded real slash-command acceptance; never claim a method-quality result."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from engine.budget import Policy
from engine.controller import Controller
from engine.launching import Launcher
from engine.research import Research
from launch import _save
from launch_routes import install_launch_routes
from experiment_runner import _terminate_group, _usage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--project', required=True, help='Fresh acceptance project')
    parser.add_argument('--problem', required=True, help='CPU fixture or already calibrated NPU subtask')
    parser.add_argument('--endpoint', default='http://127.0.0.1:8766')
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--task-id', default='full', help='Optional installed single-shape task to exercise')
    parser.add_argument("--claude-config-dir", default=os.environ.get("CLAUDE_CONFIG_DIR"), help="Override CLAUDE_CONFIG_DIR; otherwise use the CLI default")
    args = parser.parse_args()
    root = Path(args.project).resolve()
    root.mkdir(mode=0o700)
    for name in ('launch.py','assigned.py','experiment.py','experiment_runner.py','client.py'):
        shutil.copyfile(PROJECT/name, root/name)
    shutil.copytree(PROJECT/'engine', root/'engine', ignore=shutil.ignore_patterns('__pycache__'))
    controller = Controller(args.state, database=args.database)
    launcher = Launcher(controller)
    grant = launcher.admin_register(args.problem, owner_id='research-entry-acceptance',
        policy=Policy(max_feedback=2, active_seconds=120), evaluation_purpose='entry_smoke')
    if args.task_id != 'full':
        from engine.problems import load_problem
        task = load_problem(args.problem).manifest['evaluation_task']
        if task['id'] != args.task_id:
            raise ValueError('Acceptance task id differs from its package')
        Research(launcher).admin_add_task(grant['launcher_id'],grant['token'],args.task_id,args.problem,task)
    _save(root/'.eval/launcher.json', {'launcher_id':grant['launcher_id'],'token':grant['token'],'endpoint':args.endpoint})
    install_launch_routes(root)
    _save(root/'experiment.json', {'schema_version':1,'name':'research-entry-smoke','track':'optimize',
        'tasks':[args.task_id],'repeats':args.repeats,'control':'baseline','runner':{'provider':'claude'},
        'policy':{'max_feedback':2,'active_seconds':120},
        'conditions':[{'id':'baseline','label':'Control fixture'},
                      {'id':'method','label':'Method fixture','instructions':'This is a workflow acceptance condition. Follow the administrator-bound smoke procedure.'}]})
    # Only the actual slash invocation can prepare/run the experiment. The
    # acceptance harness does not precreate sessions or call experiment_create.
    evidence = root/'evidence'
    evidence.mkdir(mode=0o700)
    environment = dict(os.environ)
    if args.claude_config_dir:
        environment['CLAUDE_CONFIG_DIR'] = args.claude_config_dir
    for key in ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT'):
        environment.pop(key, None)
    command = ['claude','-p','/op-eval compare experiment.json','--output-format','stream-json','--verbose']
    started = time.monotonic()
    timed_out = False
    with (evidence/'stdout.log').open('xb') as out, (evidence/'stderr.log').open('xb') as err:
        (evidence/'stdout.log').chmod(0o600)
        (evidence/'stderr.log').chmod(0o600)
        process = subprocess.Popen(command,cwd=root,env=environment,stdin=subprocess.DEVNULL,
                                   stdout=out,stderr=err,start_new_session=True)
        try:
            process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            _terminate_group(process)
    result = {'purpose':'entry_smoke','native_prompt':'/op-eval compare experiment.json',
              'claude_config_dir':environment.get('CLAUDE_CONFIG_DIR'),'returncode':process.returncode,
              'timed_out':timed_out,'elapsed_seconds':time.monotonic()-started,
              'launcher_id':grant['launcher_id'],'project':str(root),
              'outer_provider_metadata':_usage(evidence/'stdout.log','claude')}
    records = [json.loads(p.read_text()) for p in (root/'.eval').glob('experiment-*.json')]
    records = [r for r in records if 'experiment_id' in r]
    result['experiments_created'] = len(records)
    if len(records) == 1:
        record = records[0]
        experiment = Research(launcher).report(grant['launcher_id'],grant['token'],experiment_id=record['experiment_id'])
        directory = Path(record['directory'])
        result.update(experiment_id=record['experiment_id'],report_path=str(directory/'REPORT.md'),
            trials=len(experiment['trials']),states=[t['status'] for t in experiment['trials']],
            outcomes=[t.get('report',{}).get('result',{}).get('outcome') for t in experiment['trials']],
            sessions=[t.get('report',{}).get('session',{}).get('session_id') for t in experiment['trials']],
            feedback_used=[t.get('report',{}).get('budget',{}).get('feedback_used') for t in experiment['trials']])
        result['unchanged_starters'] = [all(e.get('is_unchanged_starter') is True for e in
            t.get('report',{}).get('diagnostics',{}).get('trace',[])) for t in experiment['trials']]
        result['accepted'] = (not timed_out and process.returncode == 0 and result['trials'] == 2*args.repeats
            and len(set(result['sessions'])) == result['trials'] and set(result['states']) == {'FINISHED'}
            and set(result['outcomes']) == {'passed'} and all(result['unchanged_starters'])
            and (directory/'comparison.json').is_file())
        for name in ('REPORT.md','comparison.json','trials.json','manifest.json'):
            if (directory/name).is_file():
                shutil.copyfile(directory/name,evidence/name)
    else:
        result['accepted'] = False
    _save(evidence/'summary.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['accepted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
