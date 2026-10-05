"""Authenticated coordinator HTTP host shared by desktop and browser clients."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from core import BASE_DIR
from forge_host import PairingAuthority


def create_app(core=None, service=None, auth=None, assets_dir=None):
    """Explicit ``core`` preserves the original test harness, never a production default."""
    legacy = core is not None and service is None
    if service is None:
        if legacy:
            from service import Service
            service = Service(core)
        else:
            from forge_service import ForgeService
            service = ForgeService()
    authority = auth or PairingAuthority()
    assets = Path(assets_dir or BASE_DIR / 'frontend' / 'dist').resolve()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.pairing = authority
    app.state.legacy = legacy

    def origin_allowed(request):
        try:
            host = urlsplit('http://' + request.headers.get('host', '')).hostname
            allowed = {'localhost', '127.0.0.1', '::1'}
            if legacy:
                allowed.add('testserver')
            allowed.update(x.strip().lower() for x in os.getenv('FORGE_ALLOWED_HOSTS', '').split(',') if x.strip())
            if host not in allowed:
                return False
            origin = request.headers.get('origin')
            return not origin or origin == str(request.base_url).rstrip('/')
        except ValueError:
            return False

    def token(request):
        authorization = request.headers.get('authorization', '')
        if authorization.startswith('Bearer '):
            return authorization[7:]
        return request.cookies.get('forge_session', '')

    @app.middleware('http')
    async def security(request, call_next):
        if not origin_allowed(request):
            return JSONResponse({'error': 'Origin rejected'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/api/') else 'no-cache'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
            "media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'self'")
        return response

    async def read_data(request):
        if 'application/json' not in request.headers.get('content-type', ''):
            return None, JSONResponse({'error': 'JSON required'}, status_code=415)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 12_000_000:
                return None, JSONResponse({'error': 'Request too large'}, status_code=413)
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError('Expected an object')
            return data, None
        except (ValueError, RecursionError):
            return None, JSONResponse({'error': 'Expected a JSON object'}, status_code=400)

    def authorized(request):
        return legacy or authority.valid(token(request))

    async def dispatch(action, data):
        if action.startswith('dictation_'):
            dictation = getattr(service, 'host_dictation', None)
            if dictation is None:
                from dictation import Dictation
                home = getattr(service.store, 'home', getattr(service.store, 'data_dir', Path.home() / '.forge'))
                service.host_dictation = dictation = Dictation(home)
            return await run_in_threadpool(dictation.dispatch, action.removeprefix('dictation_'), data)
        return await run_in_threadpool(service.dispatch, action, data)

    @app.post('/api/v1/pair')
    async def pair(request: Request):
        data, error = await read_data(request)
        if error is not None:
            return error
        try:
            session = authority.pair(data.get('code', ''), request.client.host if request.client else 'local')
            response = JSONResponse({'ok': True, 'expires_in': 86400})
            response.set_cookie('forge_session', session, httponly=True, samesite='strict',
                                secure=request.url.scheme == 'https', max_age=86400, path='/api/')
            return response
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=403)

    @app.post('/api/v1/unpair')
    async def unpair(request: Request):
        if not authorized(request):
            return JSONResponse({'error': 'Pair this browser in Forge Settings.'}, status_code=401)
        _, error = await read_data(request)
        if error is not None:
            return error
        authority.revoke(token(request))
        response = JSONResponse({'ok': True})
        response.delete_cookie('forge_session', path='/api/')
        return response

    @app.post('/api/v1/{action}')
    @app.post('/api/{action}')
    async def api(action: str, request: Request):
        if not authorized(request):
            return JSONResponse({'error': 'Pair this browser in Forge Settings.', 'pairing_required': True}, status_code=401)
        data, error = await read_data(request)
        if error is not None:
            return error
        try:
            return JSONResponse(await dispatch(action, data))
        except (ValueError, httpx.HTTPError) as exc:
            return JSONResponse({'error': str(exc)[:1000]}, status_code=400)

    @app.get('/api/v1/runs/{run_id}/events')
    async def events(run_id: str, request: Request, after: int = 0):
        if not authorized(request):
            return JSONResponse({'error': 'Pair this browser in Forge Settings.'}, status_code=401)
        if after < 0:
            return JSONResponse({'error': 'Invalid event cursor'}, status_code=400)
        try:
            previous = int(request.headers.get('last-event-id', '0'))
            if previous < 0:
                raise ValueError('Negative event cursor')
            after = max(after, previous)
        except ValueError:
            return JSONResponse({'error': 'Invalid event cursor'}, status_code=400)
        if after < 0:
            return JSONResponse({'error': 'Invalid event cursor'}, status_code=400)
        if 'text/event-stream' not in request.headers.get('accept', ''):
            try:
                return JSONResponse(await dispatch('poll', {'id': run_id, 'after': after}))
            except ValueError as exc:
                return JSONResponse({'error': str(exc)}, status_code=400)

        async def replay():
            cursor = after
            deadline = asyncio.get_running_loop().time() + 55
            yield 'retry: 1000\n\n'
            while asyncio.get_running_loop().time() < deadline:
                if await request.is_disconnected() or not authorized(request):
                    return
                try:
                    batch = await dispatch('poll', {'id': run_id, 'after': cursor})
                except ValueError as exc:
                    yield 'event: error\ndata: ' + json.dumps({'error': str(exc)}) + '\n\n'
                    return
                for event in batch.get('events', []):
                    sequence = int(event.get('seq', event.get('sequence', cursor + 1)))
                    if sequence > cursor:
                        cursor = sequence
                        yield 'id: ' + str(cursor) + '\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
                if batch.get('finished'):
                    yield 'event: finished\ndata: ' + json.dumps({'status': batch.get('status'), 'next_cursor': cursor}) + '\n\n'
                    return
                yield ': keepalive\n\n'
                await asyncio.sleep(.25)
        return StreamingResponse(replay(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @app.get('/api/v1/health')
    def health():
        return {'app': 'Forge', 'version': '4.0.0', 'pairing_required': not legacy}

    @app.get('/')
    def index():
        compiled = assets / 'index.html'
        if compiled.is_file():
            return FileResponse(compiled, media_type='text/html')
        if legacy:
            return HTMLResponse((BASE_DIR / 'frontend' / 'index.html').read_text(encoding='utf-8'))
        return HTMLResponse('<!doctype html><title>Forge</title><main>Frontend build missing. Run npm ci and npm run build in frontend.</main>', status_code=503)

    @app.get('/{path:path}')
    def static(path: str):
        candidate = (assets / path).resolve()
        if not candidate.is_relative_to(assets) or not candidate.is_file() or path.startswith('api/'):
            return JSONResponse({'error': 'Not found'}, status_code=404)
        return FileResponse(candidate)
    return app


if __name__ == '__main__':
    import uvicorn
    from forge_host import SingleCoordinator
    coordinator = SingleCoordinator()
    if not coordinator.acquire():
        raise SystemExit('Forge coordinator is already running.')
    application = create_app()
    code = application.state.pairing.issue()
    print('Pair this browser with this one-use code (expires in 3 minutes): ' + code['code'])
    background = getattr(application.state.service, 'start_background', None)
    if background:
        background()
    try:
        uvicorn.run(application, host=os.getenv('BIND_HOST', '127.0.0.1'), port=int(os.getenv('FORGE_PORT', '8081')))
    finally:
        callback = getattr(application.state.service, 'shutdown', None)
        if callback:
            callback()
        application.state.pairing.close()
        coordinator.close()
