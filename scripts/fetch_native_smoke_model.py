"""Fetch one public pinned test model outside the application package."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from forge_native_download import pinned_download
from forge_native_smoke import MODEL

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args()
    pinned_download(MODEL,MODEL,args.destination)
    print('The pinned disposable native smoke model was verified.')
