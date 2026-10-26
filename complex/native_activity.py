"""Read existing Hermes operation metadata; never expose arguments or reasoning."""
from datetime import datetime
from functools import lru_cache
import re
import sqlite3


def pending_tools(run):
    if run is None or not run.profile:return None
    from hermes_cli.profiles import get_profile_dir
    path=get_profile_dir(run.profile)/'state.db'
    try:
        conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
        try:
            rows=conn.execute('''
                SELECT json_extract(j.value, '$.function.name'), m.timestamp
                FROM messages m, json_each(m.tool_calls) j
                WHERE m.id=(SELECT id FROM messages WHERE role='assistant'
                  AND tool_calls IS NOT NULL AND timestamp>=? ORDER BY id DESC LIMIT 1)
                  AND NOT EXISTS (SELECT 1 FROM messages t WHERE t.role='tool'
                    AND t.tool_call_id=json_extract(j.value, '$.id'))
            ''',(run.started_at or 0,)).fetchall()
        finally:conn.close()
        names=[name for name,_ in rows if isinstance(name,str) and re.fullmatch(r'[\w.-]{1,100}',name)]
        if names:return {'text':'等待工具结果：'+', '.join(names),'at':max(at for _,at in rows)}
    except (OSError,ValueError,sqlite3.Error):pass
    return None

_TOOL = re.compile(r'agent\.tool_executor: tool ([\w.-]+) completed \(([\d.]+)s,')
_API = re.compile(r'agent\.conversation_loop: API call #(\d+):.*latency=([\d.]+)s')


@lru_cache(maxsize=128)
def _read(path, mtime_ns, started_at, ended_at):
    latest=None;last_tool=None;tools=[]
    stats={'model_calls':0,'model_seconds':0.0,'output_tokens':0,'tool_calls':0,'tool_seconds':0.0,'windowed':False}
    with open(path,'rb') as stream:
        stream.seek(0,2)
        size=stream.tell();stream.seek(max(0,size-524288));stats['windowed']=size>524288
        lines=stream.read().decode('utf-8',errors='replace').splitlines()
    for line in lines:
        try:at=datetime.fromisoformat(line[:23]).timestamp()
        except ValueError:continue
        # Native run timestamps are integer seconds; include its final second
        # so the successful completion tool and final API response are counted.
        if at < (started_at or 0) or ended_at and at>=ended_at+1:continue
        tool=_TOOL.search(line)
        api=_API.search(line)
        if tool:
            latest={'phase':'tool_completed','tool':tool.group(1),'duration':float(tool.group(2)),
                    'text':'工具 '+tool.group(1)+' 已完成','at':at}
            last_tool=latest.copy();tools.append(last_tool)
            stats['tool_calls']+=1;stats['tool_seconds']+=latest['duration']
        elif api:
            stats['model_calls']+=1;stats['model_seconds']+=float(api.group(2))
            usage=re.search(r'\bin=\d+ out=(\d+)',line)
            if usage:stats['output_tokens']+=int(usage.group(1))
            latest={'phase':'model_completed','call':int(api.group(1)),'duration':float(api.group(2)),
                    'text':'第 '+api.group(1)+' 次模型调用已完成','at':at}
        elif 'run_agent: OpenAI client created (' in line and ('_request,' in line or '_request)' in line):
            latest={'phase':'model_request','text':'模型请求已发起','at':at}
        elif 'agent.chat_completion_helpers: Stream stale for ' in line:
            latest={'phase':'model_stale','text':'模型流长时间无数据，原生连接恢复处理中','at':at}
    stats['model_seconds']=round(stats['model_seconds'],1);stats['tool_seconds']=round(stats['tool_seconds'],1)
    return {**latest,'last_tool':last_tool,'recent_tools':tools[-6:],'stats':stats} if latest else None


def snapshot(run):
    if run is None or not run.profile:return None
    from hermes_cli.profiles import get_profile_dir
    path=get_profile_dir(run.profile)/'logs'/'agent.log'
    try:return _read(str(path),path.stat().st_mtime_ns,run.started_at,run.ended_at)
    except (OSError,ValueError):return None


def describe(worker, run, notes):
    if worker.status=='todo':return '等待前置任务完成，尚未启动','native_state',None
    if worker.status=='ready':return '等待原生调度器领取','native_state',None
    if worker.status=='running':
        pending=pending_tools(run)
        if pending:return pending['text'],'native_messages',pending['at']
        if not worker.worker_pid:return '任务已领取，正在启动 Worker','native_state',None
    operation=snapshot(run)
    if operation:
        prefix='最后记录：' if worker.status!='running' else '原生记录：'
        text=operation['text']
        if worker.status=='running' and operation['phase']=='model_request':text='等待模型响应'
        if operation['last_tool'] and operation['phase']!='tool_completed':
            text+='；上个动作：'+operation['last_tool']['text']
        return prefix+text,'native_log',operation['at']
    if notes:
        note=notes[-1]
        prefix='最后汇报：' if worker.status!='running' else ''
        return prefix+note.payload['note'],'worker_note',note.created_at
    if worker.status=='running':return '运行中，尚无具体操作记录','native_state',None
    if worker.status=='blocked':return '已阻塞，当前没有运行中的 Worker','native_state',None
    if worker.status=='done':return '任务已交付','native_state',None
    if worker.status=='archived':return '任务已归档','native_state',None
    return '等待原生任务状态更新','native_state',None


def failure_reason(worker, run):
    if run and run.outcome=='timed_out':
        from .native_clock import timing
        label='运行上限（已扣除系统休眠）' if timing(run)['sleep_aware'] else '墙钟运行上限'
        return f"原生 run #{run.id} 超过 {run.max_runtime_seconds} 秒{label}，未完成交付。"
    if worker.status=='blocked' and run:
        return f"原生 run #{run.id} 已结束：{run.outcome or run.status}；需要处理阻塞。"
    return ''
