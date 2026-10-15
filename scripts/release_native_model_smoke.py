"""Verify the frozen host loads owned native workers on disposable state."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from forge_native_models import read_json
from project_tools import _WindowsProcessJob


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--executable',type=Path,required=True)
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    executable=args.executable.resolve(strict=True)
    output=args.output.absolute()
    if os.name!='nt' or output.exists() or output.resolve().is_relative_to(executable.parent):
        parser.error('Use Windows and a new disposable output directory outside the application.')
    output.mkdir(parents=True)
    env=os.environ.copy()
    env.update(FORGE_DATA_DIR=str(output/'state'),LOCAL_AI_SMOKE_PATH=str(output/'native-smoke.json'),
               FORGE_NATIVE_SMOKE_MODEL=str(args.model.resolve(strict=True)))
    process=subprocess.Popen([str(executable),'--smoke-test','--native-model-smoke'],env=env,
        creationflags=0x08000000,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    job=_WindowsProcessJob(process)
    try:
        if job.handle is None:raise ValueError('The disposable native host could not be contained.')
        code=process.wait(timeout=180)
        result=read_json(output/'native-smoke.json')
        checks=result.get('native_model',{})
        required=('text','tool_arguments','tool_continuation','strata_support_imported',
                  'tokenizer_round_trip','template_rendered','cpu_dictation_available','cpu_only','worker_stopped')
        passed=code==0 and not result.get('error') and all(checks.get(key) is True for key in required)
        report={'passed':passed,'exit_code':code,'checks':{key:checks.get(key) is True for key in required},
                'disposable_state':True,'cloud_requests':False}
        (output/'native-model-proof.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(report))
        return 0 if passed else 1
    finally:
        job.close()


if __name__=='__main__':raise SystemExit(main())
