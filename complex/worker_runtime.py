"""Small native Worker boundary: accepted completion ends this execution.

Stage comments/reviews do NOT end a Swarm Worker. Native task completion gates
remain authoritative. No research strategy or process scheduler lives here.
"""
import json
import os


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
    # Do not inherit the conversational coordinator's directive in a Worker.
    os.environ.pop('CHAT_DIRECTIVE', None)
    from run_agent import AIAgent
    original = AIAgent.__init__
    if getattr(original, '_research_worker', False):return
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        configure(self, task_id)
    init._research_worker = True
    AIAgent.__init__ = init
