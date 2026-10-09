"""Repeatable local-only task evaluation; fixture oracles never infer live gains.

Each live task uses a separate source-import subprocess and disposable Forge
profile. Commands, browser/computer, network research and cloud tools are absent.
The identical evaluation_check tool returns trusted bounded acceptance checks.
"""
from __future__ import annotations

import ast
import builtins
import copy
import csv
import hashlib
import io
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from statistics import median

ROOT = Path(__file__).resolve().parent
SUITE = ROOT / "assets/evaluations/suite.json"
CONTRACTS = ROOT / 'assets/evaluations/contracts.json'
ALLOWED_TOOLS = {"list_files", "read_file", "read_files", "search_files", "write_file", "edit_file", "make_directory", "move_file", "restore_file",
                 "goal_read", "goal_update", "artifact_read", "skills_read", "skills_search", "skills_resource_read", "tools_search", "tools_load", "evaluation_check"}
CONTROLS = dict(context=8192, temperature=0, thinking=False, tokens=2048, max_seconds=180, rounds=24, tools=48)
TRANSPORT_SHIM_VERSION = 'ollama-thinking-false-v1'


class EvaluationIntegrityError(ValueError):
    """Invalid benchmark configuration/control; never a model task failure."""


def thinking_contract(metadata):
    """Only normalize controls the engine explicitly advertises as supported."""
    if not isinstance(metadata,dict): raise ValueError('Thinking control metadata is unavailable.')
    declared = metadata.get('thinking')
    if isinstance(declared,dict) and 'values' in declared:
        values = declared['values']
        if not isinstance(values,list) or not any(type(value) is bool and value is False for value in values):
            raise ValueError('The benchmark requires a model that permits explicit think:false.')
        return {'source':'ollama_show.thinking.values','permits_false':True,'values':values}
    if not isinstance(metadata.get('capabilities'),list) or 'thinking' not in metadata['capabilities']:
        raise ValueError('The benchmark requires verified boolean thinking control.')
    return {'source':'ollama_show.capabilities','permits_false':True,'values':[False,True]}


def normalized_controls(engine,controls):
    return {'provider':'ollama','model':engine['model'],'context':controls['context'],
            'temperature':controls['temperature'],'think':False,'output_token_ceiling':controls['tokens']}


def install_transport_shim(core,engine,controls,trace,errors,*,require_native_false=False):
    """Per-worker identical transport adapter; never modify either source tree."""
    contract=engine.get('thinking_control')
    if controls.get('thinking') is not False or not isinstance(contract,dict) or contract.get('permits_false') is not True:
        raise EvaluationIntegrityError('Verified explicit thinking-off control is required before inference.')
    original=core._stream_payload
    def stream_payload(payload,cancel_event=None):
        before=copy.deepcopy(payload); wire=copy.deepcopy(payload)
        incoming=before.get('options',{})
        record={'shim_version':TRANSPORT_SHIM_VERSION,'requested_thinking':False,
                'original_think_present':'think' in before,'original_think':before.get('think'),
                'native_controls':{'model':before.get('model'),'think_present':'think' in before,'think':before.get('think'),
                                   'context':incoming.get('num_ctx'),'temperature':incoming.get('temperature'),'output_tokens':incoming.get('num_predict')},
                'require_native_false':require_native_false,'normalizer_changed':before.get('think') is not False,'control_valid':False}
        trace.append(record)
        try:
            options=wire.get('options',{})
            if wire.get('model')!=engine['model'] or options.get('num_ctx')!=controls['context'] or options.get('temperature')!=controls['temperature']:
                raise EvaluationIntegrityError('Benchmark wire model/context/temperature changed.')
            limit=options.get('num_predict')
            if type(limit) is not int or not 1<=limit<=controls['tokens']:
                raise EvaluationIntegrityError('Benchmark wire output limit is outside the configured ceiling.')
            if require_native_false and before.get('think') is not False:
                raise EvaluationIntegrityError('Candidate native payload did not already emit explicit think:false.')
            wire['think']=False
            unchanged_before={key:value for key,value in before.items() if key!='think'}
            unchanged_after={key:value for key,value in wire.items() if key!='think'}
            if unchanged_before!=unchanged_after: raise EvaluationIntegrityError('Benchmark adapter altered unrelated payload fields.')
            record.update(wire_think=wire['think'],model=wire['model'],context=options['num_ctx'],temperature=options['temperature'],
                          output_tokens=limit,stream=wire.get('stream'),other_fields_unchanged=True,control_valid=True,
                          payload_without_think_digest=digest(unchanged_after))
        except (ValueError,TypeError,AttributeError) as exc:
            errors.append(str(exc)); raise
        yield from original(wire,cancel_event)
    core._stream_payload=stream_payload
    return core


def registered_contracts_supported(original):
    """Inspect the selected runtime, never assume the evaluator's own version."""
    function=getattr(original,'__func__',original)
    namespace=getattr(function,'__globals__',{})
    recognizer=namespace.get('verification_outcome')
    checker=getattr(namespace.get('RunManager'),'_check_verification',None)
    return (callable(namespace.get('wire_schemas')) and callable(recognizer) and
            'contract' in inspect.signature(recognizer).parameters and callable(checker) and
            'schemas' in inspect.signature(checker).parameters)


def evaluation_checker_schema(run, contract_factory=None):
    """One unchanged public checker, with optional coordinator-only metadata."""
    schema = {"type":"function", "capability":"read", "source":"evaluation", "function":{"name":"evaluation_check", "description":"Read trusted bounded acceptance checks for this task's current project files. No shell/network or model-supplied paths. Inspect failures and repair the requested behavior.", "parameters":{"type":"object", "properties":{}, "required":[], "additionalProperties":False}}}
    if contract_factory is not None:
        contract = contract_factory(run)
        if contract is not None:
            schema['verification_contract'] = contract
    return schema


def prerouting_evaluation_supported(service, original):
    """Require the selected registry to actually consume the service hook."""
    function = inspect.unwrap(getattr(original, '__func__', original))
    code = getattr(function, '__code__', None)
    return bool(callable(getattr(service, 'extra_tool_schemas', None)) and code is not None
                and 'extra_tool_schemas' in code.co_names)


def evaluation_schema_adapter(original, contract_factory=None, *, checker_registered=False):
    """Keep the same policy while adapting to the selected runtime's API."""
    parameters = inspect.signature(original).parameters
    accepts_options = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())
    supported = {name for name,item in parameters.items() if item.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,inspect.Parameter.KEYWORD_ONLY)}
    def schemas(run, capabilities, **options):
        forwarded = options if accepts_options else {name:value for name,value in options.items() if name in supported}
        rows = [schema for schema in original(run, capabilities, **forwarded) if schema["function"]["name"] in ALLOWED_TOOLS]
        if "tools" in capabilities and not checker_registered:
            factory = contract_factory if registered_contracts_supported(original) else None
            rows = [evaluation_checker_schema(run, factory)] + [schema for schema in rows
                                                               if schema['function']['name'] != 'evaluation_check']
        return rows
    return schemas


def install_evaluation_schemas(service, contract_factory=None):
    """Register before routing when supported; retain historical API compatibility.

    This is per disposable coordinator, not a source monkey-patch. A restarted
    worker installs the hook on its new service, and reinstalling one service
    reuses its original methods instead of stacking duplicate checker schemas.
    """
    previous = getattr(service, '_evaluation_schema_registration', None)
    original = previous['schemas'] if previous else service.jobs.registry.schemas
    extra = previous['extra'] if previous else getattr(service, 'extra_tool_schemas', None)
    registered = bool(contract_factory and registered_contracts_supported(original))
    prerouting = prerouting_evaluation_supported(service, original)
    if prerouting:
        factory = contract_factory if registered else None
        def extra_schemas(run):
            return [evaluation_checker_schema(run, factory)] + [schema for schema in extra(run)
                if schema['function']['name'] != 'evaluation_check']
        service.extra_tool_schemas = extra_schemas
    service.jobs.registry.schemas = evaluation_schema_adapter(original, contract_factory,
                                                               checker_registered=prerouting)
    service._evaluation_schema_registration = {'schemas': original, 'extra': extra}
    return {'mode': 'prerouting' if prerouting else 'compatibility', 'registered_contracts': registered}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def load_suite(path=SUITE):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or len(data.get("tasks", [])) != 20:
        raise ValueError("Evaluation suite must contain exactly twenty version-1 fixtures.")
    counts, seen = {}, set()
    for task in data["tasks"]:
        identity = task.get("id", "")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,60}", identity) or identity in seen:
            raise ValueError("Invalid or duplicate fixture ID.")
        seen.add(identity); counts[task["category"]] = counts.get(task["category"], 0) + 1
        if not isinstance(task.get("prompt"), str) or not task["prompt"].strip() or not task.get("oracle"):
            raise ValueError("Fixture is missing a task or behavioral oracle.")
        for group in ("files", "reference_files"):
            if not isinstance(task.get(group), dict): raise ValueError("Fixture files must be text maps.")
            for name, text in task[group].items():
                if not isinstance(text, str) or len(text.encode()) > 100000:
                    raise ValueError("Fixture text is too large.")
                confined_path(Path.cwd(), name, existing=False)
    if counts != {"app": 12, "broader": 4, "continuity": 4}:
        raise ValueError("Suite requires twelve app/coding, four broader and four continuity tasks.")
    return data


def evaluation_contract_factory(task,store,path=CONTRACTS):
    """Host-only acceptance identity. Neither reference text nor metadata is sent."""
    values=json.loads(Path(path).read_text(encoding='utf-8'))
    suite=load_suite()
    identities={t['id'] for t in suite['tasks']}
    if (values.get('schema_version')!=1 or values.get('fixture_hash')!=digest(suite) or
            set(values.get('cases') or {})!=identities):
        raise EvaluationIntegrityError('Acceptance contracts do not match the unchanged fixture suite.')
    canonical=next((row for row in suite['tasks'] if row['id']==task.get('id')),None)
    if canonical!=task:raise EvaluationIntegrityError('Registered acceptance requires the unchanged task fixture.')
    expected=values['cases'].get(task['id'])
    if not isinstance(expected,list) or not 1<=len(expected)<=256 or any(not isinstance(v,str) or not v.strip() or len(v)>2000 for v in expected):
        raise EvaluationIntegrityError('Acceptance contract requires the complete registered check list.')
    required=sorted(set(task['files'])|set(task['reference_files']))
    def factory(run):
        if not run.get('goal_id'):return None
        original=(run.get('goal_initial') or {}).get('tasks')
        if original is None:original=store.goal(run['goal_id'])['tasks']
        ids=[t['id'] for t in original]
        if not ids or len(ids)!=len(set(ids)):raise EvaluationIntegrityError('Registered acceptance tasks have invalid original identities.')
        return {'id':'evaluation:'+task['id']+':'+task['oracle']+':v1',
                'scope':{'fixture':task['id'],'oracle':task['oracle']},
                'expected_checks':list(expected),'scope_path':'.','required_sources':required,
                'task_ids':ids,'requirement_ids':[],'complete_field':'oracle_verified'}
    return factory


def task_admission(task,project_id,*,tracked_tasks=False):
    """Both source versions receive identical accepted task identities/text."""
    if not tracked_tasks and task['category']!='continuity':return None
    texts=task.get('tasks') or [task['prompt']]
    tasks=[]
    for index,text in enumerate(texts):
        item={'text':text,'status':'pending','evidence':[]}
        if tracked_tasks:item['id']=digest({'task':task['id'],'index':index,'text':text})[:32]
        tasks.append(item)
    return {'text':task['prompt'],'project_id':project_id,'tasks':tasks}


def confined_path(root, name, *, existing=True):
    root = Path(root).resolve()
    relative = Path(name)
    if not isinstance(name, str) or relative.is_absolute() or relative.drive or ".." in relative.parts or "\x00" in name:
        raise ValueError("Fixture paths must be relative.")
    target = root / relative
    if not target.resolve().is_relative_to(root): raise ValueError("Fixture path escaped its workspace.")
    current = target
    while current != root:
        linked = current.is_symlink() or (current.exists() and bool(getattr(current.lstat(), "st_file_attributes", 0) & 0x400))
        if linked:
            raise ValueError("Fixture paths cannot traverse links.")
        current = current.parent
    if existing and (not target.is_file() or target.stat().st_size > 256000): raise ValueError("Expected bounded fixture file is missing.")
    return target


def materialize(task, root, reference=False):
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    for name, text in {**task["files"], **(task["reference_files"] if reference else {})}.items():
        target = confined_path(root, name, existing=False); target.parent.mkdir(parents=True, exist_ok=True); target.write_text(text, encoding="utf-8")


def _text(root, name):
    return confined_path(root, name).read_text(encoding="utf-8")


def effect_targets(root, arguments, filename):
    """Compare actual file identity, accepting ordinary relative path aliases."""
    value = arguments.get("path")
    if not isinstance(value,str): return False
    try:
        path = Path(value)
        return (path if path.is_absolute() else Path(root)/path).resolve() == (Path(root)/filename).resolve()
    except (OSError, ValueError): return False


def completed_effect(result):
    value = json.loads(result) if isinstance(result,str) else result
    if not isinstance(value,dict): return False
    result = value.get("result",value)
    return isinstance(result,dict) and result.get("ok") is True


def _pure_functions(root, name):
    code = _text(root, name); tree = ast.parse(code)
    allowed = {key: getattr(builtins, key) for key in ("abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance", "len", "list", "map", "max", "min", "range", "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip", "type", "ValueError", "TypeError", "KeyError", "Exception")}
    import math
    import decimal
    safe_modules = {"math": math, "json": SimpleNamespace(loads=json.loads, dumps=json.dumps),
                    "re": SimpleNamespace(match=re.match, fullmatch=re.fullmatch, search=re.search, findall=re.findall, finditer=re.finditer,
                        sub=re.sub, split=re.split, escape=re.escape, compile=re.compile, IGNORECASE=re.IGNORECASE),
                    "decimal": SimpleNamespace(Decimal=decimal.Decimal, ROUND_HALF_UP=decimal.ROUND_HALF_UP, ROUND_HALF_EVEN=decimal.ROUND_HALF_EVEN, InvalidOperation=decimal.InvalidOperation, localcontext=decimal.localcontext)}
    def safe_import(module, *args, **kwargs):
        if module not in safe_modules: raise ValueError("Pure fixture import is outside its contract.")
        return safe_modules[module]
    allowed["__import__"] = safe_import
    banned = {"open", "exec", "eval", "compile", "getattr", "setattr", "globals", "locals", "input", "breakpoint", "__import__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and (node.module if isinstance(node, ast.ImportFrom) else node.names[0].name) not in safe_modules:
            raise ValueError("Pure fixture requested an unsupported import.")
        if isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in banned): raise ValueError("Pure fixture requested unsupported dynamic/IO access.")
        if isinstance(node, ast.Name) and node.id in banned: raise ValueError("Pure fixture requested unsupported IO or dynamic code.")
        if isinstance(node, (ast.ClassDef, ast.AsyncWith)): raise ValueError("Fixture requires pure functions without classes/IO contexts.")
    namespace = {"__builtins__": allowed, "__name__":"forge_eval_fixture"}; exec(compile(tree, name, "exec"), namespace)
    return namespace


def rendered_contrast(root,kind):
    """Trusted bounded rendered-color probe; unavailable never means passed."""
    request={'fixture_dir':str(Path(root).resolve()),'case_id':kind,'timeout_seconds':10}
    try:
        result=subprocess.run([sys.executable,'-B',str(ROOT/'assets/evaluations/contrast_probe.py')],input=json.dumps(request),capture_output=True,text=True,encoding='utf-8',timeout=14,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        value=json.loads(result.stdout)
        if not isinstance(value,dict) or value.get('status') not in ('verified','failed','unverified'): raise ValueError('Invalid rendered contrast response.')
        if not isinstance(value.get('backend'),dict): raise ValueError('Rendered contrast backend metadata is missing.')
        if value.get('status') in ('verified','failed') and (result.returncode or not all(value.get('cleanup',{}).get(key) is True for key in ('server_stopped','browser_closed'))):
            raise ValueError('Rendered contrast did not confirm owned cleanup.')
        return value
    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
        return {'status':'unverified','contrast_ratio':None,'threshold':4.5,'backend':{'name':'playwright-edge','probe_version':'1','verified':False},'diagnostics':{'error':str(exc)[:300]}}


def oracle(task, root):
    root = Path(root); checks = []; backend={'behavior':{'name':'python-behavior','version':'1','python_version':sys.version.split()[0],'verified':True}}; contrast_probe=None
    def check(name, condition, detail=""):
        checks.append(dict(requirement=name, passed=bool(condition), detail=str(detail)[:400]))
    try:
        kind = task["oracle"]
        if kind in {"todo", "signup", "catalog", "cart", "theme", "dialog", "tabs", "landing"}:
            backend['behavior']={'name':'jsdom-behavior','oracle_version':'2','verified':False}
            jsdom = ROOT / "frontend/node_modules/jsdom"
            result = subprocess.run(["node", str(ROOT / "assets/evaluations/browser_oracle.cjs"), str(jsdom), str(root), kind],
                capture_output=True, text=True, encoding="utf-8", timeout=8, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode: raise ValueError(result.stderr[:500] or "Browser oracle failed.")
            value = json.loads(result.stdout); checks.extend(value["checks"]); backend['behavior']=value['backend']
            if kind in ('theme','landing'):
                contrast_probe=rendered_contrast(root,kind); backend['contrast']=dict(contrast_probe.get('backend',{'name':'playwright-edge','verified':False}))
                ratio=contrast_probe.get('contrast_ratio'); verified=contrast_probe.get('status') in ('verified','failed') and backend['contrast'].get('verified') is True
                backend['contrast']['verified']=verified
                check('Dark theme text contrast' if kind=='theme' else 'Text contrast meets 4.5:1',verified and contrast_probe.get('status')=='verified' and isinstance(ratio,(int,float)) and ratio>=4.5,
                      json.dumps({'expected_minimum':4.5,'actual_ratio':ratio,'foreground':contrast_probe.get('foreground'),'background':contrast_probe.get('background'),'body_theme':contrast_probe.get('theme_body_state'),'status':contrast_probe.get('status'),'diagnostics':contrast_probe.get('diagnostics')},ensure_ascii=False))
        elif kind == "pagination":
            function = _pure_functions(root, "service.py")["paginate"]
            rows = [{"id": i} for i in range(7)]; old = copy.deepcopy(rows)
            check("Normal page and total calculation", function(rows, 2, 3) == {"items": rows[3:6], "page": 2, "total": 7, "total_pages": 3})
            check("Empty and out-of-range pages", function([], 1, 5)["total_pages"] == 0 and function(rows, 8, 3)["items"] == [])
            check("Records remain unchanged", rows == old)
            for page, size in ((0, 5), (True, 5), (1, 0), (1, 101), (1, True), (1.5, 4)):
                try: function(rows, page, size); rejected = False
                except ValueError: rejected = True
                check("Invalid page boundary " + repr((page, size)), rejected)
        elif kind == "pricing":
            function = _pure_functions(root, "pricing.py")["total_cents"]
            rows = [{"unit_cents": 1250, "quantity": 3}, {"unit_cents": 4000, "quantity": 1}]; old = copy.deepcopy(rows)
            check("Integer price total", function(rows) == 7750)
            check("Coupon applies ten percent to a normal basket", function(rows, "SAVE10") == 6975)
            check("Coupon rounding and empty basket", function([{"unit_cents": 5, "quantity": 1}], "SAVE10") == 5 and function([], "SAVE10") == 0)
            check("Input is immutable", rows == old)
            for value, coupon in (([{"unit_cents": -1, "quantity": 1}], None), ([{"unit_cents": 5, "quantity": True}], None), ([{"unit_cents": True, "quantity": 1}], None), (rows, "UNKNOWN")):
                try: function(value, coupon); rejected = False
                except ValueError: rejected = True
                check("Reject invalid line/coupon " + repr((value, coupon)), rejected)
        elif kind == "people":
            function = _pure_functions(root, "normalizer.py")["normalize_people"]
            rows = [{"name": " Alice ", "email": " ALICE@EXAMPLE.TEST ", "age": "21"}, {"name": "Bob", "email": "broken", "age": "3"}, {"name": "Other Alice", "email": "alice@example.test", "age": "44"}, {"name": "Cedar", "email": "cedar@example.test", "age": "0"}, {"name": "Late", "email": "late@example.test", "age": "121"}]; old = copy.deepcopy(rows)
            result = function(rows)
            check("Normalization and stable valid ordering", result["people"] == [{"name": "Alice", "email": "alice@example.test", "age": 21}, {"name": "Cedar", "email": "cedar@example.test", "age": 0}])
            check("Invalid and duplicate provenance", [item["row"] for item in result["issues"]] == [2, 3, 5] and all(item.get("reason") for item in result["issues"]))
            check("Source records unchanged", rows == old)
        elif kind == "migration":
            sql = _text(root, "migration.sql")
            with sqlite3.connect(":memory:") as db:
                db.executescript("CREATE TABLE accounts(id INTEGER PRIMARY KEY,name TEXT NOT NULL);INSERT INTO accounts VALUES(7,'Alice'),(12,'Bob');")
                if re.search(r"\b(?:ATTACH|DETACH|PRAGMA|VACUUM|load_extension)\b", sql, re.I): raise ValueError("Fixture SQL must only alter the in-memory application schema.")
                db.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH) else sqlite3.SQLITE_OK)
                db.executescript(sql); db.set_authorizer(None)
                check("Existing rows and defaults preserved", db.execute("SELECT id,name,active,email FROM accounts ORDER BY id").fetchall() == [(7,"Alice",1,None),(12,"Bob",1,None)])
                db.execute("INSERT INTO accounts(name,email) VALUES('Cedar','cedar@example.test')")
                db.execute("INSERT INTO accounts(name) VALUES('Dale')")
                for query in ("INSERT INTO accounts(name,email) VALUES('Copy','cedar@example.test')", "INSERT INTO accounts(name,active) VALUES('Invalid',2)"):
                    try: db.execute(query); rejected = False
                    except sqlite3.IntegrityError: rejected = True
                    check("Schema rejects invalid row", rejected)
        elif kind in ("sales", "continuity"):
            from decimal import Decimal
            source = list(csv.DictReader(io.StringIO(task["files"]["orders.csv"])))
            totals = {}
            for row in source:
                if row["status"] == "paid":
                    total, count = totals.get(row["region"], (Decimal(0), 0)); totals[row["region"]] = total + Decimal(row["amount"]), count + 1
            expected = [{"region": region, "total": format(amount, ".2f"), "orders": count} for region, (amount, count) in sorted(totals.items())]
            if task.get("injection") == "steer": expected.sort(key=lambda item: -Decimal(item["total"]))
            actual = list(csv.DictReader(io.StringIO(_text(root, "report.csv")))) if kind == "sales" else json.loads(_text(root, "totals.json"))
            for row in actual: row["orders"] = int(row["orders"])
            check("Paid-only totals, counts and requested ordering", actual == expected)
            if kind == "continuity": check("Completed normalization preserves all rows", json.loads(_text(root, "normalized.json")) == source)
        elif kind == "sources":
            value = json.loads(_text(root, "requirements.json"))
            check("Newer source resolves each conflict", value.get("python") == "3.12" and value.get("node") == "22.12+" and value.get("port") == 8081 and "pairing" in value.get("authentication", "").lower() and "browser" in value.get("authentication", "").lower())
            check("Resolved facts retain source provenance", set(value.get("source", {})) == {"python", "node", "port", "authentication"} and all(source == "setup-2026.txt" for source in value["source"].values()))
        elif kind == "runbook":
            raw = _text(root, "runbook.json"); value = json.loads(raw)
            check("Safe operation dependency sequence", [step.get("operation") for step in value.get("steps", [])] == ["stop", "backup", "migrate", "verify", "start"] and all(step.get("purpose") for step in value["steps"]))
            prerequisites = value.get("prerequisites", [])
            check("Documented storage prerequisite", any(isinstance(item, dict) and item.get("disk_space_mb") == 500 for item in prerequisites))
            check("Private credential excluded", "example-do-not-export-739" not in raw)
        elif kind == "invoice":
            from decimal import Decimal, ROUND_HALF_UP
            rows = list(csv.DictReader(io.StringIO(_text(root, "invoice.csv"))))
            inputs = json.loads(task["files"]["items.json"])
            expected, total = [], Decimal(0)
            for item in inputs:
                unit = Decimal(item["unit_price"]); line = (unit * item["quantity"]).quantize(Decimal(".01"), rounding=ROUND_HALF_UP); total += line
                expected.append(dict(item=item["item"], quantity=str(item["quantity"]), unit_price=format(unit.quantize(Decimal(".01"), rounding=ROUND_HALF_UP), ".2f"), line_total=format(line, ".2f")))
            expected.append(dict(item="TOTAL", quantity="", unit_price="", line_total=format(total, ".2f")))
            check("CSV quoting, decimal rounding and grand total", rows == expected)
        else: raise ValueError("Unknown behavioral oracle.")
        for name, original in task["files"].items():
            if task["category"] != "app": check("Source input preserved: " + name, _text(root, name) == original)
    except Exception as exc:
        check("Required behavior is inspectable", False, str(exc))
    return dict(passed=bool(checks) and all(item["passed"] for item in checks), checks=checks,
                oracle_backend=backend,oracle_verified=all(item.get('verified') is True for item in backend.values()),contrast_probe=contrast_probe,
                visual_review="Full visual review not performed; DOM behavior/structure is checked and theme/landing text contrast uses a real browser.")


def bounded_oracle(task, root):
    config = {"task": task, "workspace": str(Path(root).resolve())}
    with tempfile.TemporaryDirectory(prefix="forge-oracle-") as folder:
        path = Path(folder) / "request.json"; path.write_text(json.dumps(config), encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/evaluate_tasks.py"), "--oracle-worker", str(path)],
            capture_output=True, text=True, encoding="utf-8", timeout=26 if task['oracle'] in ('theme','landing') else 12, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode: return dict(passed=False, checks=[dict(requirement="Bounded oracle completes", passed=False, detail=result.stderr[-500:])])
        return json.loads(result.stdout)


def dry_validate(suite_path=SUITE, repetitions=3):
    data = load_suite(suite_path); results = []
    for repetition in range(repetitions):
        for task in data["tasks"]:
            with tempfile.TemporaryDirectory(prefix="forge-fixture-") as folder:
                root = Path(folder); materialize(task, root)
                unfinished = bounded_oracle(task, root)
                materialize(task, root, reference=True)
                reference = bounded_oracle(task, root)
                results.append(dict(task=task["id"], repetition=repetition + 1,
                    rejects_unfinished=not unfinished["passed"], reference_passed=reference["passed"], checks=reference["checks"]))
    return dict(schema_version=1, mode="fixture_validation", actual_model_calls=0, fixture_hash=digest(data), repetitions=repetitions,
                passed=all(item["rejects_unfinished"] and item["reference_passed"] for item in results), cases=results)


def source_hash(repo):
    """Fingerprint authored input, excluding generated output and local profiles.

    Generic state names are repository-root exclusions: a skill's references/data
    or a frontend src/models directory remains authored source. Build/cache and
    Forge profile directories are generated regardless of where they occur.
    """
    repo = Path(repo).resolve(); files = []
    excluded = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                ".forge", ".sidekick", ".idea", ".vscode", "dist", "build", "eval-results", "htmlcov"}
    root_state = {"outputs", "screenshots", "release", "workspace", "data", "app-data", "exports", "backups", "webview", "models"}
    for current, dirs, names in os.walk(repo):
        relative = Path(current).relative_to(repo)
        dirs[:] = sorted(name for name in dirs if name.casefold() not in excluded
                         and not (relative == Path(".") and name.casefold() in root_state)
                         and not (relative.as_posix().casefold() == "benchmarks" and name.casefold() == "local")
                         and not (Path(current)/name).is_symlink())
        for name in sorted(names):
            path = Path(current) / name
            if path.suffix.lower() in {".py", ".ts", ".tsx", ".js", ".cjs", ".css", ".json", ".md", ".toml", ".html", ".lock"} and not path.is_symlink():
                files.append((path.relative_to(repo).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    return digest(files)


def harness_hash():
    return digest([(name,hashlib.sha256((ROOT/name).read_bytes()).hexdigest()) for name in
        ("forge_evals.py","scripts/evaluate_tasks.py","assets/evaluations/suite.json","assets/evaluations/contracts.json","assets/evaluations/browser_oracle.cjs","assets/evaluations/contrast_probe.py")])


def local_model(base="http://127.0.0.1:11434/api", model=None, allow_installed=False):
    from urllib.parse import urlsplit
    import httpx
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
        raise ValueError("Evaluation requires an explicit loopback Ollama endpoint.")
    response = httpx.get(base.rstrip("/") + "/ps", timeout=5, trust_env=False); response.raise_for_status()
    loaded = response.json().get("models", [])
    if model: loaded = [item for item in loaded if item.get("name") == model or item.get("model") == model]
    if not loaded and model and allow_installed:
        response = httpx.get(base.rstrip("/") + "/tags", timeout=5, trust_env=False); response.raise_for_status()
        loaded = [item for item in response.json().get("models", []) if item.get("name") == model or item.get("model") == model]
    if len(loaded) != 1: raise ValueError("Specify one currently loaded local model; evaluation never pulls or switches models.")
    item = loaded[0]
    if not isinstance(item.get("digest"), str) or not re.fullmatch(r"[a-f0-9]{64}", item["digest"]):
        raise ValueError("Exact model digest is required for comparable evaluation provenance.")
    return dict(model=item.get("name") or item["model"], digest=item.get("digest"), context_length=item.get("context_length"), base=base.rstrip("/"), observed_at=time.time())


def run_worker(config):
    """Called in its own process; import coordinator ONLY after selecting repo."""
    repo = Path(config["repo"]).resolve(); sys.path.insert(0, str(repo))
    workspace = Path(config["workspace"]).resolve(); profile = Path(config["profile"]).resolve()
    os.environ["FORGE_DATA_DIR"] = str(profile); os.environ["SIDEKICK_DATA_DIR"] = str(profile)
    from core import Core
    from forge_service import ForgeService
    from forge_store import TERMINAL
    service = None; task = config["task"]; started = time.monotonic(); injected = False; injection = {}; pending_compaction = {}; requests = []; oracle_results = []; wire_requests=[]; control_errors=[]; task_refusal=None; run=None; contract_enabled=False
    def app_call(stage,operation,*args,**kwargs):
        nonlocal task_refusal
        try: return True,operation(*args,**kwargs)
        except EvaluationIntegrityError: raise
        except ValueError as exc:
            task_refusal={'stage':stage,'error':str(exc)[:1500],'elapsed_seconds':round(time.monotonic()-started,3)}
            return False,None
    def setup():
        nonlocal contract_enabled
        if type(config.get('tracked_tasks',False)) is not bool:raise EvaluationIntegrityError('Tracked-task admission selection must be boolean.')
        controls = config["controls"]
        engine_core=install_transport_shim(Core(base=config["engine"]["base"]),config['engine'],controls,wire_requests,control_errors,require_native_false=config.get('require_native_think',False))
        value = ForgeService(core=engine_core, data_dir=profile)
        value.store.update_settings(dict(model=config["engine"]["model"], provider_id="ollama", context=controls["context"], tokens=controls["tokens"], temperature=controls["temperature"], thinking=controls["thinking"],
            permission_profile="full_access", web=False, browser_tools=False, computer_tools=False, memory_enabled=False, memory_suggestions=False, auto_delegate=False, goal_review_enabled=False,
            goal_limits=dict(minutes=max(1,(controls["max_seconds"]+59)//60), tokens=100000, rounds=controls["rounds"], tools=controls["tools"])))
        if config.get('guided_execution') is not None:
            if type(config['guided_execution']) is not bool:raise EvaluationIntegrityError('Guided-execution ablation selection must be boolean.')
            value.store.update_settings({'guided_execution':config['guided_execution']})
        original_execute = value.jobs.registry.execute
        def execute(run, name, args, cancel, **kwargs):
            if name not in ALLOWED_TOOLS: return {"ok":False,"not_executed":True,"error":"Tool is outside the isolated evaluation policy."}
            if name == "evaluation_check":
                if args: return {"ok":False,"error":"Evaluation checks accept no paths or code."}
                result = bounded_oracle(task, workspace); oracle_results.append(result); return result
            return original_execute(run, name, args, cancel, **kwargs)
        factory=evaluation_contract_factory(task,value.store) if config.get('tracked_tasks',False) else None
        contract_enabled=install_evaluation_schemas(value,factory)['registered_contracts']
        value.jobs.registry.execute = execute
        original_generate = value.providers.generate
        def generate(data, *args, **kwargs):
            if data.get("provider_id", "ollama") != "ollama" or data["model"] != config["engine"]["model"]:
                control_errors.append('Requested provider/model changed.')
                raise EvaluationIntegrityError("Evaluation provider/model changed.")
            if data.get('thinking',False) is not False:
                control_errors.append('Requested thinking control changed.')
                raise EvaluationIntegrityError('Evaluation requires requested thinking=false.')
            requests.append({key:data.get(key) for key in ("model", "context", "temperature", "thinking", "tokens")})
            yield from original_generate(data, *args, **kwargs)
        value.providers.generate = generate
        return value
    try:
        service = setup(); project = service.create_project({"name":task["id"],"path":str(workspace)})
        coordinator_module = Path(sys.modules["forge_service"].__file__).resolve()
        if coordinator_module.parent != repo:
            raise EvaluationIntegrityError("Coordinator import came from the wrong source checkout.")
        admission=task_admission(task,project['id'],tracked_tasks=config.get('tracked_tasks',False))
        goal=service.goal_create(admission) if admission else None
        original_event = service.store.event
        def event(run_id, kind, **payload):
            nonlocal injected, injection, pending_compaction
            result = original_event(run_id, kind, **payload)
            if task.get("injection") == "compact" and not injected and kind == "round" and pending_compaction:
                injected = True; injection = pending_compaction
                service.jobs.cancel(run_id, pause=True)
                return result
            if task.get("injection") and not injected and kind == "tool" and payload.get("state") == "done" and payload.get("name") in ("write_file", "edit_file"):
                with service.store._connection() as db:
                    invocation = dict(db.execute("SELECT * FROM invocations WHERE id=?", (payload["invocation_id"],)).fetchone())
                arguments = json.loads(invocation["arguments"])
                if not effect_targets(workspace, arguments, "normalized.json") or not completed_effect(invocation.get("result")): return result
                try:
                    normalized = json.loads(_text(workspace,"normalized.json"))
                    expected = list(csv.DictReader(io.StringIO(task["files"]["orders.csv"])))
                    if normalized != expected: return result
                except (ValueError,OSError): return result
                checkpoint = {"kind":task["injection"],"invocation_id":invocation["id"],"normalized_hash":hashlib.sha256(confined_path(workspace,"normalized.json").read_bytes()).hexdigest()}
                if task["injection"] == "compact":
                    if not pending_compaction: pending_compaction = checkpoint
                    return result
                injected = True; injection = checkpoint
                if task["injection"] == "steer":
                    steered=service.jobs.steer(run_id, "Keep the completed normalization. Change totals.json to sort by total descending (highest first), retaining paid-only totals and the existing fields.", "evaluation-steer")
                    injection['steering_queued']=steered.get('status')=='queued'
                else:
                    if task["injection"] == "unknown": service.store.invocation_state(invocation["id"], "outcome_unknown", {"injected":"Completed effect requires inspection."})
                    service.jobs.cancel(run_id, pause=True)
            return result
        service.store.event = event
        instructions = "Work only in the provided disposable project. No commands, network, cloud, computer or publishing tools are available. Use evaluation_check for acceptance evidence and repair failures. Complete the requested task; do not ask for unspecified optional preferences."
        accepted,run = app_call('start',service.jobs.start,{"text":task["prompt"],"project_id":project["id"],"instructions":instructions, "agent_tools":sorted(ALLOWED_TOOLS), **({"goal_id":goal["id"],"mode":"goal"} if goal else {})})
        if not accepted:
            with service.store._connection() as db:
                existing=db.execute('SELECT id FROM runs ORDER BY created_at DESC LIMIT 1').fetchone()
            if existing: run=service.store.run(existing['id'])
        while accepted and run:
            current = service.store.run(run["id"])
            if time.monotonic() - started > config["controls"]["max_seconds"]:
                service.jobs.cancel(run["id"], pause=True); injection["timeout"] = True
                deadline = time.monotonic() + 5
                while service.store.run(run["id"])["status"] not in TERMINAL and time.monotonic() < deadline: time.sleep(.05)
                break
            if current["status"] in TERMINAL:
                if injected and task.get("injection") in ("restart", "compact", "unknown") and not injection.get("resumed") and current["status"] in ("paused", "interrupted"):
                    if task["injection"] == "restart":
                        service.shutdown(); service = setup()
                    elif task["injection"] == "compact":
                        accepted,_=app_call('compact',service.jobs.compact,run['id'])
                        if not accepted: break
                        injection["compacted"] = True
                    elif task["injection"] == "unknown":
                        try: service.jobs.resume(run["id"]); injection["resume_blocked"] = False
                        except EvaluationIntegrityError: raise
                        except ValueError: injection["resume_blocked"] = True
                        accepted,_=app_call('resolve_action',service.resolve_action,{"invocation_id":injection["invocation_id"],"outcome":"completed","evidence":"Inspected normalized.json SHA256 " + injection["normalized_hash"]})
                        if not accepted: break
                        injection["inspected"] = True
                    accepted,_=app_call('resume',service.jobs.resume,run['id'])
                    if not accepted: break
                    injection["resumed"] = True; continue
                break
            time.sleep(.05)
        current = service.store.run(run['id']) if run else {'status':'paused','rounds':0,'tools':0}
        if task_refusal and run and current['status'] not in TERMINAL:
            service.jobs.cancel(run['id'],pause=True)
            deadline=time.monotonic()+5
            while service.store.run(run['id'])['status'] not in TERMINAL and time.monotonic()<deadline: time.sleep(.05)
            current=service.store.run(run['id'])
        final_oracle = bounded_oracle(task, workspace)
        if task.get('injection')=='steer' and run:
            injection['steering_applied']=any(item.get('run_id')==run['id'] and item.get('status')=='applied'
                for item in service.store.entities('steers'))
        if task.get("injection"):
            final_oracle["checks"].append(dict(requirement="Required continuity injection occurred",passed=injected,detail=task["injection"]))
            if task["injection"] in ("restart", "compact", "unknown"):
                with service.store._connection() as db:
                    writes = [dict(row) for row in db.execute("SELECT id,name,arguments,status,result FROM invocations WHERE run_id=? AND name IN ('write_file','edit_file') ORDER BY rowid",(run['id'] if run else None,))]
                first = next((index for index,row in enumerate(writes) if row["id"] == injection.get("invocation_id")),len(writes))
                repeats = sum(effect_targets(workspace,json.loads(row["arguments"]),"normalized.json") and (row["id"] == injection.get("invocation_id") or completed_effect(row.get("result"))) for row in writes[first:])
                final_oracle["checks"].append(dict(requirement="Completed stage was not repeated",passed=repeats == 1,detail=str(repeats)))
            if task["injection"] == "unknown": final_oracle["checks"].append(dict(requirement="Unknown effect blocked resume until inspected",passed=injection.get("resume_blocked") and injection.get("inspected"),detail="Inspection journal retained."))
            if goal: final_oracle["checks"].append(dict(requirement="Goal checklist finished with evidence",passed=all(item["status"] == "completed" and item.get("evidence") for item in service.store.goal(goal["id"])["tasks"]),detail="Durable goal state"))
            final_oracle["passed"] = all(item["passed"] for item in final_oracle["checks"])
        with service.store._connection() as db:
            usage = [dict(row) for row in db.execute("SELECT * FROM usage ORDER BY created_at")]
            invocations = [dict(row) for row in db.execute("SELECT name,status,result FROM invocations WHERE run_id=?",(run['id'] if run else None,))]
        errors = sum(row["status"] not in ("completed", "prepared") or '"error"' in (row.get("result") or '') or '"ok":false' in (row.get("result") or '').replace(' ', '') for row in invocations)
        return dict(status='paused' if task_refusal else current["status"], recorded_status=current['status'],task_refusal=task_refusal, success=not task_refusal and current["status"] == "completed" and final_oracle["passed"], oracle=final_oracle, injection=injection,
            wall_seconds=round(time.monotonic()-started,3), rounds=current.get("rounds",0), tool_calls=current.get("tools",0), tool_errors=errors,
            input_tokens=sum(row["input_tokens"] for row in usage), output_tokens=sum(row["output_tokens"] for row in usage), cached_input_tokens=sum(row["cached_input_tokens"] for row in usage),
            reported_requests=sum(not row["estimated"] for row in usage), estimated_requests=sum(bool(row["estimated"]) for row in usage), actual_model_requests=len(requests), requests=requests,
            inference_attempts=len(requests),transport_envelopes=len(wire_requests),
            reported_input_tokens=sum(row['input_tokens'] for row in usage if not row['estimated']),
            estimated_input_tokens=sum(row['input_tokens'] for row in usage if row['estimated']),
            models=sorted({row["model"] for row in usage}), cloud_requests=sum(row["provider"] != "ollama" for row in usage), recovery=current.get("recovery"),
            checkpoint=current.get("checkpoint"), oracle_calls=len(oracle_results), run_id=run['id'] if run else None,wire_requests=wire_requests,control_errors=control_errors,control_valid=not control_errors,
            oracle_backend=final_oracle.get('oracle_backend'),oracle_verified=final_oracle.get('oracle_verified',False),
            tracked_tasks=bool(config.get('tracked_tasks',False)),task_admission=admission,
            workflow_execution=current.get('execution_mode','legacy'),
            continuity_exercised=(bool(injected) and (bool(injection.get('steering_applied')) if task['injection']=='steer' else
                bool(injection.get('resumed')) and (bool(injection.get('compacted')) if task['injection']=='compact' else
                bool(injection.get('resume_blocked') and injection.get('inspected')) if task['injection']=='unknown' else True))) if task.get('injection') else None,
            registered_contracts=contract_enabled)
    except Exception as exc:
        return dict(status="harness_error",success=False,error=str(exc)[:1500],wall_seconds=round(time.monotonic()-started,3),injection=injection,actual_model_requests=len(requests),requests=requests,cloud_requests=0,wire_requests=wire_requests,control_errors=control_errors,control_valid=False)
    finally:
        if service: service.shutdown()


def summarize(rows):
    groups = {}
    for row in rows: groups.setdefault(row["variant"], []).append(row)
    totals = {key:dict(runs=len(values), successes=sum(bool(item.get("success")) for item in values),failures=sum(not item.get("success") for item in values),
        failure_rate=sum(not item.get("success") for item in values)/len(values) if values else None,
        wall_seconds=round(sum(item.get("wall_seconds",0) for item in values),3), actual_model_requests=sum(item.get("actual_model_requests",0) for item in values),
        input_tokens=sum(item.get("input_tokens",0) for item in values), output_tokens=sum(item.get("output_tokens",0) for item in values)) for key,values in groups.items()}
    by_key = {(item["task"],item["repetition"],item["variant"]):item for item in rows}
    pairs = []; matched_rows = []; comparable_rows = []
    for task, repetition, variant in by_key:
        if variant != "baseline": continue
        old,new = by_key[(task,repetition,variant)],by_key.get((task,repetition,"candidate"))
        if not new: continue
        comparable = old.get("measurement_valid",True) and new.get("measurement_valid",True) and old.get('control_valid',False) and new.get('control_valid',False) and old.get('oracle_verified',False) and new.get('oracle_verified',False) and old.get('oracle_backend')==new.get('oracle_backend') and old.get('transport_shim')==new.get('transport_shim')==TRANSPORT_SHIM_VERSION and bool(old.get('normalized_controls')) and old.get('normalized_controls')==new.get('normalized_controls') and old.get("execution_mode") == "both" and new.get("execution_mode") == "both" and old.get("pair_order") == new.get("pair_order") and old["fixture_hash"] == new["fixture_hash"] and bool(old.get("harness_hash")) and old.get("harness_hash") == new.get("harness_hash") and old["controls"] == new["controls"] and old["engine"]["model"] == new["engine"]["model"] and old["engine"].get("digest") == new["engine"].get("digest")
        comparable &= old.get('tracked_tasks',False)==new.get('tracked_tasks',False) and old.get('guided_execution')==new.get('guided_execution')
        if comparable and "started_at" in old and "started_at" in new:
            first,second = (old,new) if old["pair_order"][0] == "baseline" else (new,old)
            comparable = 0 <= second["started_at"]-first.get("finished_at",first["started_at"]) <= 120
        if comparable: comparable_rows.append((old,new))
        if comparable and old.get("success") and new.get("success"): matched_rows.append((old,new))
        pairs.append(dict(task=task,repetition=repetition,comparable=comparable,matched_success=comparable and old.get("success") and new.get("success"),
                          baseline_seconds=old.get("wall_seconds"),candidate_seconds=new.get("wall_seconds"),pair_order=old.get("pair_order",["baseline","candidate"])))
    matched = [item for item in pairs if item["matched_success"]]
    ratio = sum(item["candidate_seconds"] for item in matched)/sum(item["baseline_seconds"] for item in matched) if matched and sum(item["baseline_seconds"] for item in matched) else None
    old_failures = sum(not old.get("success") for old,new in comparable_rows)
    new_failures = sum(not new.get("success") for old,new in comparable_rows)
    def paired_tokens(values):
        old = sum(item[0].get("input_tokens",0) for item in values); new = sum(item[1].get("input_tokens",0) for item in values)
        return dict(pairs=len(values),baseline_input_tokens=old,candidate_input_tokens=new,reduction_fraction=1-new/old if old else None)
    old_median = median(old["wall_seconds"] for old,new in matched_rows) if matched_rows else None
    new_median = median(new["wall_seconds"] for old,new in matched_rows) if matched_rows else None
    median_ratio = new_median/old_median if old_median else None
    return dict(schema_version=1,mode="live_local",totals=totals,paired_runs=len(pairs),comparable_pairs=sum(item["comparable"] for item in pairs),matched_success_pairs=len(matched),
                matched_success_wall_ratio=ratio,matched_success_summed_wall_ratio=ratio,
                comparable_failure_metrics=dict(pairs=len(comparable_rows),baseline_failures=old_failures,candidate_failures=new_failures,
                    baseline_failure_rate=old_failures/len(comparable_rows) if comparable_rows else None,candidate_failure_rate=new_failures/len(comparable_rows) if comparable_rows else None,
                    relative_failed_task_reduction=1-new_failures/old_failures if old_failures else None),
                mutual_success_median_wall=dict(pairs=len(matched_rows),baseline_seconds=old_median,candidate_seconds=new_median,ratio=median_ratio,reduction_fraction=1-median_ratio if median_ratio is not None else None),
                comparable_all_run_input_tokens=paired_tokens(comparable_rows),mutual_success_input_tokens=paired_tokens(matched_rows),
                continuity_coverage={key:{'required':sum(item.get('category')=='continuity' for item in values),
                    'exercised':sum(item.get('category')=='continuity' and bool(item.get('continuity_exercised',item.get('injection',{}).get('kind'))) for item in values)} for key,values in groups.items()},
                pairs=pairs,interpretation="Success and behavioral evidence precede speed comparison. Sum and median ratios are distinct; missing denominators are unavailable. Unpaired/failed tasks do not establish a speed gain; DOM checks do not establish visual quality.")


def live_suite(baseline, candidate, output, *, variant="both", repetitions=3, base="http://127.0.0.1:11434/api", model=None, controls=None, case=None, allow_installed=False,require_native_think=False,tracked_tasks=False,guided_execution=None):
    if type(tracked_tasks) is not bool or guided_execution is not None and type(guided_execution) is not bool:
        raise EvaluationIntegrityError('Tracked task and guided execution selections must be boolean.')
    data = load_suite(); engine = local_model(base, model, allow_installed); controls = {**CONTROLS,**(controls or {})}
    if require_native_think and (variant!='candidate' or not case): raise ValueError('Native-control assertion is for a separate one-case candidate preflight only.')
    if not 1 <= repetitions <= 3 or not 15 <= controls["max_seconds"] <= 180 or any(controls[key] != CONTROLS[key] for key in ("context","temperature","thinking","tokens","rounds","tools")):
        raise ValueError("Evaluation controls exceed the approved local-suite bounds.")
    # Warm the exact already-installed model/context before measured samples so
    # first-load cost does not systematically disadvantage the baseline.
    import httpx
    response=httpx.post(engine['base']+'/show',json={'model':engine['model']},timeout=10,trust_env=False)
    response.raise_for_status(); engine['thinking_control']=thinking_contract(response.json())
    normalized=normalized_controls(engine,controls)
    warm_started = time.monotonic()
    response = httpx.post(engine["base"] + "/generate", json={"model":engine["model"],"prompt":"","stream":False,"think":False,
        "options":{"num_ctx":controls["context"],"temperature":controls["temperature"],"num_predict":0}}, timeout=180, trust_env=False)
    response.raise_for_status(); engine["warm_seconds"] = round(time.monotonic()-warm_started,3); engine["warm_control_requests"] = 1
    output = Path(output).resolve(); output.mkdir(parents=True,exist_ok=True)
    repos = {"baseline":Path(baseline).resolve(),"candidate":Path(candidate).resolve()}
    for repo in repos.values():
        if not (repo/"forge_service.py").is_file() or output.is_relative_to(repo): raise ValueError("Use source repositories and a separate evaluation output directory.")
    versions = {key:source_hash(repo) for key,repo in repos.items()}; evaluator_version = harness_hash()
    selected = data["tasks"] if not case else [task for task in data["tasks"] if task["id"] == case]
    if not selected: raise ValueError("Unknown evaluation case.")
    rows = []
    for path in output.glob("*/result.json"):
        saved = json.loads(path.read_text(encoding="utf-8"))
        expected_order = ["baseline","candidate"] if (next((index for index,task in enumerate(data["tasks"]) if task["id"] == saved.get("task")),0)+saved.get("repetition",1))%2 else ["candidate","baseline"]
        if saved.get("variant") not in versions or saved.get("source_hash") != versions[saved["variant"]] or saved.get("harness_hash") != evaluator_version or saved.get("pair_order") != expected_order or saved.get("controls") != controls or saved.get('transport_shim')!=TRANSPORT_SHIM_VERSION or saved.get('normalized_controls')!=normalized or saved.get('require_native_think',False)!=require_native_think or saved.get('tracked_tasks',False)!=tracked_tasks or saved.get('guided_execution')!=guided_execution or saved.get("engine",{}).get("digest") != engine.get("digest"):
            raise ValueError("Existing output has different provenance; choose a new output directory.")
        rows.append(saved)
    for repetition in range(1,repetitions+1):
        for task in selected:
            case_index = next(index for index,item in enumerate(data["tasks"]) if item["id"] == task["id"])
            pair_order = ["baseline","candidate"] if (case_index+repetition)%2 else ["candidate","baseline"]
            for key in (pair_order if variant == "both" else [variant]):
                if harness_hash() != evaluator_version: raise ValueError("Evaluation harness changed; recapture a stable suite before continuing.")
                if source_hash(repos[key]) != versions[key]: raise ValueError("Source changed during evaluation; stop and capture a stable revision.")
                observed = local_model(base,engine["model"])
                if observed.get("digest") != engine.get("digest"): raise ValueError("Loaded model changed during evaluation.")
                destination = output / f"{task['id']}-{repetition}-{key}"; result_path = destination / "result.json"
                metadata = dict(task=task["id"],category=task["category"],repetition=repetition,variant=key,source_hash=versions[key],harness_hash=evaluator_version,
                    fixture_hash=digest({k:v for k,v in task.items() if k != "reference_files"}),controls=controls,engine=engine,pair_order=pair_order,transport_shim=TRANSPORT_SHIM_VERSION,normalized_controls=normalized,require_native_think=require_native_think,
                    tracked_tasks=tracked_tasks,guided_execution=guided_execution)
                if result_path.is_file():
                    row = json.loads(result_path.read_text(encoding="utf-8"))
                    if any(row.get(field) != metadata[field] for field in ("source_hash","harness_hash","fixture_hash","controls","pair_order","transport_shim","normalized_controls","require_native_think","tracked_tasks","guided_execution")) or row.get("engine",{}).get("digest") != engine.get("digest"):
                        raise ValueError("Saved evaluation does not match source, fixture or controls; choose a new output directory.")
                    if not any(item.get("task") == row["task"] and item.get("repetition") == row["repetition"] and item.get("variant") == row["variant"] for item in rows): rows.append(row)
                    continue
                # Preserve interrupted attempts; every new measurement still
                # begins from an identical clean project/profile snapshot.
                attempt = destination / ("attempt-"+str(time.time_ns()))
                workspace,profile = attempt/"project",attempt/"profile"; materialize(task,workspace)
                metadata.update(execution_mode=variant,started_at=time.time(),attempt=str(attempt))
                config = {**metadata,"repo":str(repos[key]),"workspace":str(workspace),"profile":str(profile),"task":task}
                request = destination / "request.json"; request.write_text(json.dumps(config),encoding="utf-8")
                print(json.dumps({"event":"task_start",**{k:metadata[k] for k in ("task","repetition","variant")}}),flush=True)
                process = subprocess.Popen([sys.executable,"-B",str(ROOT/"scripts/evaluate_tasks.py"),"--worker",str(request)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8",creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
                try:
                    stdout,stderr = process.communicate(timeout=controls["max_seconds"]+45)
                    outcome = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else dict(status="worker_failed",success=False,error=stderr[-1000:])
                except (subprocess.TimeoutExpired,ValueError) as exc:
                    process.kill(); process.communicate(timeout=5); outcome=dict(status="worker_timeout",success=False,error=str(exc),wall_seconds=controls["max_seconds"]+45)
                row={**metadata,**outcome,"finished_at":time.time(),"measurement_valid":source_hash(repos[key]) == versions[key] and harness_hash() == evaluator_version and outcome.get('control_valid',False)}
                result_path.write_text(json.dumps(row,indent=2),encoding="utf-8"); rows.append(row)
                print(json.dumps({"event":"task_result","task":task["id"],"repetition":repetition,"variant":key,"success":row["success"],"status":row["status"],"seconds":row.get("wall_seconds")}),flush=True)
                (output/"summary.json").write_text(json.dumps(summarize(rows),indent=2),encoding="utf-8")
                if not row["measurement_valid"]:
                    reason='Source/harness changed during the task.' if source_hash(repos[key])!=versions[key] or harness_hash()!=evaluator_version else 'Benchmark configuration/control or worker integrity failed: '+str(outcome.get('control_errors') or outcome.get('error') or outcome.get('status'))
                    raise EvaluationIntegrityError(reason+' Retained result is excluded from matched comparison.')
    return summarize(rows)
