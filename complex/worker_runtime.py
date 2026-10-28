"""Small native Worker boundary: accepted completion ends this execution.

Stage comments/reviews do NOT end a Swarm Worker. Native task completion gates
remain authoritative. No research strategy or process scheduler lives here.
"""
import json
import os


def install_request_bounds(values):
    """Bound native local commands independently of the Worker's lifetime.

    Native registry spawn/ownership/checkpoints/kill remain authoritative. Each
    local command executes through a portable timeout subprocess, not a daemon
    timer in the Worker. That executable reads the originating Worker's explicit
    PID/birth identity and stops only its OWN payload when the Worker ends/reuses.
    Remote background execution remains fail-closed because
    native remote PID killing cannot prove birth identity or terminate a tree.
    """
    import functools
    import inspect
    import math
    from contextvars import ContextVar
    from tools import terminal_tool as terminal
    from tools.process_registry import ProcessRegistry, _find_shell
    from tools.environments import local
    from .command_timeout import wrap_command, TERMINATION_GRACE_SECONDS

    if getattr(terminal.terminal_tool, '_research_request_bound', False):return
    limit = values['tool_timeout_seconds']
    request = ContextVar('research_terminal_request', default=None)
    tree_cleanup = ContextVar('research_terminal_tree_cleanup', default=False)
    original_terminal = terminal.terminal_tool
    signature = inspect.signature(original_terminal)
    original_spawn = ProcessRegistry.spawn_local
    spawn_signature = inspect.signature(original_spawn)
    original_finish = ProcessRegistry._move_to_finished
    original_kill = ProcessRegistry.kill_process
    original_run_bash = local.LocalEnvironment._run_bash
    original_local_kill = local.LocalEnvironment._kill_process
    original_grace = ProcessRegistry._daemon_term_grace_seconds

    def terminate_owned_tree(pid, birth):
        token = tree_cleanup.set(True)
        try:ProcessRegistry._terminate_host_pid(pid, expected_start=birth)
        finally:tree_cleanup.reset(token)

    def grace():
        seconds = original_grace()
        # Native grace=0 means SIGTERM-only. Wrapped normal timeout/PTY cleanup
        # must escalate ignoring descendants; unrelated native kills stay exact.
        return max(seconds, TERMINATION_GRACE_SECONDS) if tree_cleanup.get() else seconds

    def reject_detachment(command):
        if not isinstance(command, str):return
        # Native foreground already rejects these. A detached payload whose
        # root exits cannot be safely identified by the native registry later.
        unquoted = terminal._strip_quotes(command)
        if any(pattern.search(unquoted) for pattern in (
                terminal._SHELL_LEVEL_BACKGROUND_RE, terminal._INLINE_BACKGROUND_AMP_RE,
                terminal._TRAILING_BACKGROUND_AMP_RE)):
            raise ValueError('Do not detach a Research Worker background payload with nohup/disown/setsid/&. Run the command directly with background=true so its native process identity remains tracked.')

    def preflight():
        owner_pid = os.getpid()
        owner_birth = ProcessRegistry._safe_host_start_time(owner_pid)
        if owner_birth is None:
            raise RuntimeError('Cannot safely bound local commands: host birth-time probing unavailable')
        import psutil
        try:
            psutil.Process(owner_pid).children(recursive=True)
        except (psutil.Error, OSError) as error:
            raise RuntimeError('Cannot safely bound local commands: host process-tree inspection unavailable') from error
        return owner_pid, owner_birth  # explicit originating Worker, never PPID

    @functools.wraps(original_terminal)
    def bounded_terminal(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        requested = bound.arguments.get('timeout')
        if requested is not None and (isinstance(requested, bool) or not isinstance(requested, (int, float))
                                      or requested <= 0 or not math.isfinite(requested)):
            return json.dumps({'status':'error', 'error':'timeout must be positive finite seconds', 'exit_code':-1})
        if bound.arguments.get('background'):
            try:reject_detachment(bound.arguments.get('command'))
            except ValueError as error:
                return json.dumps({'status':'error', 'exit_code':-1, 'error':str(error)})
        seconds = limit if requested is None else min(requested, limit)
        bound.arguments['timeout'] = seconds
        token = request.set(seconds)
        try:
            # Guards/approval still see the ORIGINAL command. Wrapping happens
            # only at the native local spawn boundary, after those checks.
            result = original_terminal(*bound.args, **bound.kwargs)
            try:data = json.loads(result)
            except (TypeError, ValueError):return result
            if isinstance(data, dict) and data.get('session_id') and not data.get('error'):
                data['execution_timeout_seconds'] = seconds
                return json.dumps(data, ensure_ascii=False)
            return result
        finally:request.reset(token)

    @functools.wraps(original_spawn)
    def spawn(registry, *args, **kwargs):
        bound = spawn_signature.bind(registry, *args, **kwargs)
        command = bound.arguments['command']
        reject_detachment(command)
        owner_pid, owner_birth = preflight()
        wrapped, receipt = wrap_command(command, request.get() or limit, _find_shell(),
                                        owner_pid=owner_pid, owner_birth=owner_birth)
        bound.arguments['command'] = wrapped
        session = original_spawn(*bound.args, **bound.kwargs)
        if session.detached or session.pid_scope != 'host' or session.host_start_time is None:
            # The executable still owns its payload deadline, but this Worker
            # must not claim a safely killable native registry identity.
            raise RuntimeError('Native background session ' + session.id + ' lacks safe birth identity; its command wrapper still enforces the deadline')
        # Only this newly returned native object is tagged. No existing session
        # is scanned/adopted, and native PID/birth identity is unchanged by exec.
        session._research_timeout_token = receipt
        return session

    @functools.wraps(original_run_bash)
    def run_bash(env, command, *, login=False, timeout=120, stdin_data=None):
        seconds = request.get()
        if seconds is None:
            return original_run_bash(env, command, login=login, timeout=timeout, stdin_data=stdin_data)
        owner_pid, owner_birth = preflight()
        # Preserve native foreground snapshot/cwd markers and shell startup:
        # they execute INSIDE the payload. The outer shell only execs timeout.
        if login:
            init_files = local._resolve_shell_init_files()
            if init_files:command = local._prepend_shell_init(command, init_files)
        wrapped, receipt = wrap_command(command, min(timeout, seconds), local._find_bash(),
                                        shell_flags='-lc' if login else '-c',
                                        owner_pid=owner_pid, owner_birth=owner_birth)
        proc = original_run_bash(env, wrapped, login=False, timeout=timeout, stdin_data=stdin_data)
        proc._research_timeout_token = receipt
        proc._research_wrapper_birth = ProcessRegistry._safe_host_start_time(proc.pid)
        return proc

    @functools.wraps(original_local_kill)
    def kill_local(env, proc):
        if not getattr(proc, '_research_timeout_token', None):
            return original_local_kill(env, proc)
        birth = getattr(proc, '_research_wrapper_birth', None)
        if birth is None or not ProcessRegistry._host_pid_is_ours(proc.pid, birth):
            # Never use native's cached PGID fallback on an unverifiable or
            # already-reaped wrapper; that number could belong to a stranger.
            local.logger.error('Cannot revalidate foreground wrapper identity for pid %s; no signal sent', proc.pid)
            return
        # Native LocalEnvironment timeout only TERMs the wrapper GROUP, then
        # KILLs it after 1s. Its payload has a different group, and cleanup/import
        # latency could otherwise let that normal escalation kill the wrapper
        # first. Snapshot/terminate the proven native TREE instead, including
        # payload/setsid descendants, before allowing the wrapper to disappear.
        terminate_owned_tree(proc.pid, birth)
        try:proc.wait(timeout=0.5)
        except Exception:
            local.logger.error('Foreground native tree cleanup did not collect pid %s', proc.pid)

    @functools.wraps(original_kill)
    def kill(registry, session_id, *, source='process.kill', consume_output=True):
        session = registry.get(session_id)
        # Native PTY terminate(force=True) only signals the wrapper and may
        # SIGKILL it before its own cleanup completes. Preserve the native tree
        # path first, ONLY for this Worker's freshly wrapped, proven identity.
        if (session is not None and getattr(session, '_research_timeout_token', None)
                and session._pty and not session.exited and session.host_start_time is not None
                and registry._host_pid_is_ours(session.pid, session.host_start_time)):
            terminate_owned_tree(session.pid, session.host_start_time)
        return original_kill(registry, session_id, source=source, consume_output=consume_output)

    @functools.wraps(original_finish)
    def finish(registry, session):
        receipt = getattr(session, '_research_timeout_token', None)
        # Exit 124 by itself may be the payload's own exit status. Attribute
        # the deadline only with this fresh command's unguessable receipt.
        if (receipt and session.exit_code == 124 and not session.termination_source
                and '[research-command-timeout:' + receipt + ']' in session.output_buffer):
            session.termination_source = 'research.command_timeout'
        return original_finish(registry, session)

    def refuse_remote_background(*args, **kwargs):
        raise RuntimeError('Research Worker background commands require the local backend for identity-checked bounded execution; use a foreground command on this backend')

    bounded_terminal._research_request_bound = True
    terminal.terminal_tool = bounded_terminal
    terminal.TERMINAL_SCHEMA['parameters']['properties']['timeout']['description'] = (
        f'Per-command execution seconds (default and hard maximum {limit}); larger values are clamped. '
        'Local commands also stop when their originating Worker exits; the configured deadline remains independent. '
        'Remote background commands are unavailable in Research Workers.'
    )
    ProcessRegistry.spawn_local = spawn
    ProcessRegistry.spawn_via_env = refuse_remote_background
    ProcessRegistry._move_to_finished = finish
    ProcessRegistry.kill_process = kill
    ProcessRegistry._daemon_term_grace_seconds = staticmethod(grace)
    local.LocalEnvironment._run_bash = run_bash
    local.LocalEnvironment._kill_process = kill_local


def configure(agent, task_id):
    agent._research_done = False
    agent._skill_nudge_interval = 0
    agent._memory_nudge_interval = 0
    agent._spawn_background_review = lambda *args, **kwargs: None
    # Skills are readable; Workers must not delegate again or rewrite shared
    # research instructions from their private conversations.
    forbidden = {'delegate_task', 'skill_manage'}
    tools=agent.tools;names=agent.valid_tool_names;cls=type(agent)
    def set_tools(self,value):self.__dict__['_worker_tools']=[t for t in (value or []) if t.get('function',{}).get('name') not in forbidden]
    def set_names(self,value):self.__dict__['_worker_names']=set(value or [])-forbidden
    agent.__class__=type(cls.__name__+'ResearchWorker',(cls,),{
        'tools':property(lambda s:s.__dict__.get('_worker_tools',[]),set_tools),
        'valid_tool_names':property(lambda s:s.__dict__.get('_worker_names',set()),set_names)})
    agent.tools=tools;agent.valid_tool_names=names
    previous = agent.tool_complete_callback
    def completed(call_id, name, args, result):
        if previous:previous(call_id, name, args, result)
        if name != 'kanban_complete':return
        try:data = json.loads(result)
        except (TypeError, ValueError):return
        if isinstance(data,dict) and data.get('ok') is True and data.get('task_id') == task_id:
            agent._research_done = True
            agent.interrupt('Native Kanban task delivered; Worker execution finished.')
    agent.tool_complete_callback = completed
    original = agent._execute_tool_calls
    def execute(message, messages, effective_task_id, api_call_count=0):
        # Completion is a serial boundary even if emitted with other tools.
        # Native interrupt handling persists its result and cancels later calls.
        if any(t.function.name == 'kanban_complete' for t in message.tool_calls):
            agent._executing_tools = True
            try:return agent._execute_tool_calls_sequential(message, messages, effective_task_id, api_call_count)
            finally:agent._executing_tools = False
        return original(message, messages, effective_task_id, api_call_count)
    agent._execute_tool_calls = execute


def install():
    task_id = os.environ.get('HERMES_KANBAN_TASK', '')
    if not task_id:return
    # _default_spawn derives these from max_runtime_seconds. Reset BEFORE
    # run_agent/CLI imports tools (foreground max is an import-time constant).
    from .request_timeouts import configure_worker_environment
    values = configure_worker_environment()
    # Do not inherit the conversational coordinator's directive in a Worker.
    os.environ.pop('CHAT_DIRECTIVE', None)
    install_request_bounds(values)
    from run_agent import AIAgent
    original = AIAgent.__init__
    if getattr(original, '_research_worker', False):return
    from .budgets import register_worker_request
    register_worker_request(task_id)
    def init(self, *args, **kwargs):
        # CLI profile/.env bridging happens after shim installation and may
        # replace terminal.timeout with native defaults. Restore the independent
        # fallbacks here too; native per-model API configuration still wins.
        configure_worker_environment()
        original(self, *args, **kwargs)
        configure(self, task_id)
    init._research_worker = True
    AIAgent.__init__ = init
