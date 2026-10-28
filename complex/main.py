"""Unified direct Hermes / native Kanban / Swarm entry: python -m complex.main."""
from pathlib import Path
import os
import sys

HERE=Path(__file__).resolve().parent
SHARED=HERE.parent/'webui'
if not SHARED.is_dir():SHARED=HERE.parent  # Image layout.
sys.path.insert(0,str(SHARED))


def main():
    import argparse
    import hashlib
    import json
    import logging
    import signal
    import threading
    import time
    import yaml
    home=Path(os.environ.get('COMPLEX_HOME',str(Path.home()/'.complex'))).resolve()
    os.environ['HERMES_HOME']=str(home)
    os.environ['HERMES_KANBAN_HOME']=str(home)
    os.environ['HERMES_KANBAN_DB']=''  # Each owner has a native board, not one global DB override.
    os.environ.pop('HERMES_KANBAN_TASK',None)
    single_toolsets=[t.strip() for t in os.environ.pop('AGENT_TOOLSETS','').split(',') if t.strip()]
    parser=argparse.ArgumentParser(description='Agent WebUI: single Agent / native Kanban / research Swarm')
    parser.add_argument('--host',default=os.environ.get('HOST','127.0.0.1'))
    parser.add_argument('--port',type=int,default=int(os.environ.get('COMPLEX_PORT','8081')))
    args=parser.parse_args()
    home.mkdir(parents=True,exist_ok=True,mode=0o700)
    from .native_compat import worker_launcher
    os.environ['HERMES_BIN']=worker_launcher(home)
    logging.basicConfig(level=os.environ.get('LOG_LEVEL','INFO'),format='%(asctime)s %(levelname)s %(name)s %(message)s')
    log=logging.getLogger('complex')
    from hermes_agent import load_endpoints,register_webui_tools
    from media_storage import s3_enabled
    from http_shell import Response,json_response,serve
    from app_routes import build_app
    from turns import TurnManager,Refused
    from hermes_cli import kanban_db as kb
    from .swarm import ResearchSwarm, MODES
    from . import coordinator
    raw=os.environ.get('COMPLEX_ENDPOINTS','').strip()
    if not raw:
        path=Path(os.environ.get('COMPLEX_MODELS_FILE',str(HERE/'models.example.json')))
        rows=json.loads(path.read_text())
        for row in rows:
            if 'model_env' in row:
                row['model']=os.environ.get(row.pop('model_env'),'')
            if not row.get('model'):raise SystemExit('Complex model is not configured: '+str(row.get('key')))
        raw=json.dumps(rows)
    rows=json.loads(raw)
    capabilities={row['key']: {key: row[key] for key in ['toolsets','mcp_servers','api_key_env','agent','timeouts'] if key in row} for row in rows}
    if any(not row.get('api_key_env') or row.get('api_key') for row in rows):
        raise SystemExit('Complex worker credentials must use api_key_env, not inline keys')
    from .request_timeouts import settings
    from .budgets import run_ceiling
    settings();run_ceiling()  # Fail before service admission for malformed resource settings.
    for spec in capabilities.values():
        if 'toolsets' in spec and (not isinstance(spec['toolsets'],list) or any(not isinstance(t,str) for t in spec['toolsets'])):
            raise SystemExit('Worker toolsets must be a list of configured toolset names')
        if 'mcp_servers' in spec and not isinstance(spec['mcp_servers'],dict):
            raise SystemExit('Worker mcp_servers must be a Hermes MCP config mapping')
        if 'agent' in spec and not isinstance(spec['agent'],dict):
            raise SystemExit('Worker agent settings must be a native Hermes config mapping')
        if 'timeouts' in spec:
            if not isinstance(spec['timeouts'],dict) or set(spec['timeouts'])-{'api_timeout_seconds','api_stale_timeout_seconds','timeout_seconds','stale_timeout_seconds'}:
                raise SystemExit('Endpoint timeouts must contain only native/API timeout overrides; tool timeout uses COMPLEX_TOOL_TIMEOUT_SECONDS')
            settings(spec['timeouts'])
    models=load_endpoints(raw)
    if any(not os.environ.get(row['api_key_env']) for row in rows):
        raise SystemExit('Complex endpoint credentials are not configured; anonymous endpoints must explicitly configure an env value such as none')
    cfgpath=home/'config.yaml'
    cfg=yaml.safe_load(cfgpath.read_text()) if cfgpath.exists() else {}
    cfg=cfg or {}
    cfg.update({'toolsets':['kanban'],
                'platform_toolsets':{'cli':['kanban','skills','research-coordinator','clarify','input-image']},
                'skills':{'external_dirs':[str(HERE/'skills')]},
                'kanban':{**(cfg.get('kanban') or {}),'dispatch_in_gateway':False,'notify_in_gateway':False},'mcp_servers':{}})
    cfgpath.write_text(yaml.safe_dump(cfg,allow_unicode=True,sort_keys=False))
    (home/'SOUL.md').write_text('You only coordinate research and aggregate published evidence. Never solve the research question yourself.\n')
    runtime=ResearchSwarm(home,models,capacity=int(os.environ.get('COMPLEX_MAX_WORKERS','8')),capabilities=capabilities)
    coordinator.register(runtime);register_webui_tools()
    from tool_budget import install
    install()
    from .session_agent import SessionAgentPool, prepare_single, native_home
    single_home,servers=prepare_single(os.environ.get('COMPLEX_SINGLE_HOME') or os.environ.get('GENERAL_HOME') or home/'single')
    if servers:
        from tools import mcp_tool
        from tools.mcp_tool import register_mcp_servers
        from mcp_resilience import install_semantic_error_guard
        with native_home(single_home):
            install_semantic_error_guard(mcp_tool)
            added=register_mcp_servers(servers)
        log.info('Single Agent registered %d configured MCP tools',len(added))
    pool=SessionAgentPool(runtime,single_home,single_toolsets=single_toolsets)
    manager=TurnManager(pool,run=pool.run,workers=sum(m.max_concurrent for m in models))
    import hermes_session_api
    from auth import JWTAuthenticator
    jwks=os.environ.get('AUTH_JWKS_URL','').strip()
    local=os.environ.get('WEBUI_LOCAL_MODE')=='1'
    authenticator=JWTAuthenticator(jwks,audience='deepwiki') if jwks else None
    auth_user=os.environ.get('AUTH_USER','');auth_pass=os.environ.get('AUTH_PASS','')
    if local and (jwks or auth_user or auth_pass):raise SystemExit('WEBUI_LOCAL_MODE cannot be combined with authentication settings')
    if jwks and (auth_user or auth_pass):raise SystemExit('AUTH_JWKS_URL cannot be combined with AUTH_USER/AUTH_PASS')
    if not local and not jwks and not auth_user:raise SystemExit('Use existing AUTH_JWKS_URL online or WEBUI_LOCAL_MODE=1 on loopback')
    def page(body):
        body=body.replace(b'<body>',('<body data-execution-mode="single" data-welcome-title="我能帮你做什么？" data-welcome-subtitle="直接执行，也可以选择团队协作或独立群研" data-welcome-placeholder="发消息…">').encode())
        footer=(HERE/'static'/'mode.html').read_bytes()+('<button id="board-toggle" class="btn ghost compose-board-toggle" type="button" aria-controls="research-board" aria-expanded="false" title="查看当前会话的 Agent 与研究进展">Agent 看板</button>').encode()
        body=body.replace(b'<span id="model-badge" hidden>',footer+b'<span id="model-badge" hidden>')
        body=body.replace(b'<div id="approval-dock" hidden>',(HERE/'static'/'panel.html').read_bytes()+b'\n<div id="approval-dock" hidden>')
        for filename,tag in [('board.css','<link rel="stylesheet" href="{url}">'),('board.js','<script src="{url}"></script>')]:
            version=hashlib.sha256((HERE/'static'/filename).read_bytes()).hexdigest()[:12]
            url=f'/complex-static/{filename}?v={version}'
            body=body.replace(b'</body>',tag.format(url=url).encode()+b'\n</body>')
        return body
    from hermes_agent import _artifacts_root
    artifacts=_artifacts_root();artifacts.mkdir(parents=True,exist_ok=True)
    def chat_context(req,body,session_id):
        context=body.get('context')
        if not isinstance(context,dict):raise ValueError('页面执行方式未加载，请刷新页面后重新发送')
        mode=context.get('researchMode')
        if not isinstance(mode,str) or mode not in MODES:raise ValueError('页面执行方式无效，请重新选择后发送')
        if session_id in manager.running(req.user_id):raise ValueError('wait for the current reply before changing mode')
        runtime.set_session_mode(req.user_id or ('local' if local else 'basic:'+auth_user),session_id,mode)
    introduction=HERE.parent/'general'/'skills-introduction.html'
    app=build_app(welcome_html=introduction,manager=manager,endpoints=models,static_dir=SHARED/'static',index_html=SHARED/'static'/'index.html',
                  artifacts_dir=artifacts,authenticator=authenticator,auth_user=auth_user,auth_pass=auth_pass,
                  sessions=hermes_session_api,page_transform=page,chat_context=chat_context)
    app.local_identity=local
    def req_owner(req):
        return req.user_id or ('local' if local else 'basic:'+auth_user)
    from functools import wraps
    def route(method,path):
        def decorate(fn):
            @wraps(fn)
            def handler(req):
                try:return json_response(fn(req,req_owner(req)))
                except LookupError:return json_response({'error':'not found'},404)
                except (ValueError,PermissionError,KeyError,TypeError) as exc:return json_response({'error':str(exc)},400)
            app.route(method,path)(handler)
            return handler
        return decorate
    @route('GET','/api/kanban/swarms')
    def swarms(req,owner):return {'swarms':runtime.roots(owner)}
    @route('GET','/api/kanban/swarm')
    def detail(req,owner):return runtime.detail(owner,req.query.get('id',''))
    @route('GET','/api/kanban/inspect')
    def inspect(req,owner):
        with runtime.connect(owner) as (conn,_):return {'board':kb.board_stats(conn),'config':runtime.config()}
    def check_session(req,owner,session_id):
        if not session_id:return
        live_owner=manager.owner_of(session_id)
        if req.user_id:
            if live_owner==req.user_id or hermes_session_api.owner(session_id)==req.user_id:return
        elif hermes_session_api.exists(session_id) or live_owner is not None:return
        raise LookupError('session not found')
    @route('GET','/api/kanban/mode')
    def get_mode(req,owner):
        session=req.query.get('session_id','');check_session(req,owner,session)
        return {'session_id':session,**runtime.mode_config(owner,session)}
    @route('POST','/api/kanban/mode')
    def set_mode(req,owner):
        data=req.json();session=data.get('session_id','');check_session(req,owner,session)
        if session in manager.running(req.user_id):raise ValueError('wait for the current reply before changing mode')
        runtime.set_session_mode(owner,session,data.get('mode'))
        return {'session_id':session,**runtime.mode_config(owner,session)}
    @route('POST','/api/kanban/cancel')
    def cancel(req,owner):return runtime.cancel(owner,req.json()['id'])
    @app.route('GET','/api/kanban/attachment')
    def attachment(req):
        try:attachment_id=int(req.query.get('id',''))
        except ValueError:return json_response({'error':'invalid attachment id'},400)
        with runtime.connect(req_owner(req)) as (conn,board):
            if req.query.get('board')!=board:return json_response({'error':'not found'},404)
            item=kb.get_attachment(conn,attachment_id)
            if item is None:return json_response({'error':'not found'},404)
            path=Path(item.stored_path).resolve()
            try:path.relative_to((kb.board_dir(board)/'attachments').resolve())
            except ValueError:return json_response({'error':'not found'},404)
            from urllib.parse import quote
            return app._send_file(req,path,item.content_type or 'application/octet-stream',
                                  'private, max-age=31536000, immutable',use_last_modified=False,use_metadata_etag=False,
                                  extra_headers=[('Content-Disposition',"attachment; filename*=UTF-8''"+quote(item.filename,safe=''))]) or json_response({'error':'not found'},404)
    for filename,kind in [('board.js','application/javascript'),('board.css','text/css')]:
        app.route('GET','/complex-static/'+filename)(lambda req,f=filename,k=kind:app._serve_immutable_file(req,HERE/'static'/f,k))
    stop=threading.Event()
    server=serve(app,args.host,args.port)
    log.info('Agent WebUI single / Kanban / Swarm on http://%s:%s; storage=%s',args.host,args.port,'s3' if s3_enabled() else 'filesystem')
    if servers:
        from mcp_resilience import recover_mcp_servers
        def recover():
            with native_home(single_home):recover_mcp_servers(mcp_tool,servers,stop)
        threading.Thread(target=recover,name='single-mcp-recovery',daemon=True).start()
    # Native task dispatcher and persisted notification cursors, not another job queue.
    from .notifications import PublicationNotifier
    notifier=PublicationNotifier(runtime,manager,models,authenticated=bool(jwks))
    def dispatch():
        while not stop.wait(2):
            for board in kb.list_boards(include_archived=False):
                slug=board['slug']
                if not slug.startswith('research-'):continue
                try:
                    with kb.scoped_current_board(slug):
                        conn=kb.connect(board=slug)
                        try:
                            released=runtime.release_legacy_drafts(conn)
                            if released:log.info('Released %d retired confirmation blocks on %s',released,slug)
                            kb.dispatch_once(conn,board=slug,max_in_progress=runtime.capacity,max_spawn=runtime.capacity,**runtime.dispatch_options())
                            for sub in kb.list_notify_subs(conn):
                                if sub['platform']!='complex-webui':continue
                                notifier.deliver(conn,sub,slug)
                        finally:conn.close()
                except Exception:log.exception('Native Kanban dispatch/notification failed for %s',slug)
    threading.Thread(target=dispatch,name='native-kanban-dispatch',daemon=True).start()
    manager.start_completion_dispatcher(stop)
    def bye(*_):
        stop.set();server.shutdown()
        # Native workers are durable and managed by Kanban; shutdown does not
        # signal unrelated PIDs or erase their tasks. User cancel uses reclaim.
    signal.signal(signal.SIGTERM,bye);signal.signal(signal.SIGINT,bye)
    stop.wait()


if __name__=='__main__':main()
