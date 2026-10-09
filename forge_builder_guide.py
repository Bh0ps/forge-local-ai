"""Chat-scoped brainstorming, retained across turns and coordinator restarts."""
from storage import _now

GUIDANCE = """Builder conversation: help the user discover a project worth making.
Start from their idea, or suggest three specific, varied ideas if they have none.
Ask at most two short questions per reply. Offer concrete choices and your recommendation;
free-text answers are welcome. Develop the audience, problem, primary flows, pages,
visual direction, constraints and acceptance checks gradually. Remember answered questions
and revisions; do not repeat the whole questionnaire or demand a complete specification.
Use ordinary conversation for these questions. Inspect project tools only when relevant.
You are read-only during this conversation. Do not implement, start servers or delegate a
writer. When enough is known, provide a concise proposed brief, including acceptance checks,
and invite refinements. Explain that /build accepts the current brief and starts a tracked
implementation in a connected project; /builder off returns to normal chat. Never claim
that an idea, draft or final answer has already been built or verified.
"""


def prepare(store, data):
    """Compile the guide into every foreground conversational turn, never children."""
    if data.get('parent_id') or data.get('goal_id') or data.get('mode', 'chat') not in ('chat', 'builder'):
        return data
    guide = None
    if data.get('chat_id'):
        try:
            guide = store.entity('builder_guides', data['chat_id'])
        except ValueError:
            pass
    if not data.get('builder_guided') and not (guide or {}).get('active'):
        return data
    return {**data, 'builder_guided': True, 'mode': 'builder', 'readonly': True,
            'auto_delegate': False, 'instructions': (data.get('instructions', '') + '\n' + GUIDANCE).strip()}


def remember(store, run):
    if run.get('builder_guided'):
        try:
            previous = store.entity('builder_guides', run['chat_id'])
        except ValueError:
            previous = {}
        store.save_entity('builder_guides', {'id': run['chat_id'], 'version': 1,
                          'active': True, 'run_id': run['id'],
                          'first_request_id': previous.get('first_request_id', run['request_message_id']) if previous.get('active') else run['request_message_id'],
                          'updated_at': _now()})
    elif run.get('goal_id') and not run.get('parent_id'):
        disable(store, run['chat_id'])


def disable(store, chat_id):
    if not chat_id:
        raise ValueError('Select a chat first.')
    store.get_chat(chat_id, limit=1)
    store.save_entity('builder_guides', {'id': chat_id, 'version': 1, 'active': False, 'updated_at': _now()})


def accepted_brief(store, chat_id):
    if not chat_id:
        raise ValueError('Use /builder to develop an idea first.')
    guide = store.entity('builder_guides', chat_id)
    if not guide.get('active'):
        raise ValueError('Use /builder to develop an idea first.')
    run = store.run(guide['run_id'])
    if run['status'] != 'completed':
        raise ValueError('Wait for the Builder reply before accepting it with /build.')
    chat = store.get_chat(chat_id, limit=200)
    first = guide.get('first_request_id', run['request_message_id'])
    if not chat['messages'] or chat['messages'][0]['id'] > first:
        raise ValueError('The complete Builder discussion is no longer in this page. Start a new /builder chat with your agreed brief before accepting it.')
    replies = [message['content'] for message in chat['messages']
               if message['role'] == 'assistant' and message.get('interaction_run_id') == run['id']
               and not message.get('tool_calls') and message.get('content')]
    if not replies:
        raise ValueError('No completed Builder brief is available. Ask Builder for a proposed brief.')
    conversation = '\n\n'.join(message['role'] + ': ' + message['content'] for message in chat['messages']
                                if message['id'] >= first and message['role'] in ('user', 'assistant') and message.get('content'))
    if len(conversation) > 64000:
        raise ValueError('This Builder discussion is too long to accept safely. Start a new /builder chat with your agreed brief.')
    return 'Accepted Builder proposal:\n' + replies[-1] + '\n\nRequirements and revisions from the discussion:\n' + conversation
