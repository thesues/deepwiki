"""Research time plans and extension decisions on native Kanban records only."""
import json
import os
import time

from hermes_cli import kanban_db as kb, kanban_swarm as swarm
from .native_clock import elapsed

AUTHOR = 'research-coordinator'
REQUEST_PREFIX = 'extension:'
DECISION_PREFIX = 'extension-decision:'


def run_ceiling():
    value = int(os.environ.get('COMPLEX_MAX_RUN_SECONDS', '86400'))
    if not 30 <= value <= 86400:
        raise ValueError('COMPLEX_MAX_RUN_SECONDS must be 30..86400')
    return value


def time_plan(value, initial_seconds, *, required=False):
    if value is None and not required:
        return None  # Legacy/Kanban groups are not silently granted extensions.
    if not isinstance(value, dict) or set(value) != {'assessment', 'max_run_seconds'}:
        raise ValueError('Swarm time_plan requires assessment and max_run_seconds')
    assessment = value['assessment']
    maximum = value['max_run_seconds']
    if not isinstance(assessment, str) or not assessment.strip() or len(assessment) > 2000:
        raise ValueError('time_plan.assessment must explain difficulty, work and time estimate')
    if type(maximum) is not int or not initial_seconds <= maximum <= run_ceiling():
        raise ValueError('time_plan.max_run_seconds must cover the initial run and respect the configured ceiling')
    return {'assessment': assessment.strip(), 'max_run_seconds': maximum}


def _payload(comment):
    if not comment.body.startswith(swarm.BLACKBOARD_PREFIX):
        return {}
    try:
        value = json.loads(comment.body[len(swarm.BLACKBOARD_PREFIX):])
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def announced_plan(conn, root):
    """The first coordinator-authored topology fixes the advertised ceiling.

    A Worker comment or a later topology refresh cannot enlarge that contract.
    """
    for comment in kb.list_comments(conn, root):
        payload = _payload(comment)
        if comment.author == AUTHOR and payload.get('key') == 'topology' and isinstance(payload.get('value'), dict):
            return payload['value'].get('time_plan')
    return None


def requests(conn, root, topology, authors):
    """First authenticated request/decision wins; budget comments are not evidence."""
    pending = {}; decisions = {}
    for comment in kb.list_comments(conn, root):
        payload = _payload(comment); key = payload.get('key'); value = payload.get('value')
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        if comment.author == AUTHOR and key.startswith(DECISION_PREFIX):
            target = value.get('request_comment_id')
            if type(target) is int and key == DECISION_PREFIX + str(target) and value.get('status') in {'APPROVED', 'REJECTED'}:
                decisions.setdefault(target, {**value, 'comment_id': comment.id, 'created_at': comment.created_at})
            continue
        author = authors.get(comment.author)
        if not author or not key.startswith(REQUEST_PREFIX) or key in pending:
            continue
        tid = value.get('task_id'); rid = value.get('run_id'); extra = value.get('extra_seconds')
        if tid != author['task_id'] or type(rid) is not int or type(extra) is not int or not 1 <= extra <= 86400:
            continue
        if root not in kb.parent_ids(conn, tid):
            continue  # Topology comments cannot borrow another group's native task.
        if any(not isinstance(value.get(k), str) or not value[k].strip() or len(value[k]) > 1000 for k in ['reason', 'plan']):
            continue
        source = conn.execute('SELECT task_id, profile FROM task_runs WHERE id=?', (rid,)).fetchone()
        if not source or source['task_id'] != tid or source['profile'] != comment.author:
            continue
        pending[key] = {'key': key, 'comment_id': comment.id, 'created_at': comment.created_at,
                        'task_id': tid, 'run_id': rid, 'author_label': author['author_label'],
                        'extra_seconds': extra, 'reason': value['reason'].strip(), 'plan': value['plan'].strip()}
    rows = []
    policy = announced_plan(conn, root) or {}
    maximum = policy.get('max_run_seconds')
    maximum = min(maximum, run_ceiling()) if type(maximum) is int else None
    for row in pending.values():
        task = kb.get_task(conn, row['task_id']); run = kb.latest_run(conn, row['task_id'])
        current = bool(task and run and task.status == 'running' and task.current_run_id == row['run_id']
                       and run.id == row['run_id'] and run.ended_at is None)
        limit = task.max_runtime_seconds if task else None
        remaining = max(0, int(limit - elapsed(run))) if current and limit is not None else 0
        decision = decisions.get(row['comment_id'])
        status = decision['status'] if decision else 'UNAVAILABLE' if maximum is None else 'PENDING' if current and remaining > 0 else 'STALE'
        rows.append({**row, 'status': status, 'can_approve': status == 'PENDING' and limit is not None and limit < maximum,
                     'limit_seconds': limit, 'remaining_seconds': remaining,
                     'max_run_seconds': maximum, 'decision': decision})
    return rows


def _decision(conn, root, request, value):
    """Inside the limit-update transaction; same native comment/event schema."""
    body = swarm.BLACKBOARD_PREFIX + json.dumps({'key': DECISION_PREFIX + str(request['comment_id']), 'value': value}, ensure_ascii=False)
    now = int(time.time())
    cur = conn.execute('INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)',
                       (root, AUTHOR, body, now))
    kb._append_event(conn, root, 'commented', {'author': AUTHOR, 'len': len(body)})
    return {**value, 'comment_id': int(cur.lastrowid), 'created_at': now}


def register_worker_request(task_id):
    """A typed convenience tool on native comments, bound to this CLI Worker."""
    from tools.registry import registry
    from tools.kanban_tools import _connect
    import secrets
    worker_run_id = os.environ.get('HERMES_KANBAN_RUN_ID')

    def request(args, **_):
        conn = None
        try:
            extra = args.get('extra_seconds')
            if type(extra) is not int or not 1 <= extra <= 86400:
                raise ValueError('extra_seconds must be positive bounded seconds')
            if any(not isinstance(args.get(k), str) or not args[k].strip() or len(args[k]) > 1000 for k in ['reason', 'plan']):
                raise ValueError('Explain actual need and a bounded remaining plan')
            _, conn = _connect()
            with kb.write_txn(conn):
                task = kb.get_task(conn, task_id); run = kb.latest_run(conn, task_id)
                if not task or not run or task.status != 'running' or task.current_run_id != run.id or run.ended_at is not None:
                    raise ValueError('Only your current live run may request time')
                if worker_run_id is not None and int(worker_run_id) != run.id:
                    raise ValueError('This Worker belongs to an older run; it cannot request time for a replacement run')
                if task.max_runtime_seconds is None or elapsed(run) >= task.max_runtime_seconds:
                    raise ValueError('Run already expired; requesting cannot revive it')
                root = next((p for p in kb.parent_ids(conn, task_id) if announced_plan(conn, p)), None)
                if root is None:
                    raise ValueError('This task group has no announced extension policy; no automatic budget increase is authorized')
                policy = announced_plan(conn, root)
                author = run.profile
                authors = {author: {'task_id': task_id, 'author_label': author}}
                existing = next((r for r in requests(conn, root, {}, authors) if r['run_id'] == run.id and r['status'] == 'PENDING'), None)
                if existing:
                    return json.dumps({'ok': True, 'already_pending': True, 'root_id': root, 'comment_id': existing['comment_id'],
                                       'run_id': run.id, 'extra_seconds': existing['extra_seconds'], 'max_run_seconds': existing['max_run_seconds'],
                                       'notice': 'An existing request is pending; no additional request or time grant was created.'})
                if min(policy['max_run_seconds'], run_ceiling()) <= task.max_runtime_seconds:
                    raise ValueError('Announced run ceiling is already exhausted; no further extension is authorized')
                value = {'task_id': task_id, 'run_id': run.id, 'extra_seconds': extra,
                         'reason': args['reason'].strip(), 'plan': args['plan'].strip()}
                body = swarm.BLACKBOARD_PREFIX + json.dumps({'key': REQUEST_PREFIX + secrets.token_hex(12), 'value': value}, ensure_ascii=False)
                cur = conn.execute('INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)',
                                   (root, author, body, int(time.time())))
                kb._append_event(conn, root, 'commented', {'author': author, 'len': len(body)})
                return json.dumps({'ok': True, 'root_id': root, 'comment_id': int(cur.lastrowid), 'run_id': run.id,
                                   'extra_seconds': extra, 'max_run_seconds': min(policy['max_run_seconds'], run_ceiling()),
                                   'notice': 'Request posted, not yet approved. Current run deadline is unchanged.'})
        except (ValueError, LookupError, PermissionError, RuntimeError) as exc:
            return json.dumps({'ok': False, 'error': str(exc)})
        finally:
            if conn is not None:conn.close()

    registry.register(name='research_request_extension', toolset='kanban', handler=request,
        schema={'name': 'research_request_extension',
                'description': 'Request extra research time before your current run expires. Your native task/run/root are bound automatically. Posts a native root comment; does not grant time, reset counters or resume a stopped run. Existing pending requests are reused.',
                'parameters': {'type': 'object', 'properties': {'extra_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 86400},
                    'reason': {'type': 'string', 'maxLength': 1000, 'description': 'Public operation/time evidence or published stage references only; no private hypotheses, reasoning or unpublished direction.'},
                    'plan': {'type': 'string', 'maxLength': 1000, 'description': 'Remaining acceptance scope, operation categories and estimated work; do not reveal an unfinished private strategy.'}},
                    'required': ['extra_seconds', 'reason', 'plan']}})


def resolve(conn, root, topology, authors, request_comment_id, *, approve, reason, grant_seconds=None):
    if type(request_comment_id) is not int or type(approve) is not bool:
        raise ValueError('request_comment_id and approve must be explicit')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError('Explain the extension decision in reason')
    with kb.write_txn(conn):
        # Re-read after BEGIN IMMEDIATE: duplicate approvals and native timeout
        # transitions serialize against this decision, without a new scheduler.
        row = next((r for r in requests(conn, root, topology, authors) if r['comment_id'] == request_comment_id), None)
        if row is None:
            raise LookupError('Authenticated extension request not found')
        if row['decision']:
            return row['decision']  # Durable idempotence; do not grant twice.
        result = {'request_comment_id': request_comment_id, 'task_id': row['task_id'],
                  'run_id': row['run_id'], 'status': 'REJECTED', 'reason': reason.strip(), 'granted_seconds': 0}
        if approve:
            if not row['can_approve']:
                result['reason'] = 'Not extended: request is stale, run has stopped/expired, no announced extension policy, or the run ceiling is exhausted. ' + reason.strip()
            else:
                grant = row['extra_seconds'] if grant_seconds is None else grant_seconds
                if type(grant) is not int or not 1 <= grant <= row['extra_seconds']:
                    raise ValueError('grant_seconds must be 1..requested extra_seconds')
                limit = row['limit_seconds']; updated = limit + grant
                if updated > row['max_run_seconds']:
                    raise ValueError('Requested grant exceeds the announced run ceiling; explicitly grant a smaller amount or reject')
                task_update = conn.execute("UPDATE tasks SET max_runtime_seconds=? WHERE id=? AND status='running' AND current_run_id=? AND max_runtime_seconds=?",
                                           (updated, row['task_id'], row['run_id'], limit))
                run_update = conn.execute('UPDATE task_runs SET max_runtime_seconds=? WHERE id=? AND task_id=? AND ended_at IS NULL',
                                          (updated, row['run_id'], row['task_id']))
                if task_update.rowcount != 1 or run_update.rowcount != 1:
                    raise RuntimeError('Native task/run changed during extension decision')
                result.update(status='APPROVED', granted_seconds=grant, previous_limit_seconds=limit, max_runtime_seconds=updated)
                kb._append_event(conn, row['task_id'], 'runtime_extended', result, run_id=row['run_id'])
        return _decision(conn, root, row, result)
