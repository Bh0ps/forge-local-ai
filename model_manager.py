"""Same-origin browser host for the shared desktop core."""
import json
import os
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from core import Core, BASE_DIR
from runtime import Jobs
from service import Service
def create_app(core=None):
    app, engine = FastAPI(docs_url=None,redoc_url=None), core or Core()
    service = Service(engine)
    @app.get('/')
    def index(): return HTMLResponse((BASE_DIR/'frontend/index.html').read_text(encoding='utf-8'))
    @app.post('/api/{action}')
    async def api(action: str, request: Request):
        host, origin = request.headers.get('host','').split(':')[0], request.headers.get('origin')
        if host not in ('localhost','127.0.0.1','testserver') or (origin and origin != str(request.base_url).rstrip('/')):
            return JSONResponse({'error':'Origin rejected'},status_code=403)
        if 'application/json' not in request.headers.get('content-type',''):
            return JSONResponse({'error':'JSON required'},status_code=415)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>12000000: return JSONResponse({'error':'Request too large'},status_code=413)
        try:
            data = json.loads(body)
            if not isinstance(data,dict): raise ValueError('Expected an object')
            result = await run_in_threadpool(service.dispatch,action,data)
            return JSONResponse(result)
        except (ValueError,httpx.HTTPError) as exc:
            return JSONResponse({'error':str(exc)},status_code=400)
    return app
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(create_app(),host=os.getenv('BIND_HOST','127.0.0.1'),port=8081)
