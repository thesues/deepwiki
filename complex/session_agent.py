"""Session-selected execution on Hermes; no second scheduler or Agent runtime."""
from contextlib import contextmanager
import os
from pathlib import Path

from hermes_agent import AgentPool, CHAT_DIRECTIVE, build_agent, run_turn
from . import coordinator


@contextmanager
def native_home(home):
    # Hermes already supports concurrent, context-local configuration/skills.
    # Never switch os.environ[HERMES_HOME] between concurrent conversations.
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def prepare_single(home):
    """Reuse the configured ordinary Hermes home, not the coordinator config."""
    from hermes_config import ensure_skills_directory, ensure_platform_toolsets, ensure_mcp_server
    home = Path(home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    config = home / 'config.yaml'
    shared = Path(__file__).resolve().parents[1] / 'webui'
    if not shared.is_dir():
        shared = Path(__file__).resolve().parents[1]
    ensure_skills_directory(config, shared / 'hermes' / 'skills')
    ensure_platform_toolsets(config)
    for item in os.environ.get('MCP_SERVERS', '').split(','):
        if '=' not in item:
            continue
        name, url = (part.strip() for part in item.split('=', 1))
        if name and url:
            ensure_mcp_server(config, name, url)
    with native_home(home):
        from hermes_cli.config import load_config
        servers = (load_config() or {}).get('mcp_servers') or {}
    return home, servers


def single_scope(agent):
    """Retain direct tools/MCP on refresh, but never spawn another Agent."""
    def permitted(name):
        return name != 'delegate_task' and not name.startswith(('kanban_', 'research_'))
    cls = type(agent)
    raw_tools, raw_names = agent.tools, agent.valid_tool_names
    def set_tools(self, value):
        self.__dict__['_single_tools'] = [t for t in (value or []) if permitted(t.get('function', {}).get('name', ''))]
    def set_names(self, value):
        self.__dict__['_single_names'] = {n for n in (value or []) if permitted(n)}
    agent.__class__ = type(cls.__name__ + 'Single', (cls,), {
        'tools': property(lambda s: s.__dict__.get('_single_tools', []), set_tools),
        'valid_tool_names': property(lambda s: s.__dict__.get('_single_names', set()), set_names),
    })
    agent.tools, agent.valid_tool_names = raw_tools, raw_names
    return agent


class SessionAgentPool(AgentPool):
    def __init__(self, runtime, single_home, *, single_toolsets=None):
        self.runtime = runtime
        self.single_home = single_home
        self.single_toolsets = single_toolsets
        super().__init__(builder=self.build)

    def build(self, session_id, endpoint, user_id=''):
        mode = coordinator.current_mode.get()
        toolsets = None
        if mode == 'single':
            from hermes_cli.config import load_config
            from hermes_config import resolve_toolsets
            from toolsets import resolve_toolset
            from model_tools import get_toolset_for_tool  # Performs native built-in tool discovery.
            requested = self.single_toolsets or resolve_toolsets(load_config() or {})
            # Expand native bundles to their leaf toolsets so Tool Search cannot
            # recover Kanban/delegation tools through a broad composed bundle.
            toolsets = set()
            for name in requested:
                if name.startswith('mcp-'):
                    toolsets.add(name)
                for tool in resolve_toolset(name):
                    leaf = get_toolset_for_tool(tool)
                    if leaf and leaf not in {'kanban', 'research-coordinator', 'delegation'}:
                        toolsets.add(leaf)
            toolsets = sorted(toolsets)
        agent = build_agent(session_id, endpoint, user_id=user_id, toolsets_override=toolsets)
        agent = single_scope(agent) if mode == 'single' else coordinator.scope(agent)
        agent._webui_execution_mode = mode
        return agent

    def acquire(self, session_id, endpoint, user_id=''):
        mode = self.runtime.session_mode(coordinator.owner(), session_id)
        with self._lock:
            cached = self._cache.get(session_id)
            changed = cached and cached[0]._webui_execution_mode != mode
        if changed:
            self.evict(session_id)  # Discard the old role's tools and cached prompt.
        token = coordinator.current_mode.set(mode)
        try:
            with native_home(self.single_home if mode == 'single' else self.runtime.home):
                return super().acquire(session_id, endpoint, user_id=user_id)
        finally:
            coordinator.current_mode.reset(token)

    def run(self, agent, **kwargs):
        owner = coordinator.owner()
        mode = agent._webui_execution_mode
        if self.runtime.session_mode(owner, kwargs['session_id']) != mode:
            raise RuntimeError('Session execution mode changed while the reply was starting')
        if mode == 'single':
            self.runtime.set_session_mode(owner, kwargs['session_id'], mode, lock=True)
            with native_home(self.single_home):
                return run_turn(agent, **kwargs)
        from hermes_agent import PROCESS_WAKEUP_SOURCE
        token = coordinator.current_mode.set(mode)
        notice = coordinator.read_only_notice.set(kwargs.get('user_source') == PROCESS_WAKEUP_SOURCE)
        message = kwargs.get('system_message') or CHAT_DIRECTIVE
        if message.startswith(CHAT_DIRECTIVE):
            message = message[len(CHAT_DIRECTIVE):]
        kwargs['system_message'] = coordinator.DIRECTIVE + message + f'\nCurrent UI-selected collaboration mode: {mode}.'
        try:
            with native_home(self.runtime.home), self.runtime.connect(owner):
                return run_turn(agent, **kwargs)
        finally:
            coordinator.current_mode.reset(token)
            coordinator.read_only_notice.reset(notice)
