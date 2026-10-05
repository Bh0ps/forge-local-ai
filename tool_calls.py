"""Assemble bounded streamed tool calls before the runtime executes any action."""
import copy
import json
import re


MAX_CALL_CHARACTERS = 60000


def _json_size(value):
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
    except (TypeError, ValueError, RecursionError):
        raise ValueError('Tool arguments must contain valid JSON.') from None


def _object(text):
    def invalid_constant(_):
        raise ValueError('Non-finite JSON value')

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON argument key')
            result[key] = value
        return result

    try:
        value = json.loads(text, parse_constant=invalid_constant, object_pairs_hook=unique_keys)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _merge_objects(current, incoming):
    result = copy.deepcopy(current)
    for key, value in incoming.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
        elif isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_objects(result[key], value)
        elif result[key] != value:
            raise ValueError('Conflicting streamed tool arguments.')
    return result


class ToolCallAccumulator:
    """Accumulate deltas or complete calls; ``finish`` returns detached calls.

    Explicit indexes/IDs identify parallel fragments. Without them, complete
    calls are independent and only one unfinished fragmented call is allowed.
    Repeated complete calls are deduplicated to avoid replaying an action.
    """

    def __init__(self, max_calls=8):
        if type(max_calls) is not int or not 1 <= max_calls <= 32:
            raise ValueError('Invalid tool call limit.')
        self.max_calls = max_calls
        self._states = []
        self._by_index = {}
        self._by_id = {}

    @staticmethod
    def _complete(state):
        return bool(state['name']) and (isinstance(state['arguments'], dict)
                                        or (isinstance(state['arguments'], str) and _object(state['arguments']) is not None))

    def _locate(self, index, call_id, function):
        indexed = self._by_index.get(index) if index is not None else None
        identified = self._by_id.get(call_id) if call_id is not None else None
        if indexed is not None and identified is not None and indexed is not identified:
            raise ValueError('Conflicting streamed tool identifiers.')
        state = indexed if indexed is not None else identified
        if state is not None:
            return state
        if index is not None or call_id is not None:
            return None

        # A finished unindexed call may be emitted again in the done packet.
        arguments = function.get('arguments')
        complete_arguments = arguments if isinstance(arguments, dict) else _object(arguments) if isinstance(arguments, str) else None
        if complete_arguments is not None and function.get('name'):
            for existing in self._states:
                existing_args = existing['arguments']
                existing_args = _object(existing_args) if isinstance(existing_args, str) else existing_args
                if existing['name'] == function['name'] and existing_args == complete_arguments:
                    return existing

        pending = [existing for existing in self._states if not self._complete(existing)]
        if len(pending) > 1:
            raise ValueError('Parallel tool fragments require an index or id.')
        if pending:
            return pending[0]
        if arguments is None and self._states and not function.get('name'):
            raise ValueError('Tool fragment has no identifier.')
        return None

    def add(self, chunks):
        if not isinstance(chunks, list) or len(chunks) > self.max_calls:
            raise ValueError('Too many or invalid tool calls.')
        for chunk in chunks:
            if not isinstance(chunk, dict) or not isinstance(chunk.get('function'), dict):
                raise ValueError('Invalid streamed tool call.')
            if chunk.get('type', 'function') != 'function':
                raise ValueError('Invalid streamed tool type.')
            function = chunk['function']
            function_index, outer_index = function.get('index'), chunk.get('index')
            if function_index is not None and outer_index is not None and function_index != outer_index:
                raise ValueError('Conflicting streamed tool indexes.')
            index = function_index if function_index is not None else outer_index
            if index is not None and (type(index) is not int or not 0 <= index < 32):
                raise ValueError('Invalid streamed tool index.')
            call_id = chunk.get('id')
            if call_id is not None and (not isinstance(call_id, str) or not call_id or len(call_id) > 256):
                raise ValueError('Invalid streamed tool id.')
            name = function.get('name', '')
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{0,128}', name):
                raise ValueError('Invalid streamed tool name.')
            incoming = function.get('arguments')
            if incoming is not None and not isinstance(incoming, (dict, str)):
                raise ValueError('Invalid streamed tool arguments.')
            if _json_size(incoming) > MAX_CALL_CHARACTERS:
                raise ValueError('Tool arguments are too large.')

            state = self._locate(index, call_id, function)
            if state is None:
                if len(self._states) >= self.max_calls:
                    raise ValueError('Too many tool calls in one response.')
                state = {'name': '', 'arguments': None, 'index': index, 'id': call_id}
                self._states.append(state)
            if index is not None:
                if state['index'] is not None and state['index'] != index:
                    raise ValueError('Conflicting streamed tool indexes.')
                state['index'] = index
                self._by_index[index] = state
            if call_id is not None:
                if state['id'] is not None and state['id'] != call_id:
                    raise ValueError('Conflicting streamed tool ids.')
                state['id'] = call_id
                self._by_id[call_id] = state

            if name and name != state['name']:
                # Some providers repeat the full name, others send name deltas.
                state['name'] = name if name.startswith(state['name']) else state['name'] + name
                if len(state['name']) > 128:
                    raise ValueError('Streamed tool name is too large.')
            if incoming is not None:
                current = state['arguments']
                if current is None:
                    state['arguments'] = copy.deepcopy(incoming)
                elif isinstance(current, dict) and isinstance(incoming, dict):
                    state['arguments'] = _merge_objects(current, incoming)
                elif isinstance(current, str) and isinstance(incoming, str):
                    incoming_object = _object(incoming)
                    if incoming_object is not None and (incoming == current or incoming.startswith(current)):
                        state['arguments'] = incoming
                    else:
                        state['arguments'] += incoming
                elif isinstance(current, dict) and isinstance(incoming, str):
                    if not current:
                        state['arguments'] = incoming
                    elif _object(incoming) != current:
                        raise ValueError('Conflicting streamed tool argument formats.')
                else:
                    parsed = _object(current)
                    if parsed is None:
                        raise ValueError('Incomplete streamed JSON before object arguments.')
                    state['arguments'] = _merge_objects(parsed, incoming)
            if sum(_json_size(existing) for existing in self._states) > MAX_CALL_CHARACTERS:
                raise ValueError('Tool arguments are too large.')

    def finish(self):
        calls = []
        for state in self._states:
            name = state['name']
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.:-]{0,127}', name):
                raise ValueError('Tool call ended without a valid name.')
            arguments = state['arguments']
            if isinstance(arguments, str):
                arguments = _object(arguments)
            if not isinstance(arguments, dict):
                raise ValueError('Tool call ended with incomplete or invalid JSON arguments.')
            _json_size(arguments)
            function = {'name': name, 'arguments': copy.deepcopy(arguments)}
            if state['index'] is not None:
                function['index'] = state['index']
            call = {'function': function}
            if state['id'] is not None:
                call['id'] = state['id']
            calls.append(call)
        return calls
