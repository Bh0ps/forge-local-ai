import copy

import pytest

from tool_calls import ToolCallAccumulator


def call(name=None, arguments=None, index=None, call_id=None):
    function = {}
    if name is not None:
        function['name'] = name
    if arguments is not None:
        function['arguments'] = arguments
    if index is not None:
        function['index'] = index
    result = {'function': function}
    if call_id is not None:
        result['id'] = call_id
    return result


def test_interleaved_indexed_fragments_merge_names_and_arguments():
    accumulator = ToolCallAccumulator()
    accumulator.add([call('read_', '{"path":"', 0), call('list_', '{"path":', 1)])
    accumulator.add([call('directory', '"src"}', 1), call('file', 'src/app.py"}', 0)])
    assert accumulator.finish() == [
        call('read_file', {'path': 'src/app.py'}, 0),
        call('list_directory', {'path': 'src'}, 1),
    ]


def test_unindexed_complete_calls_stay_separate_and_duplicates_are_not_replayed():
    chunks = [call('read_file', {'path': 'one'}), call('read_file', {'path': 'two'})]
    original = copy.deepcopy(chunks)
    accumulator = ToolCallAccumulator()
    accumulator.add(chunks)
    accumulator.add(chunks)
    calls = accumulator.finish()
    assert calls == chunks
    calls[0]['function']['arguments']['path'] = 'changed'
    assert accumulator.finish() == original
    assert chunks == original


def test_unindexed_single_fragmented_call_and_repeated_full_name():
    accumulator = ToolCallAccumulator()
    accumulator.add([call('write_file', '{"content":"')])
    accumulator.add([call('write_file', 'a')])
    accumulator.add([call(arguments='a')])
    accumulator.add([call(arguments='"}')])
    assert accumulator.finish() == [call('write_file', {'content': 'aa'})]


def test_id_calls_and_outer_index_are_merged():
    accumulator = ToolCallAccumulator()
    accumulator.add([call('read_file', '{"path":', call_id='abc')])
    final = call(arguments='"test"}', call_id='abc')
    final['index'] = 0
    accumulator.add([final])
    assert accumulator.finish() == [call('read_file', {'path': 'test'}, 0, 'abc')]


def test_dict_fragments_merge_disjoint_keys_and_do_not_mutate_input():
    accumulator = ToolCallAccumulator()
    first = call('write_file', {'path': 'test', 'options': {'mode': 'safe'}}, 0)
    accumulator.add([first])
    accumulator.add([call('write_file', {'content': 'hello', 'options': {'encoding': 'utf8'}}, 0)])
    assert accumulator.finish()[0]['function']['arguments'] == {
        'path': 'test', 'content': 'hello', 'options': {'mode': 'safe', 'encoding': 'utf8'},
    }
    assert first['function']['arguments'] == {'path': 'test', 'options': {'mode': 'safe'}}


def test_cumulative_complete_arguments_and_replayed_done_call():
    accumulator = ToolCallAccumulator()
    accumulator.add([call('read_file', '{"path":', 0)])
    accumulator.add([call('read_file', '{"path":"test"}', 0)])
    accumulator.add([call('read_file', {'path': 'test'}, 0)])
    assert accumulator.finish() == [call('read_file', {'path': 'test'}, 0)]


@pytest.mark.parametrize('arguments', ['{"path":', '[]', 'null', '{"x":NaN}', '{"x":1,"x":2}'])
def test_incomplete_or_non_object_arguments_are_never_executable(arguments):
    accumulator = ToolCallAccumulator()
    accumulator.add([call('read_file', arguments, 0)])
    with pytest.raises(ValueError, match='incomplete or invalid'):
        accumulator.finish()


def test_conflicting_dictionary_values_fail_before_execution():
    accumulator = ToolCallAccumulator()
    accumulator.add([call('write_file', {'path': 'one'}, 0)])
    with pytest.raises(ValueError, match='Conflicting'):
        accumulator.add([call('write_file', {'path': 'two'}, 0)])


def test_conflicting_identifiers_fail_before_execution():
    accumulator = ToolCallAccumulator()
    accumulator.add([call('read_file', {}, 0, 'first'), call('read_file', {}, 1, 'second')])
    with pytest.raises(ValueError, match='identifiers'):
        accumulator.add([call('read_file', {}, 0, 'second')])


def test_call_count_and_total_argument_size_are_bounded():
    accumulator = ToolCallAccumulator()
    for index in range(8):
        accumulator.add([call('read_file', {'path': str(index)}, index)])
    with pytest.raises(ValueError, match='Too many'):
        accumulator.add([call('read_file', {'path': 'ninth'}, 8)])
    with pytest.raises(ValueError, match='too large'):
        ToolCallAccumulator().add([call('write_file', {'content': 'x' * 60000})])


def test_missing_tool_name_cannot_be_finalized():
    accumulator = ToolCallAccumulator()
    accumulator.add([call(arguments={}, index=0)])
    with pytest.raises(ValueError, match='valid name'):
        accumulator.finish()


def test_ambiguous_parallel_unindexed_fragment_is_rejected():
    accumulator = ToolCallAccumulator()
    accumulator.add([call('read_file', '{"path":', 0), call('list_directory', '{"path":', 1)])
    with pytest.raises(ValueError, match='require an index'):
        accumulator.add([call(arguments='"src"}')])


def test_empty_accumulator_has_no_calls():
    assert ToolCallAccumulator().finish() == []
