"""Conversation coordinator: native Kanban tools plus a research topology helper."""
from copy import deepcopy
from contextvars import ContextVar

current_mode = ContextVar('complex_research_mode', default='kanban')
read_only_notice = ContextVar('complex_publication_notice', default=False)
import json
import os
from pathlib import Path

DIRECTIVE = """You coordinate research tasks on native Hermes Kanban.
ONLY aggregate and coordinate. Never solve the research problem, derive proofs,
find counterexamples, implement candidates, run experiments or act as a verifier.
Call research_config and load research-orchestration to read the selected session mode.
Default kanban mode: decompose the objective into assigned tasks with explicit
acceptance criteria and dependencies. Workers publish completed deliverables;
native Kanban releases dependent tasks automatically.
Swarm mode: all workers receive the identical question/baseline, choose their own
direction, repeatedly publish public stages, review peers and continue independently.
Unpublished notes/workspaces remain private in both modes.
Create tasks with research_create. Creation automatically releases the complete
graph to native dispatch; there is NO draft or manual start button. Explain actual
models/budgets/costs before creating work, and never pretend task creation is free.
The user selects the mode below the message input before execution. Once this session
has execution tasks its mode is fixed; another mode requires a new session.
Do not silently change the choice. Use native kanban_show/list/comment to coordinate.
Use research_status for actual operations, run outcomes and dependency blockers.
Default Kanban uses single-shot execution, not continuous research or goal judge.
For simple work use the smallest useful graph; do not require full-project validation
on every implementation card. Native retries follow operator Kanban configuration.
Choose Swarm run time from problem difficulty, expected experiments/proof work,
peer verification and model latency. Do not blindly reuse a short execution-task
budget. Swarm research_create requires time_plan: explain assessment and announce
max_run_seconds as the extension ceiling. All workers start with the same budget.
Never restart an exhausted run or increase its timeout outside an announced policy
without user authorization. A live Worker may request time on the native root;
use research_extension_requests and research_resolve_extension to judge and decide
within the announced ceiling. Granting does not restart/reset the run or increase
its turn limits. Per-tool/API request timeouts are separate from total run time.
When the user explicitly requests changing the model of a stopped task, use
research_reassign on that same card, preserving its root, workspace and dependencies.
Do not mistake your tool restrictions for missing native Kanban capabilities.
Do not rank models without measured evidence. Heartbeat proves liveness, not progress.
Report actual model/tool timing from research_status; never invent a Worker chat/log UI.
Native attachments are downloadable from the Agent board. You have no terminal or
file tools; do not promise to unpack/inspect artifacts yourself or call it zero cost.
Read research_publications for published stages AND peer verification reports;
attribute each finding/verdict and its scope to the actual Agent/model. Report
partial candidates as partial and pending review, not as completion of the entire
problem. Missing publication means unpublished, not unsolved. Never invent scope,
retroactive stages or publication times for old records. Preserve evidence and
limits, and proactively summarize new publications to the user. Use evidence-review.
Publication notifications trigger observation/reporting and, when present,
review of authenticated extension requests within the already announced ceiling;
they never authorize more tasks, stopped-run recovery or raising that ceiling.
A task done or peer PASS is not a formal proof. Missing capability is a blocker,
not permission to solve or verify the problem yourself.
"""


def owner():
    from gateway.session_context import get_session_env
    value=get_session_env('HERMES_SESSION_USER_ID','')
    if value:return value
    if os.environ.get('WEBUI_LOCAL_MODE')=='1':return 'local'
    if os.environ.get('AUTH_USER') and not os.environ.get('AUTH_JWKS_URL'):
        return 'basic:' + os.environ['AUTH_USER']
    raise PermissionError('research coordinator requires an authenticated user')


def register(runtime):
    from tools import kanban_tools  # Native schemas/handlers; no private research protocol.
    from tools.registry import registry
    native=['kanban_show','kanban_list','kanban_comment','kanban_attachments']
    for name in native:
        entry=registry.get_entry(name)
        if entry is None:raise RuntimeError('Installed Hermes lacks '+name)
        original=entry.handler;schema=deepcopy(entry.schema)
        schema.get('parameters',{}).get('properties',{}).pop('board',None)
        def handler(args,_original=original,_name=name,**kw):
            try:
                current=owner();board=runtime.board(current)
                if args.get('board') not in (None,'',board):raise PermissionError('other board is unavailable')
                published_delivery=False
                with runtime.connect(current) as (conn,_):
                    result = _original({**args,'board':board},**kw)
                    if _name == 'kanban_show' and args.get('task_id'):
                        from hermes_cli import kanban_db as kb
                        from hermes_cli.kanban_swarm import latest_blackboard
                        published_delivery=any(latest_blackboard(conn,parent).get('topology',{}).get('mode')=='kanban'
                                               for parent in kb.parent_ids(conn,args['task_id']))
                if _name == 'kanban_show':
                    data = json.loads(result)
                    if 'task' not in data:return result
                    data.pop('worker_context',None)
                    task = data['task'];task.pop('workspace_path',None)
                    # Worker card comments/results are private; only its status
                    # and explicitly public heartbeat notes reach the lead.
                    if task.get('created_by') == 'research-coordinator' and task.get('assignee') != 'default':
                        delivered=published_delivery and task.get('status')=='done'
                        if not delivered:task.pop('result',None)
                        data['comments'] = []
                        data['events'] = [e for e in data['events'] if e['kind'] == 'heartbeat']
                        for run in data['runs']:
                            for key in ['metadata','error',*([] if delivered else ['summary'])]:run.pop(key,None)
                    return json.dumps(data,ensure_ascii=False)
                return result
            except (ValueError,LookupError,PermissionError) as exc:return json.dumps({'error':str(exc)})
        registry.register(name=name,toolset='kanban',schema=schema,handler=handler)
    def config(args,**_):return json.dumps({**runtime.config(),'mode':current_mode.get()},ensure_ascii=False)
    def facts(group):
        workers=[]
        for w in group['workers']:
            item={k:w[k] for k in ['id','title','model','status','activity','activity_source','activity_at','status_reason',
                                  'dependencies','consecutive_failures','failure_limit','goal_mode','stats','timing','attachments']}
            item['run']={k:w['run'][k] for k in ['id','status','outcome','started_at','ended_at','max_runtime_seconds']} if w['run'] else None
            item['extensions']=[{k:r[k] for k in ['comment_id','run_id','status','extra_seconds','limit_seconds','max_run_seconds','can_approve']} for r in w.get('extensions',[])]
            item['request_timeouts']=w.get('request_timeouts')
            workers.append(item)
        return {'id':group['id'],'mode':group['mode'],'state':group['state'],'state_reason':group['state_reason'],
                'budget':group['budget'],'time_plan':group.get('time_plan'),'workers':workers}
    def status(args,**_):
        try:return json.dumps(facts(runtime.detail(owner(),args['root_id'])),ensure_ascii=False)
        except (ValueError,LookupError,PermissionError,KeyError) as exc:return json.dumps({'error':str(exc)})
    registry.register(name='research_status',toolset='research-coordinator',handler=status,
                      schema={'name':'research_status','description':'Read actual native worker/run status, operation metadata, timeout budgets and dependency blockers. No private reasoning, workspace contents or tool arguments.',
                              'parameters':{'type':'object','properties':{'root_id':{'type':'string'}},'required':['root_id']}})
    def create(args,**_):
        if read_only_notice.get():return json.dumps({'error':'Publication notifications only observe and report; task creation is unavailable.'})
        from gateway.session_context import get_session_env
        try:
            result=runtime.create(owner(),question=args['question'],models=args['models'],budget=args['budget'],
                                  baseline=args.get('baseline',''),session_id=get_session_env('HERMES_SESSION_KEY',''),
                                  mode=current_mode.get(),tasks=args.get('tasks'),time_plan=args.get('time_plan'))
            # Return bounded coordination facts, not private workspaces or credentials.
            return json.dumps(facts(result),ensure_ascii=False)
        except (ValueError,LookupError,PermissionError,KeyError) as exc:return json.dumps({'error':str(exc)})
    def reassign(args,**_):
        if read_only_notice.get():return json.dumps({'error':'Notification turns cannot reassign or restart Workers.'})
        if args.get('user_authorized') is not True:return json.dumps({'error':'Explicit user authorization is required.'})
        try:return json.dumps(facts(runtime.reassign(owner(),args['task_id'],args['model'])),ensure_ascii=False)
        except (ValueError,LookupError,PermissionError,KeyError,RuntimeError) as exc:return json.dumps({'error':str(exc)})
    registry.register(name='research_reassign',toolset='research-coordinator',handler=reassign,
                      schema={'name':'research_reassign','description':'After explicit user authorization, reassign a stopped unfinished card to a configured model and natively unblock it. Keeps the same root, workspace, dependencies, history and timeout; do not create a replacement project.',
                              'parameters':{'type':'object','properties':{'task_id':{'type':'string'},'model':{'type':'string'},
                                            'user_authorized':{'type':'boolean'}},'required':['task_id','model','user_authorized']}})
    registry.register(name='research_config',toolset='research-coordinator',handler=config,
                      schema={'name':'research_config','description':'Read actual configured models, capacity, native research mode and budget semantics.',
                              'parameters':{'type':'object','properties':{}}})
    def extension_requests(args,**_):
        try:
            after=args.get('after_comment_id',0);limit=args.get('limit',4)
            if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=8:raise ValueError('invalid extension cursor/limit')
            result=runtime.extension_requests(owner(),args['root_id'])
            rows=[r for r in result['requests'] if r['comment_id']>after];page=[]
            for row in rows[:limit]:
                item={**row,'excerpted':False}
                for key,size in [('reason',400),('plan',500)]:
                    if len(item[key])>size:item['excerpted']=True
                    item[key]=item[key][:size]
                if item['decision']:
                    item['decision']={**item['decision'],'reason':item['decision']['reason'][:400]}
                page.append(item)
            return json.dumps({'root_id':args['root_id'],'time_plan':result['time_plan'],'requests':page,
                               'next_comment_id':page[-1]['comment_id'] if page else after,'has_more':len(rows)>limit},ensure_ascii=False)
        except (ValueError,LookupError,PermissionError,KeyError) as exc:return json.dumps({'error':str(exc)})
    def resolve_extension(args,**_):
        try:
            return json.dumps(runtime.resolve_extension(owner(),args['root_id'],args['request_comment_id'],
                approve=args['approve'],reason=args['reason'],grant_seconds=args.get('grant_seconds')),ensure_ascii=False)
        except (ValueError,LookupError,PermissionError,KeyError,RuntimeError) as exc:return json.dumps({'error':str(exc)})
    registry.register(name='research_extension_requests',toolset='research-coordinator',handler=extension_requests,
        schema={'name':'research_extension_requests','description':'Read authenticated Worker extension requests and native decisions. Time requests are not research evidence. Only live unexpired runs within the announced ceiling are eligible.',
                'parameters':{'type':'object','properties':{'root_id':{'type':'string'},'after_comment_id':{'type':'integer'},'limit':{'type':'integer','minimum':1,'maximum':8}},'required':['root_id']}})
    registry.register(name='research_resolve_extension',toolset='research-coordinator',handler=resolve_extension,
        schema={'name':'research_resolve_extension','description':'Approve/reject an authenticated native-root extension request within its announced run ceiling. Atomic and idempotent; keeps the same run/start time/turn limits. Never resumes stopped or expired runs. Optional grant_seconds permits a smaller grant than requested.',
                'parameters':{'type':'object','properties':{'root_id':{'type':'string'},'request_comment_id':{'type':'integer'},'approve':{'type':'boolean'},'reason':{'type':'string'},'grant_seconds':{'type':'integer','minimum':1}},'required':['root_id','request_comment_id','approve','reason']}})
    def publications(args,**_):
        try:
            limit=int(args.get('limit',2));after=int(args.get('after_comment_id',0))
            if not 1<=limit<=2 or after<0:raise ValueError('limit is 1..2; cursor must be nonnegative')
            with runtime.connect(owner()) as (conn,_):
                _,topology=runtime._topology(conn,args['root_id'])
                rows=[r for r in runtime.public_records(conn,args['root_id'],topology) if r['comment_id']>after]
            page=[]
            for row in rows[:limit]:
                item={k:v for k,v in row.items() if k not in {'reports','references'}}
                item['excerpted']=False
                for key,size in [('claim',350),('stage_claim',200),('evidence',400),('reproduction',250),('limitations',200),('scope',120)]:
                    if key=='scope' and key not in item:continue
                    value=str(item.get(key,'') or '')
                    if len(value)>size:item['excerpted']=True
                    item[key]=value[:size]
                page.append(item)
            return json.dumps({'next_comment_id':page[-1]['comment_id'] if page else after,'has_more':len(rows)>limit,'records':page},ensure_ascii=False)
        except (ValueError,LookupError,PermissionError,KeyError) as exc:return json.dumps({'error':str(exc)})
    registry.register(name='research_publications',toolset='research-coordinator',handler=publications,
                      schema={'name':'research_publications','description':'Read public stage/verification records with declared scope and Agent/model provenance, in bounded cursor-paged excerpts. Scope is coverage, not proof of completion. Includes peer PASS/FAIL evidence. Does not execute verification.',
                              'parameters':{'type':'object','properties':{'root_id':{'type':'string'},'after_comment_id':{'type':'integer'},'limit':{'type':'integer','minimum':1,'maximum':2}},'required':['root_id']}})
    registry.register(name='research_create',toolset='research-coordinator',handler=create,
                      schema={'name':'research_create','description':'Create and automatically dispatch tasks in the UI-selected session mode. Kanban requires assigned tasks/dependencies; Swarm gives everyone the same question. This consumes model budget.',
                              'parameters':{'type':'object','properties':{'question':{'type':'string'},
                                  'models':{'type':'array','items':{'type':'string'}},
                                  'budget':{'type':'object','properties':{'wall_seconds':{'type':'integer'},'max_iterations':{'type':'integer'},'max_turns':{'type':'integer'}},'required':['wall_seconds','max_iterations','max_turns']},
                                  'time_plan':{'type':'object','description':'Required for Swarm. Assess problem difficulty, research/verification work and model latency; choose initial budget and this extension ceiling, not a fixed short timeout.',
                                               'properties':{'assessment':{'type':'string'},'max_run_seconds':{'type':'integer','minimum':30}},'required':['assessment','max_run_seconds']},
                                  'baseline':{'type':'string'},
                                  'tasks':{'type':'array','description':'Required in default Kanban mode, omitted in Swarm mode. One specification per model, in dependency order.',
                                           'items':{'type':'object','properties':{'title':{'type':'string'},'body':{'type':'string'},
                                                   'depends_on':{'type':'array','items':{'type':'integer'},'description':'Zero-based indices of earlier tasks'}},'required':['title','body']}}},'required':['question','models','budget']}})


def scope(agent):
    """Keep research leads coordinator-only, including native tool refreshes."""
    if getattr(agent,'_coordination_scope',False):return agent
    allowed={'kanban_show','kanban_list','kanban_comment','kanban_attachments',
             'research_config','research_create','research_reassign','research_publications','research_status',
             'research_extension_requests','research_resolve_extension','skill_view','skills_list','clarify','input_image_open'}
    raw_tools=agent.tools;raw_names=agent.valid_tool_names
    cls=type(agent)
    def set_tools(self,value):self.__dict__['_coordinator_tools']=[t for t in (value or []) if t.get('function',{}).get('name') in allowed]
    def set_names(self,value):self.__dict__['_coordinator_names']=set(value or [])&allowed
    agent.__class__=type(cls.__name__+'Coordinator',(cls,),{
        'tools':property(lambda s:s.__dict__.get('_coordinator_tools',[]),set_tools),
        'valid_tool_names':property(lambda s:s.__dict__.get('_coordinator_names',set()),set_names)})
    agent.tools=raw_tools;agent.valid_tool_names=raw_names;agent._coordination_scope=True
    return agent


def pool():
    from hermes_agent import AgentPool
    class CoordinatorPool(AgentPool):
        def acquire(self,*args,**kwargs):return scope(super().acquire(*args,**kwargs))
    return CoordinatorPool()
