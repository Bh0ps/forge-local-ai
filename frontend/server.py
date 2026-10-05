"""Compatibility entry point: run the unified server from any directory."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from model_manager import create_app
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(),host="127.0.0.1",port=8081)
