"""Synthetic local inference and tool continuation checks; no project files or tool effects."""
import re


def validate_native(runtime, model, cancel):
    request = {'model': model, 'context': 8192, 'tokens': 1024, 'temperature': 0,
               'thinking': False, 'messages': [{'role':'user','content':'Reply with the number 731 and nothing else.'}]}
    packets = list(runtime.generate(request, cancel))
    text = ''.join(p.get('message',{}).get('content','') for p in packets)
    if not packets or not packets[-1].get('done') or not re.search(r'\b731\b', text):
        raise ValueError('The native model did not pass the synthetic text check.')
    tool = {'type':'function','function':{'name':'forge_validation_sum','description':'Add the two supplied integers.',
        'parameters':{'type':'object','properties':{'a':{'type':'integer'},'b':{'type':'integer'}},
                      'required':['a','b'],'additionalProperties':False}}}
    request['tools'] = [tool]
    request['messages'] = [{'role':'user','content':'Use forge_validation_sum to add 17 and 29. You must call the tool before answering.'}]
    from tool_calls import ToolCallAccumulator
    accumulator = ToolCallAccumulator()
    content = ''
    for packet in runtime.generate(request, cancel):
        message = packet.get('message',{})
        content += message.get('content','')
        if message.get('tool_calls'):
            accumulator.add(message['tool_calls'])
    calls = accumulator.finish()
    if len(calls) != 1 or calls[0]['function']['name'] != 'forge_validation_sum':
        raise ValueError('The native model did not call the requested synthetic tool.')
    args = calls[0]['function']['arguments']
    if args != {'a':17,'b':29}:
        raise ValueError('The native model supplied incorrect synthetic tool arguments.')
    request['messages'] += [{'role':'assistant','content':content,'tool_calls':calls},
                            {'role':'tool','tool_name':'forge_validation_sum','content':'46'}]
    packets = list(runtime.generate(request,cancel))
    text = ''.join(p.get('message',{}).get('content','') for p in packets)
    if not packets or not packets[-1].get('done') or not re.search(r'\b46\b',text):
        raise ValueError('The native model did not consume the synthetic tool result.')
    return {'text':True,'tool_arguments':True,'tool_continuation':True,'synthetic':True}
