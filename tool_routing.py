"""Task-aware schema selection and complete-call validation without execution."""
import json
import re
from functools import lru_cache

CORE = {'request_user_input', 'tools_search', 'tools_load', 'goal_read', 'goal_update', 'goal_task_update', 'artifact_read',
        'delegate_agent', 'agent_result', 'list_files', 'read_file', 'search_files', 'read_files'}


def relevance(schema, query, loaded=()):
    name = schema['function']['name']
    if name in loaded:
        return 1000
    score = 4 if name in CORE else 0
    terms = set(re.findall(r'[a-z][a-z0-9_]{2,}', str(query).casefold()))
    haystack = schema['function'].get('description','').casefold() + ' ' + name.replace('_', ' ')
    score += sum(3 for term in terms if re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', haystack))
    if any(term in terms for term in ('build', 'implement', 'fix', 'code', 'app', 'website','write','create','edit','patch')) and name in ('edit_file', 'write_file', 'apply_patch', 'run_command', 'command_start'):
        score += 20
    if terms & {'test','tests','pytest','verify','verification','check','checks'} and name in ('run_command','command_start','command_read','command_wait'):
        score += 20
    if any(term in terms for term in ('browser', 'preview', 'ui', 'website', 'page')) and name.startswith(('browser_', 'preview_')):
        score += 20
    return score


def select_schemas(schemas, query, budget_characters, loaded=(), limit=64, phase=None):
    selected = []
    coordination=['tools_search','tools_load','request_user_input','goal_read','goal_update','artifact_read']
    loaded=list(loaded)
    terms=set(re.findall(r'[a-z][a-z0-9_]{2,}',str(query).casefold()))
    actions=[]
    if terms & {'write','create','build','implement','app','website'}: actions.append('write_file')
    if terms & {'edit','fix','patch','repair'}: actions.extend(('edit_file','apply_patch'))
    if terms & {'test','tests','pytest','verify','verification','check','checks'}: actions.extend(('command_start','run_command','command_wait','command_read'))
    def rank(schema):
        name=schema['function']['name']
        if name in coordination: return (0,coordination.index(name))
        if name in loaded: return (1,loaded.index(name))
        if name=='read_file': return (2,0)
        if name in actions: return (2,1+actions.index(name))
        if name in ('delegate_agent','agent_result','artifact_read'): return (2,len(actions)+1)
        phase_score=0
        if phase=='inspect' and name in ('list_files','read_file','read_files','search_files'): phase_score=35
        if phase in ('implement','repair') and name in ('edit_file','write_file','apply_patch'): phase_score=40
        if phase=='verify' and (schema.get('verification_contract') or name in ('command_start','command_read','command_wait','run_command') or name.startswith(('preview_','build_check'))): phase_score=45
        return (3,-relevance(schema,query,loaded)-phase_score)
    for schema in sorted(schemas, key=rank):
        if len(selected) >= limit:
            break
        if len(json.dumps(wire_schemas(selected + [schema]), ensure_ascii=False, separators=(',', ':')).encode('utf-8')) <= budget_characters:
            selected.append(schema)
    return selected


def wire_schemas(schemas):
    """Private coordinator contract metadata never enters provider schemas."""
    return [{'type':schema.get('type','function'),'function':schema['function']} for schema in schemas]


@lru_cache(maxsize=128)
def _validator(serialized):
    from jsonschema import Draft202012Validator
    return Draft202012Validator(json.loads(serialized))


def catalog_search(schemas, query='', limit=8):
    if not isinstance(query, str) or len(query) > 2000 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Tool query must be text and limit 1–20.')
    normalized=str(query).strip().casefold()
    terms=set(re.findall(r'[a-z][a-z0-9_]{2,}',normalized.replace('_',' ')))
    aliases={'write_file':{'write','replace','file','text','content'},
             'edit_file':{'edit','replace','file','text','match','hash'},
             'apply_patch':{'patch','replace','file','files','text','hash','checked','atomic','edit','changes'}}
    def score(schema):
        name=schema['function']['name'].casefold()
        if not normalized: return relevance(schema,'')
        exact=10000 if normalized==name else 1000 if name in re.findall(r'[a-z][a-z0-9_:.\-]*',normalized) else 0
        name_terms=set(re.findall(r'[a-z][a-z0-9]{2,}',name.replace('_',' ')))
        description=schema['function'].get('description','').casefold()
        return exact+10*len(terms&(name_terms|aliases.get(name,set())))+sum(3 for term in terms if re.search(r'(?<!\w)'+re.escape(term)+r'(?!\w)',description))
    ranked = sorted(schemas, key=lambda s: (-score(s), s['function']['name']))
    return [{'name': s['function']['name'], 'description': s['function'].get('description','')[:350],
             'capability': s.get('capability', 'coordinator-reviewed')} for s in ranked[:limit]]


def _fallback(value, schema, path='$', depth=0):
    if depth > 30:
        raise ValueError('Schema nesting exceeds validation limit.')
    if '$ref' in schema:
        raise ValueError('This tool requires the jsonschema validator for reference resolution.')
    choices = schema.get('anyOf') or schema.get('oneOf')
    if choices:
        matches = 0
        for choice in choices:
            try:
                _fallback(value, choice, path, depth + 1)
                matches += 1
            except ValueError:
                pass
        if matches < 1 or ('oneOf' in schema and matches != 1):
            raise ValueError(path + ' does not match its allowed schema.')
    expected = schema.get('type')
    types = expected if isinstance(expected, list) else [expected] if expected else []
    predicates = {'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list),
        'string': lambda v: isinstance(v, str), 'integer': lambda v: type(v) is int,
        'number': lambda v: type(v) in (int, float), 'boolean': lambda v: type(v) is bool,
        'null': lambda v: v is None}
    if types and not any(predicates.get(t, lambda v: False)(value) for t in types):
        raise ValueError(path + ' must be ' + '/'.join(types) + '.')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(path + ' has an unsupported value.')
    if 'const' in schema and value != schema['const']:
        raise ValueError(path + ' has an unsupported value.')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        missing = set(schema.get('required', [])) - set(value)
        if missing:
            raise ValueError(path + ' missing: ' + ', '.join(sorted(missing)))
        if schema.get('additionalProperties') is False and set(value) - set(properties):
            raise ValueError(path + ' unknown fields: ' + ', '.join(sorted(set(value)-set(properties))))
        for key, child in value.items():
            spec = properties.get(key, schema.get('additionalProperties', {}))
            if isinstance(spec, dict):
                _fallback(child, spec, path + '.' + key, depth + 1)
    elif isinstance(value, list):
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', 1000000):
            raise ValueError(path + ' has an invalid item count.')
        for index, child in enumerate(value):
            if isinstance(schema.get('items'), dict):
                _fallback(child, schema['items'], path + '[' + str(index) + ']', depth + 1)
    elif isinstance(value, str):
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', 1000000):
            raise ValueError(path + ' has an invalid length.')
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            raise ValueError(path + ' does not match the required format.')
    elif type(value) in (int, float):
        if value < schema.get('minimum', float('-inf')) or value > schema.get('maximum', float('inf')):
            raise ValueError(path + ' is outside its allowed range.')


def validate_arguments(schema, arguments):
    """Return a bounded validation issue; no coercion, defaults, or side effects."""
    if not isinstance(arguments, dict):
        return 'Arguments must be an object.'
    try:
        json.dumps(arguments, allow_nan=False)
        def references(value):
            if isinstance(value,dict):
                if '$ref' in value and not str(value['$ref']).startswith('#'):
                    raise ValueError('External schema references are not fetched during tool execution.')
                for child in value.values(): references(child)
            elif isinstance(value,list):
                for child in value: references(child)
        references(schema['function']['parameters'])
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            _fallback(arguments, schema['function']['parameters'])
        else:
            issue = next(_validator(json.dumps(schema['function']['parameters'],sort_keys=True,separators=(',',':'))).iter_errors(arguments), None)
            if issue:
                # Never echo arbitrary tool argument content (which may contain secrets).
                path = '$' + ''.join('.' + str(p) for p in issue.absolute_path)
                return (path + ': invalid ' + str(issue.validator) + ' constraint.')[:700]
        return None
    except (ValueError, TypeError, RecursionError) as exc:
        return str(exc)[:700]
    except Exception as exc:
        return 'Schema could not be validated ('+type(exc).__name__+').'
