"""Two collaboration topologies on the native Kanban/Swarm kernel.

Tasks, dependencies, claims, processes and evidence stay in native Kanban.
Only WebUI preferences (session mode) are stored separately as small JSON files.
"""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_swarm as swarm
from .native_compat import install
from .native_clock import timing

install()
RESEARCH_MODES = {'kanban', 'swarm'}
MODES = {'single', *RESEARCH_MODES}

# Identical publication instructions, not a phase scheduler or new task state.
STAGE_PUBLICATION_POLICY = """Publication checkpoint rule:
When an independent subproblem, lemma, boundary result or experiment improvement
within your assigned acceptance scope has reproducible evidence and has passed
your own checks, publish it to the common root BEFORE continuing other exploration.
Do not accumulate checked partial results until the entire problem is solved.
Use native blackboard stage comments with a unique ID and include scope, claim,
evidence, reproduction and limitations. Attach public evidence files first when
needed, then cite the task/attachment in the root comment; attaching alone or
commenting only on your own card is not a public stage submission.
Choose your own research order. Do not wait for peers to finish a phase, preassign
research directions, invent success from guesses/heartbeats, or manufacture a
fixed number of submissions. A genuinely joint result may be one stage with an
explicit combined scope. Keep unfinished reasoning and failures private.
Publishing is not completing your task. Partial scope does not satisfy the full
acceptance criteria. max_turns limits goal-loop turns, not subproblem/stage count.
Continue independent research and peer review; complete only when the assigned
acceptance scope is satisfied, or report genuine blockage without claiming success.
Publication and peer support are never automatic formal verification.
"""


class ResearchSwarm:
    def __init__(self, home, models, *, capacity=8, capabilities=None):
        self.home = Path(home).resolve()
        self.models = {m.key: m for m in models}
        if type(capacity) is not int or capacity < 1:
            raise ValueError('positive Kanban worker capacity required')
        self.capacity = capacity
        self.capabilities = capabilities or {}
        self.skills = Path(__file__).parent / 'skills'
        self._settings_lock = threading.RLock()

    def board(self, owner):
        if not owner:
            raise PermissionError('authenticated owner required')
        return 'research-' + hashlib.sha256(owner.encode()).hexdigest()[:24]

    @contextmanager
    def connect(self, owner):
        board = self.board(owner)
        if not kb.board_exists(board):
            kb.create_board(board, name='复杂研究')
        with kb.scoped_current_board(board):
            conn = kb.connect(board=board)
            try:
                yield conn, board
            finally:
                conn.close()

    @staticmethod
    def topology_mode(topology):
        return 'kanban' if topology.get('mode') == 'kanban' else 'swarm'

    @staticmethod
    def session_lineage(session_id):
        if not session_id:return []
        from hermes_agent import _Db
        db=_Db.get()
        tip=db.get_compression_tip(session_id) or session_id
        ids=[]
        while tip and tip not in ids:
            ids.append(tip)
            tip=(db.get_session(tip) or {}).get('parent_session_id')
        return ids

    def _settings_path(self, owner, session_id):
        if not session_id or len(session_id) > 256:
            raise ValueError('session_id required')
        return self.home / 'ui-settings' / self.board(owner) / (hashlib.sha256(session_id.encode()).hexdigest() + '.json')

    def mode_config(self, owner, session_id):
        if not session_id:return {'mode':'single','locked':False}
        lineage=self.session_lineage(session_id)
        roots=[r for r in self.roots(owner) if r['session_id'] in lineage]
        if roots:
            # Execution history is authoritative, including legacy sessions.
            return {'mode':min(roots,key=lambda r:r['created_at'])['mode'],'locked':True}
        with self._settings_lock:
            for sid in lineage:
                path=self._settings_path(owner,sid)
                if path.exists():
                    cfg=json.loads(path.read_text())
                    if cfg.get('mode') in MODES:return {'mode':cfg['mode'],'locked':bool(cfg.get('locked'))}
        # Pre-selector conversations were research-coordinator chats. Preserve
        # their role instead of silently turning an old research session into single.
        from hermes_agent import _Db
        legacy = any(_Db.get().get_session(sid) for sid in lineage)
        legacy_mode=os.environ.get('COMPLEX_LEGACY_MODE','kanban')
        if legacy_mode not in {'single','kanban'}:raise ValueError('COMPLEX_LEGACY_MODE must be single or kanban')
        # An upgraded direct-only store must not turn old chats into coordinators.
        return {'mode':legacy_mode if legacy else 'single','locked':bool(legacy and legacy_mode=='single')}

    def session_mode(self, owner, session_id):
        return self.mode_config(owner,session_id)['mode']

    def set_session_mode(self, owner, session_id, mode, *, lock=False):
        if mode not in MODES:raise ValueError('mode must be single, kanban or swarm')
        session_id=(self.session_lineage(session_id) or [''])[0]
        path=self._settings_path(owner,session_id)
        with self._settings_lock:
            cfg=self.mode_config(owner,session_id)
            if cfg['locked'] and cfg['mode']!=mode:
                raise PermissionError('该会话已经开始执行，方式已固定；请新建会话选择另一种方式。')
            path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            tmp=path.with_suffix('.tmp')
            tmp.write_text(json.dumps({'session_id':session_id,'mode':mode,'locked':lock or cfg['locked']}))
            tmp.chmod(0o600);tmp.replace(path)
        return mode

    def dispatch_options(self):
        """Pass only explicit native Kanban configuration, otherwise use its defaults."""
        from hermes_cli.config import load_config
        cfg=(load_config() or {}).get('kanban') or {}
        options={}
        for key,arg,minimum in [('failure_limit','failure_limit',1),('dispatch_stale_timeout_seconds','stale_timeout_seconds',0)]:
            if key not in cfg:continue
            try:value=int(cfg[key])
            except (TypeError,ValueError):continue
            if value>=minimum:options[arg]=value
        return options

    def config(self):
        from .budgets import run_ceiling
        from .request_timeouts import settings
        return {'run_time_ceiling_seconds':run_ceiling(),
                'swarm_time_plan':'Choose initial run duration and extension ceiling from assessed difficulty, expected research/verification work and model latency; explain the estimate. No fixed short default.',
                'extensions':'Worker requests in native root comments; coordinator decides within the announced ceiling, only for live unexpired runs. Requests and heartbeat do not extend time.',
                'models': [{**m.as_json(), 'worker_toolsets': self.capabilities.get(m.key, {}).get('toolsets', ['file', 'terminal']),
                            'worker_mcp': list(self.capabilities.get(m.key, {}).get('mcp_servers', {})),
                            'request_timeouts':settings(self.capabilities.get(m.key,{}).get('timeouts'))}
                           for m in self.models.values()], 'max_in_progress': self.capacity,
                'modes': ['single', 'kanban', 'swarm'], 'default_mode': 'single', 'automatic_dispatch': True,
                'single_execution': 'direct Hermes conversation; no Kanban task or Worker',
                'native_failure_limit':self.dispatch_options().get('failure_limit',kb.DEFAULT_FAILURE_LIMIT),
                'goal_loop':'native single-shot Kanban tasks; goal loop enabled only for continuous Swarm',
                'storage': 'native Kanban tasks/runs/comments/attachments',
                'budgets': 'wall_seconds bounds each native run; max_iterations is per Hermes turn; max_turns bounds the native goal loop',
                'verification': 'peer reports are evidence, never automatic formal verification'}

    def _profile(self, name, model_key, budget):
        from hermes_cli.profiles import create_profile, get_profile_dir
        import yaml
        profile = get_profile_dir(name)
        if not profile.exists():
            profile = create_profile(name, no_alias=True, no_skills=True)
        model = self.models[model_key]
        from hermes_cli.config import load_config
        common_agent=(load_config() or {}).get('agent') or {}
        native_agent={}
        if common_agent.get('reasoning_effort') is not None:
            native_agent['reasoning_effort']=common_agent['reasoning_effort']
        native_agent.update(self.capabilities.get(model_key,{}).get('agent',{}))
        native_agent['max_turns']=budget['max_iterations']
        from .request_timeouts import settings
        request_timeouts=settings(self.capabilities.get(model_key,{}).get('timeouts'))
        key_name = self.capabilities.get(model_key, {}).get('api_key_env', 'ARK_API_KEY')
        cfg = {'model': {'provider': model.provider, 'model': model.model,
                         'base_url': model.base_url, 'api_key': '${' + key_name + '}',
                         'context_length': model.context or 1048576},
               'toolsets': ['kanban'], 'platform_toolsets': {'cli': list(dict.fromkeys(
                   [*self.capabilities.get(model_key, {}).get('toolsets', ['file', 'terminal']), 'skills', 'kanban']))},
               'agent': native_agent,
               'terminal': {'timeout':request_timeouts['tool_timeout_seconds']},
               'providers': {model.provider: {'models': {model.model: {
                   'timeout_seconds':request_timeouts['api_timeout_seconds'],
                   'stale_timeout_seconds':request_timeouts['api_stale_timeout_seconds']}}}},
               'skills': {'external_dirs': [str(self.skills)], 'creation_nudge_interval': 0},
               'kanban': {'dispatch_in_gateway': False, 'notify_in_gateway': False},
               'mcp_servers': self.capabilities.get(model_key, {}).get('mcp_servers', {})}
        (profile / 'config.yaml').write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
        (profile / 'SOUL.md').write_text('You execute the assigned Kanban task. Follow its skill, publish deliverables and keep unpublished notes private.\n')
        return profile

    def create(self, owner, *, question, models, budget, baseline='', session_id='', mode='swarm', tasks=None, time_plan=None):
        if mode not in RESEARCH_MODES:
            raise ValueError('Single execution is a direct Hermes conversation, not a Kanban task')
        if session_id:
            cfg=self.mode_config(owner,session_id)
            if cfg['locked'] and cfg['mode']!=mode:raise PermissionError('该会话的执行方式已固定，请新建会话。')
        if not isinstance(question, str) or not question.strip() or len(question.encode()) > 50000:
            raise ValueError('a bounded research question is required')
        minimum = 2 if mode == 'swarm' else 1
        if not isinstance(models, list) or not minimum <= len(models) <= 32 or any(m not in self.models for m in models):
            raise ValueError(f'models must name {minimum}..32 configured worker endpoints')
        if not isinstance(budget, dict) or set(budget) != {'wall_seconds', 'max_iterations', 'max_turns'}:
            raise ValueError('budget requires wall_seconds, max_iterations, max_turns')
        for key, low, high in [('wall_seconds', 30, 86400), ('max_iterations', 1, 1000), ('max_turns', 1, 100)]:
            if type(budget[key]) is not int or not low <= budget[key] <= high:
                raise ValueError('invalid ' + key)
        from .budgets import time_plan as validate_time_plan, run_ceiling
        if budget['wall_seconds'] > run_ceiling():raise ValueError('wall_seconds exceeds configured run ceiling')
        time_plan=validate_time_plan(time_plan,budget['wall_seconds'],required=mode=='swarm')
        if mode == 'swarm':
            if tasks:
                raise ValueError('Swarm workers receive identical tasks; do not preassign directions')
            specs = [{'title': f'Independent researcher {i+1}', 'body': question, 'depends_on': []} for i in range(len(models))]
        else:
            if not isinstance(tasks, list) or len(tasks) != len(models):
                raise ValueError('Kanban requires one task specification per model')
            specs = []
            for i, task in enumerate(tasks):
                if not isinstance(task, dict) or any(not isinstance(task.get(k), str) or not task[k].strip() for k in ['title', 'body']):
                    raise ValueError('each Kanban task requires title and body')
                parents = task.get('depends_on', [])
                if not isinstance(parents, list) or any(type(p) is not int or not 0 <= p < i for p in parents):
                    raise ValueError('depends_on uses zero-based indices of earlier tasks')
                specs.append({**task, 'depends_on': list(dict.fromkeys(parents))})
        source = None
        if baseline:
            allowed = Path(os.environ.get('COMPLEX_BASELINES_DIR', self.home / 'baselines')).resolve()
            source = Path(baseline).resolve()
            source.relative_to(allowed)
            if not source.is_dir() or any(p.is_symlink() for p in source.rglob('*')):
                raise ValueError('baseline must be an approved directory without symlinks')
        with self.connect(owner) as (conn, board):
            # Construction triage is temporary, not a user approval gate.
            root = kb.create_task(conn, title=question.splitlines()[0][:120], body=question,
                                  created_by='research-coordinator', assignee='default',
                                  triage=True, session_id=session_id or None, board=board)
            workroot = kb.board_dir(board) / 'research-workspaces' / root
            created = []
            try:
                if source:
                    snapshot = workroot / 'initial-baseline'
                    shutil.copytree(source, snapshot, ignore=shutil.ignore_patterns('.git', '__pycache__'))
                skill = 'research-worker' if mode == 'swarm' else 'kanban-worker'
                for i, (model, spec) in enumerate(zip(models, specs), 1):
                    identity = f'r-{root}-{i}'
                    self._profile(identity, model, budget)
                    workspace = workroot / str(i)
                    if source:
                        shutil.copytree(snapshot, workspace)
                    else:
                        workspace.mkdir(parents=True)
                    body = (f'Common research question:\n{question}\n\nPublic swarm root: {root}\n'
                            f'Initial baseline: {baseline or "No supplied code/data baseline"}\n')
                    if mode == 'swarm':
                        body += ('Research independently. Publish stages to the root blackboard when useful, read and verify public stages, '
                                 'then continue your own research. Publishing is not completion. Use research-worker. '
                                 'Finish only with a justified task outcome or report that the budget is exhausted; '
                                 'never claim peer support is formal verification.')
                    else:
                        # Default Kanban cards carry their assigned scope, not
                        # repeated copies of the entire project/research goal.
                        body=(f'Assigned Kanban task:\n{spec["body"]}\n\nPublic Kanban root: {root}\n'
                              f'Initial baseline: {baseline or "No supplied baseline"}\n'
                              'Use kanban-worker. Complete only this card acceptance scope; the common root is project background, '
                              'not a demand to solve downstream tasks. Read completed parent deliverables and publish this task result. '
                              'Resume existing workspace work on retry rather than rebuilding from scratch.')
                    body += '\n\n' + STAGE_PUBLICATION_POLICY
                    if time_plan:
                        body += ('\nInitial run time: '+str(budget['wall_seconds'])+' seconds. Announced per-run extension ceiling: '
                                 +str(time_plan['max_run_seconds'])+' seconds. Time assessment: '+time_plan['assessment']
                                 +'\nIf useful work needs more time, request BEFORE expiry with research_request_extension(extra_seconds, reason, plan), '
                                 +'which binds your native task/current run/root and posts the native comment. Alternatively use native blackboard '
                                 +'key extension:<unique> and value {task_id, run_id, extra_seconds, reason, plan}. '
                                 +'Use your own actual current_run_id. Await extension-decision:<request comment ID>; '
                                 +'heartbeat/request does not extend time. Continue useful work without busy polling, '
                                 +'never edit the DB or restart a stopped run to bypass limits.')
                    task = kb.create_task(conn, title=spec['title'], body=body,
                                          assignee=identity, created_by='research-coordinator',
                                          parents=[root, *[created[p]['task_id'] for p in spec['depends_on']]],
                                          workspace_kind='dir', workspace_path=str(workspace), skills=[skill], goal_mode=mode=='swarm',
                                          goal_max_turns=budget['max_turns'] if mode=='swarm' else None,
                                          max_runtime_seconds=budget['wall_seconds'], session_id=session_id or None, board=board)
                    created.append({'task_id': task, 'model': model, 'identity': identity,
                                    'model_name': self.models[model].model, 'model_label': self.models[model].label})
                swarm.post_blackboard_update(conn, root, author='research-coordinator', key='topology',
                    value={'mode': 'continuous_research' if mode == 'swarm' else 'kanban', 'root_id': root,
                           'workers': created, 'budget': budget, 'baseline': baseline, 'state': 'running', 'session_id': session_id,
                           **({'time_plan':time_plan} if time_plan else {})})
                if session_id:
                    kb.add_notify_sub(conn, task_id=root, platform='complex-webui', chat_id=session_id,
                                      user_id=owner, notifier_profile='default')
                    for item in created:
                        kb.add_notify_sub(conn,task_id=item['task_id'],platform='complex-webui',chat_id=session_id,
                                          thread_id=root,user_id=owner,notifier_profile='default')
                # Keep the planning card non-dispatchable until native completion.
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET status='blocked' WHERE id=? AND status='triage'", (root,))
                    kb._append_event(conn, root, 'blocked', {'reason': 'Planning card; not an execution task'})
                if not kb.complete_task(conn, root, summary='Common task and public blackboard; no research performed by the coordinator.'):
                    raise ValueError('planning card could not be finalized')
                if session_id:self.set_session_mode(owner,session_id,mode,lock=True)
            except Exception:
                for item in created:
                    kb.archive_task(conn, item['task_id'])
                kb.archive_task(conn, root)
                raise
        return self.detail(owner, root)

    def release_legacy_drafts(self, conn):
        """Remove only our retired UI-confirmation blocks, never worker blocks."""
        released = 0
        for task in kb.list_tasks(conn, limit=200, include_archived=False):
            topology = swarm.latest_blackboard(conn, task.id).get('topology')
            if not isinstance(topology, dict) or topology.get('state') != 'draft':
                continue
            for item in topology.get('workers', []):
                worker = kb.get_task(conn, item['task_id'])
                if worker is None or worker.status != 'blocked' or kb.latest_run(conn, worker.id) is not None:
                    continue
                blocks = [e for e in kb.list_events(conn, worker.id) if e.kind in {'blocked', 'unblocked'}]
                if blocks and blocks[-1].kind == 'blocked' and (blocks[-1].payload or {}).get('reason') == 'Awaiting explicit user confirmation':
                    released += bool(kb.unblock_task(conn, worker.id))
            swarm.post_blackboard_update(conn, task.id, author='research-coordinator', key='topology', value={**topology, 'state': 'running'})
        return released

    def roots(self, owner):
        with self.connect(owner) as (conn, _):
            result = []
            for task in kb.list_tasks(conn, limit=200, include_archived=False):
                topology = swarm.latest_blackboard(conn, task.id).get('topology')
                if isinstance(topology, dict) and topology.get('mode') in {'continuous_research', 'kanban'}:
                    topology={**topology,'workers':self.members(conn,task.id,topology)}
                    result.append({'id': task.id, 'title': task.title, 'state': self._state(conn, topology),
                                   'worker_count':len(topology['workers']),
                                   'active_count':sum(kb.get_task(conn,w['task_id']).status in {'running','ready'} for w in topology['workers']),
                                   'mode': self.topology_mode(topology), 'session_id': (self.session_lineage(topology.get('session_id') or task.session_id or '') or [''])[0],
                                   'created_at': task.created_at})
            return sorted(result, key=lambda r: r['created_at'], reverse=True)

    def endpoint_for(self, profile, fallback=''):
        """Native assignee/profile is authoritative; old run profiles stay immutable."""
        from hermes_cli.profiles import get_profile_dir
        import yaml
        path=get_profile_dir(profile)/'config.yaml'
        try:
            model=(yaml.safe_load(path.read_text()) or {}).get('model') or {}
            name=model.get('model') if isinstance(model,dict) else model
            return next((m.key for m in self.models.values() if m.model==name),fallback)
        except (OSError,ValueError,yaml.YAMLError):return fallback

    @staticmethod
    def profile_request_timeouts(profile):
        from hermes_cli.profiles import get_profile_dir
        import yaml
        try:
            cfg=yaml.safe_load((get_profile_dir(profile)/'config.yaml').read_text()) or {}
            model=cfg.get('model') or {}
            values=(cfg.get('providers',{}).get(model.get('provider'),{}).get('models',{}).get(model.get('model'),{}))
            tool=cfg.get('terminal',{}).get('timeout')
            if tool is None or 'timeout_seconds' not in values or 'stale_timeout_seconds' not in values:return None
            return {'tool_timeout_seconds':tool,'api_timeout_seconds':values['timeout_seconds'],'api_stale_timeout_seconds':values['stale_timeout_seconds']}
        except (OSError,ValueError,AttributeError,yaml.YAMLError):return None

    def members(self, conn, root, topology):
        seeds={w['task_id']:w for w in topology.get('workers',[])}
        ids=list(dict.fromkeys([*seeds,*kb.child_ids(conn,root)]))
        members=[]
        for tid in ids:
            task=kb.get_task(conn,tid)
            if task is None:continue
            seed=seeds.get(tid,{})
            key=self.endpoint_for(task.assignee,seed.get('model',''))
            model=self.models.get(key)
            members.append({**seed,'task_id':tid,'identity':task.assignee,'model':key,
                            'model_name':model.model if model else key,'model_label':model.label if model else key})
        return members

    def authors(self, conn, topology):
        authors={}
        for i,w in enumerate(topology['workers'],1):
            profiles={w['identity'],*[r.profile for r in kb.list_runs(conn,w['task_id']) if r.profile]}
            for profile in profiles:
                key=self.endpoint_for(profile,w['model']);model=self.models.get(key)
                authors[profile]={'author':profile,'agent':i,'task_id':w['task_id'],'model':key,'model_name':model.model if model else key,
                                  'author_label':f'Agent {i} · {model.label if model else key}'}
        return authors

    def reassign(self, owner, task_id, model_key):
        """Explicit user recovery, using native assign/unblock on the SAME card."""
        if model_key not in self.models:raise ValueError('unknown configured model')
        with self.connect(owner) as (conn,_):
            task=kb.get_task(conn,task_id)
            if task is None:raise LookupError('task not found')
            if task.status not in {'blocked','ready','todo'}:raise ValueError('only stopped unfinished tasks can be reassigned')
            root=None;topology=None
            for pid in kb.parent_ids(conn,task_id):
                try:_,candidate=self._topology(conn,pid)
                except LookupError:continue
                root=pid;topology=candidate;break
            if root is None:raise PermissionError('not an execution task in this research board')
            profile=f'r-{task_id}-{time.time_ns():x}'
            self._profile(profile,model_key,topology['budget'])
            # Each native operation owns its transaction; do not wrap it in
            # another BEGIN IMMEDIATE or replace its claim/recovery rules.
            kb.assign_task(conn,task_id,profile)
            if task.status=='blocked':kb.unblock_task(conn,task_id)
        return self.detail(owner,root)

    def _topology(self, conn, root):
        task = kb.get_task(conn, root)
        topology = swarm.latest_blackboard(conn, root).get('topology')
        if task is None or not isinstance(topology, dict) or topology.get('mode') not in {'continuous_research', 'kanban'}:
            raise LookupError('research task group not found')
        return task, {**topology,'workers':self.members(conn,root,topology)}

    def _state(self, conn, topology):
        if topology['state'] == 'cancelled':
            return 'cancelled'
        states = [kb.get_task(conn, w['task_id']).status for w in topology['workers']]
        if all(s == 'done' for s in states):
            return 'finished'
        if 'running' in states:
            return 'running'
        if 'ready' in states:
            return 'ready'
        if 'blocked' in states:
            return 'blocked'
        if 'todo' in states:
            return 'todo'
        if 'review' in states:
            return 'review'
        return 'blocked'

    def detail(self, owner, root):
        with self.connect(owner) as (conn, _):
            task, topology = self._topology(conn, root)
            mode = self.topology_mode(topology)
            workers = []
            from .budgets import requests, announced_plan
            extensions=requests(conn,root,topology,self.authors(conn,topology))
            for item in topology['workers']:
                worker = kb.get_task(conn, item['task_id'])
                run = kb.latest_run(conn, worker.id)
                notes = [e for e in kb.list_events(conn, worker.id) if e.kind == 'heartbeat' and e.payload and e.payload.get('note')]
                heartbeat = worker.last_heartbeat_at or (run.last_heartbeat_at if run else None)
                from .native_activity import describe, failure_reason, snapshot
                activity,activity_source,activity_at=describe(worker,run,notes)
                operation=snapshot(run)
                dependencies=[]
                for pid in kb.parent_ids(conn,worker.id):
                    parent=kb.get_task(conn,pid)
                    if parent and parent.status not in {'done','archived'}:
                        dependencies.append({'id':parent.id,'title':parent.title,'status':parent.status})
                workers.append({'id': worker.id, 'title': worker.title, 'model': item['model'], 'status': worker.status,
                                'parents': kb.parent_ids(conn, worker.id), 'pid': worker.worker_pid,
                                'started_at': worker.started_at, 'last_heartbeat': heartbeat, 'alive': kb._pid_alive(worker.worker_pid),
                                'heartbeat_age': max(0, int(time.time()) - heartbeat) if heartbeat else None,
                                'activity':activity,'activity_source':activity_source,'activity_at':activity_at,
                                'recent_actions':operation['recent_tools'] if operation else [],
                                'stats':operation['stats'] if operation else None,
                                'timing':timing(run),
                                'attachments':[{'id':a.id,'filename':a.filename,'size':a.size,
                                                'url':f'/api/kanban/attachment?id={a.id}&board={self.board(owner)}'} for a in kb.list_attachments(conn,worker.id)],
                                'status_reason':failure_reason(worker,run),'dependencies':dependencies,
                                'consecutive_failures':worker.consecutive_failures,
                                'failure_limit':worker.max_retries if worker.max_retries is not None else self.dispatch_options().get('failure_limit',kb.DEFAULT_FAILURE_LIMIT),
                                'goal_mode':worker.goal_mode,'run':asdict(run) if run else None,
                                'extensions':[r for r in extensions if r['task_id']==worker.id][-5:],
                                'request_timeouts':self.profile_request_timeouts(run.profile if run else worker.assignee),
                                'result': worker.result if mode == 'kanban' and worker.status == 'done' else None})
            return {'id': root, 'title': task.title, 'created_at':task.created_at, 'question': task.body, 'state': self._state(conn, topology), 'mode': mode,
                    'session_id': (self.session_lineage(topology.get('session_id') or task.session_id or '') or [''])[0], 'workers': workers,
                    'budget': topology['budget'],'time_plan':announced_plan(conn,root),
                    'state_reason':'；'.join(w['title']+'：'+(w['status_reason'] or '已阻塞') for w in workers if w['status']=='blocked'),
                    'publications': self.public(conn, root, topology),
                    'public_records': self.public_records(conn,root,topology)}

    def public(self, conn, root, topology):
        authors=self.authors(conn,topology)
        identities = set(authors)
        stages = {}; reviews = []
        for comment in kb.list_comments(conn, root):
            if comment.author not in identities or not comment.body.startswith(swarm.BLACKBOARD_PREFIX):
                continue
            try:
                payload = json.loads(comment.body[len(swarm.BLACKBOARD_PREFIX):])
            except ValueError:
                continue
            if not isinstance(payload,dict):continue
            value = payload.get('value'); key = payload.get('key', '')
            if not isinstance(value, dict) or not isinstance(key, str):
                continue
            if key.startswith('stage:') and all(isinstance(value.get(k), str) for k in ['claim', 'evidence', 'reproduction', 'limitations']) and key not in stages:
                stages[key] = {'id': key, 'author': comment.author, 'claim': value['claim'], 'evidence': value['evidence'],
                               'reproduction': value['reproduction'], 'limitations': value['limitations'],
                               'references': value.get('references', []), 'comment_id': comment.id, 'created': comment.created_at, 'reports': []}
                if isinstance(value.get('scope'),str) and value['scope'].strip():
                    stages[key]['scope']=value['scope'].strip()
            elif key.startswith('review:') and all(isinstance(value.get(k), str) for k in ['stage', 'verdict', 'evidence', 'reproduction']):
                reviews.append((comment, value))
        for comment, value in reviews:
            stage = stages.get(value['stage'])
            if stage and authors[comment.author]['task_id'] != authors[stage['author']]['task_id'] and value['verdict'] in ['PASS', 'FAIL', 'INCONCLUSIVE']:
                prior = [r for r in stage['reports'] if authors[r['author']]['task_id'] != authors[comment.author]['task_id']]
                stage['reports'] = prior + [{'author': comment.author, 'verdict': value['verdict'], 'evidence': value['evidence'],
                                            'reproduction': value['reproduction'], 'comment_id': comment.id}]
        for stage in stages.values():
            votes = [r['verdict'] for r in stage['reports']]
            stage['status'] = 'DISPUTED' if 'FAIL' in votes else 'PEER_SUPPORTED' if votes.count('PASS') >= 2 else 'PUBLISHED'
            stage['formal_verification'] = 'not_performed'
        return list(stages.values())

    def public_records(self, conn, root, topology):
        authors=self.authors(conn,topology)
        stages={s['id']:s for s in self.public(conn,root,topology)}
        records=[];seen=set()
        for comment in kb.list_comments(conn,root):
            if comment.author not in authors:continue
            base={**authors[comment.author],'comment_id':comment.id,'created':comment.created_at}
            if not comment.body.startswith(swarm.BLACKBOARD_PREFIX):
                records.append({**base,'id':f'comment:{comment.id}','kind':'note','claim':'公开发布','evidence':comment.body,
                                'reproduction':'','limitations':''});continue
            try:payload=json.loads(comment.body[len(swarm.BLACKBOARD_PREFIX):])
            except ValueError:continue
            if not isinstance(payload,dict):continue
            key=payload.get('key');value=payload.get('value')
            if not isinstance(key,str) or not isinstance(value,dict) or key in seen:continue
            if key in stages and stages[key]['comment_id']==comment.id:
                records.append({**base,**stages[key],'kind':'stage'});seen.add(key)
            elif key.startswith('review:') and all(isinstance(value.get(k),str) for k in ['stage','verdict','evidence','reproduction']) and value['verdict'] in {'PASS','FAIL','INCONCLUSIVE'}:
                target=stages.get(value['stage'])
                records.append({**base,'id':key,'kind':'review','stage':value['stage'],'verdict':value['verdict'],
                                'stage_claim':target['claim'] if target else '',
                                'stage_author':authors[target['author']]['author_label'] if target else '',
                                'independent':bool(target and authors[target['author']]['task_id']!=authors[comment.author]['task_id']),
                                'evidence':value['evidence'],'reproduction':value['reproduction'],
                                'limitations':value.get('limitations',''),
                                **({'scope':target['scope']} if target and 'scope' in target else {})});seen.add(key)
        return sorted(records,key=lambda r:r['comment_id'])

    def extension_requests(self, owner, root):
        from .budgets import requests, announced_plan
        with self.connect(owner) as (conn,_):
            _,topology=self._topology(conn,root)
            return {'root_id':root,'time_plan':announced_plan(conn,root),
                    'requests':requests(conn,root,topology,self.authors(conn,topology))}

    def resolve_extension(self, owner, root, request_comment_id, *, approve, reason, grant_seconds=None):
        from .budgets import resolve
        with self.connect(owner) as (conn,_):
            _,topology=self._topology(conn,root)
            return resolve(conn,root,topology,self.authors(conn,topology),request_comment_id,
                           approve=approve,reason=reason,grant_seconds=grant_seconds)

    def cancel(self, owner, root):
        with self.connect(owner) as (conn, _):
            _, topology = self._topology(conn, root)
            for item in topology['workers']:
                task = kb.get_task(conn, item['task_id'])
                if task.status == 'running':
                    kb.reclaim_task(conn, task.id, reason='User cancelled research')
                kb.archive_task(conn, task.id)
            swarm.post_blackboard_update(conn, root, author='research-coordinator', key='topology', value={**topology, 'state': 'cancelled'})
        return self.detail(owner, root)

    def tick(self, owner, *, spawn_fn=None, dry_run=False):
        with self.connect(owner) as (conn, board):
            return kb.dispatch_once(conn, board=board, spawn_fn=spawn_fn, dry_run=dry_run,
                                    max_in_progress=self.capacity, max_spawn=self.capacity,
                                    **self.dispatch_options())
