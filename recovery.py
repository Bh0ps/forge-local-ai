"""Close interrupted saved tool requests without replaying their actions."""
import json
import re
import threading


_RECOVERY_LOCK = threading.RLock()
_ERROR = (
    'The previous run was interrupted before this tool result was saved. '
    'Its execution outcome is unknown; the action may already have happened. '
    'This recovery record did not execute or retry the tool. Inspect current '
    'files, task state or process state before deciding whether a retry is safe.'
)


def _missing_calls(chat):
    """Match actual results within each block and recovery results by identity.

    A later user/assistant turn closes the preceding block for normal matching;
    its results must not accidentally satisfy an older interrupted same-name
    call. Recovery records can close an older block using a stable saved ID.
    """
    pending, active = {}, []
    boundary = chat.get('compacted_through') or 0
    for message in chat['messages']:
        if message['id'] <= boundary:
            continue
        role = message['role']
        if role == 'assistant':
            active = []
            for index, call in enumerate(message.get('tool_calls') or []):
                function = call.get('function') if isinstance(call, dict) else None
                name = function.get('name') if isinstance(function, dict) else None
                if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.:-]{0,127}', name):
                    raise ValueError('Saved tool call has an invalid name. Start a new chat to continue.')
                key = (message['id'], index)
                pending[key] = {'assistant_message_id': message['id'], 'call_index': index, 'name': name}
                active.append(key)
        elif role == 'user':
            active = []
        elif role == 'tool':
            recovery_key = None
            if message.get('status') == 'interrupted':
                try:
                    content = json.loads(message['content'])
                    marker = content.get('sidekick_recovery') if isinstance(content, dict) else None
                    if (isinstance(marker, dict) and type(marker.get('assistant_message_id')) is int
                            and type(marker.get('call_index')) is int):
                        recovery_key = (marker['assistant_message_id'], marker['call_index'])
                except (TypeError, ValueError):
                    pass
            if recovery_key is not None:
                item = pending.get(recovery_key)
                if item is not None and item['name'] == message.get('tool_name'):
                    pending.pop(recovery_key)
            else:
                matched = next((key for key in active if key in pending
                                and pending[key]['name'] == message.get('tool_name')), None)
                if matched is not None:
                    pending.pop(matched)
    return list(pending.values())


def recover_interrupted_tools(store, chat_id):
    """Append missing result records and return the freshly loaded saved chat.

    Call only when no request for this chat is running, before adding a new user
    message. Original messages and summaries are untouched. Each appended row
    has a stable reference, making repeated calls and partial-write recovery
    idempotent. Compacted history is already archived and is not reintroduced.
    """
    with _RECOVERY_LOCK:
        chat = store.get_active_chat(chat_id)
        for missing in _missing_calls(chat):
            marker = {key: missing[key] for key in ('assistant_message_id', 'call_index')}
            result = {
                'ok': False, 'error': _ERROR, 'interrupted': True,
                'execution_outcome': 'unknown', 'sidekick_recovery': marker,
            }
            store.add_message(chat_id, 'tool', json.dumps(result, ensure_ascii=False),
                              tool_name=missing['name'], status='interrupted')
        return store.get_active_chat(chat_id)
