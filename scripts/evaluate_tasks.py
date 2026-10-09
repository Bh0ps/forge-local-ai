"""Dry-validate fixtures or run an isolated, paired local-backend suite."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from forge_evals import CONTROLS, ROOT, SUITE, dry_validate, live_suite, oracle, run_worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Make actual local model requests; otherwise validate the offline oracles.")
    parser.add_argument("--baseline", default=str(ROOT.parent / "forge-5.0-baseline"))
    parser.add_argument("--candidate", default=str(ROOT))
    parser.add_argument("--variant", choices=("both", "baseline", "candidate"), default="both")
    parser.add_argument("--output", help="Separate output directory for retained checkpoints and result provenance.")
    parser.add_argument("--model", help="Exact model already loaded in Ollama; no pull or model switch.")
    parser.add_argument("--load-installed", action="store_true", help="Explicitly authorize warming the exact --model tag already installed; never downloads.")
    parser.add_argument('--require-native-think',action='store_true',help='Candidate one-case preflight: refuse payloads unless the runtime already emits explicit think:false before the shared shim.')
    parser.add_argument('--tracked-tasks',action='store_true',help='Admit every fixture as the same tracked goal in both versions; host contracts stay off the model wire.')
    parser.add_argument('--guided-execution',choices=('source-default','on','off'),default='source-default',help='Explicit isolated ablation selection; does not modify personal settings.')
    parser.add_argument("--base", default="http://127.0.0.1:11434/api")
    parser.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--max-seconds", type=int, choices=range(15, 181), default=180)
    parser.add_argument("--case", help="Optional one-case smoke check; does not constitute the full suite.")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--oracle-worker", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.worker:
        result = run_worker(json.loads(Path(arguments.worker).read_text(encoding="utf-8")))
    elif arguments.oracle_worker:
        data = json.loads(Path(arguments.oracle_worker).read_text(encoding="utf-8")); result = oracle(data["task"], data["workspace"])
    elif arguments.live:
        if not arguments.output: parser.error("--live requires a separate --output directory")
        result = live_suite(arguments.baseline, arguments.candidate, arguments.output, variant=arguments.variant,
            repetitions=arguments.repetitions, base=arguments.base, model=arguments.model,
            controls={"max_seconds":arguments.max_seconds}, case=arguments.case, allow_installed=arguments.load_installed,require_native_think=arguments.require_native_think,
            tracked_tasks=arguments.tracked_tasks,guided_execution={'source-default':None,'on':True,'off':False}[arguments.guided_execution])
    else:
        result = dry_validate(SUITE, arguments.repetitions)
        if arguments.output:
            path = Path(arguments.output).resolve(); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    display = result if arguments.worker or arguments.oracle_worker or arguments.live else {key:value for key,value in result.items() if key != "cases"} | {"case_runs":len(result["cases"]),"failed":[item for item in result["cases"] if not item["reference_passed"] or not item["rejects_unfinished"]]}
    print(json.dumps(display), flush=True)
    return 0 if arguments.worker or arguments.oracle_worker or result.get("passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
