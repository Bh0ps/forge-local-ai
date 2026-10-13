"""Native-only orchestration; renderer calls cannot mint backend grants."""
import threading

from forge_native_handoff import HandoffRejected, HelperTransport, NativeAuthority, unavailable


class NativeController:
    def __init__(self, manager, authority, *, binding, input_idle, fence, depart,
                 emit=lambda value: None, transport_factory=HelperTransport):
        if type(authority) is not NativeAuthority:
            raise ValueError('A native authority is required.')
        self.manager, self.authority = manager, authority
        self.binding, self.input_idle, self.fence, self.depart = binding, input_idle, fence, depart
        self.emit, self.transport_factory = emit, transport_factory
        self.operation_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.attempt = None
        self.reservation = self.prepared = self.transport = self.commit = None
        self.abort_worker = None
        self.abort_for = None
        self.stopped_receipt = None
        manager.bind_native_authority(authority)

    def _emit(self):
        try: self.emit(self.authority.status())
        except Exception: pass

    def _binding(self):
        value = self.binding()
        self.authority.update_binding(value)
        if value is None:
            raise HandoffRejected('workspace_loading')
        return value

    def begin(self, data):
        if not isinstance(data, dict) or set(data) - {'kind'} or data.get('kind', 'apply') not in ('apply', 'rollback'):
            raise HandoffRejected()
        if not self.operation_lock.acquire(blocking=False):
            raise HandoffRejected('active_work')
        try:
            if self.attempt and self.authority.status()['locked']:
                raise HandoffRejected('active_work')
            binding = self._binding()
            reservation = self.manager.native_reserve(binding, action=data.get('kind', 'apply'))
            try:
                result = self.authority.begin(binding, reservation)
            except Exception:
                self.manager.native_cancel_reservation(reservation)
                raise
            self.attempt, self.reservation = result['attempt_id'], reservation
            self.prepared = self.transport = self.commit = None
            self.stopped_receipt = None
            try:
                self._binding()
                if self.authority.status(self.attempt)['phase'] != 'checking':
                    raise HandoffRejected('workspace_changed')
            except Exception:
                self.invalidate()
                raise HandoffRejected('workspace_changed') from None
            self._emit()
            return result
        finally:
            self.operation_lock.release()

    def acknowledge(self, data):
        if not isinstance(data, dict) or data.get('attempt_id') != self.attempt:
            raise HandoffRejected()
        if not self.operation_lock.acquire(blocking=False):
            raise HandoffRejected('active_work')
        try:
            self._binding()
            grant = self.authority.acknowledge(data)
            self.prepared = self.manager.native_prepare(grant, data)
            result = self.authority.prepared(grant)
            self._emit()
            return result
        except Exception as exc:
            self.invalidate(getattr(exc, 'reason', 'preparation_failed'))
            raise HandoffRejected(getattr(exc, 'reason', 'preparation_failed')) from None
        finally:
            self.operation_lock.release()

    def confirm(self, data):
        if not isinstance(data, dict) or data.get('attempt_id') != self.attempt:
            raise HandoffRejected()
        if not self.operation_lock.acquire(blocking=False):
            raise HandoffRejected('active_work')
        try:
            self._binding()
            launch = self.authority.confirm(data)
            if self.input_idle() is not True:
                raise HandoffRejected('native_input')
            bootstrap = self.manager.native_launch(launch, self.prepared)
            self._binding()
            from forge_update_handoff import native_bootstrap_frame
            self.transport = self.transport_factory(bootstrap, native_bootstrap_frame(bootstrap))
            self.authority.register_helper(launch, self.prepared, self.transport)
            ready = self.transport.await_ready()
            self.authority.ready(launch, ready)
            fence = self.fence(self.authority.fence_request(self.attempt))
            self._binding()
            if self.input_idle() is not True:
                raise HandoffRejected('native_input')
            grant = self.authority.commit_grant(self.attempt, ready, fence)
            self.commit = grant
            if self.manager.native_commit(grant, self.prepared, ready) is not True:
                raise HandoffRejected('helper_failed')
            self._binding()
            self.authority.committed(grant)
            self.transport.commit(ready, admit=lambda: self.authority.pipe_admission(grant, self.transport))
            self._binding()
            self.authority.pipe_admission(grant, self.transport)
            if self.depart(self, grant, self.transport) is not True:
                raise HandoffRejected('workspace_changed')
            return {'attempt_id': self.attempt, 'phase': 'committed'}
        except Exception as exc:
            self.invalidate(getattr(exc, 'reason', 'helper_failed'))
            raise HandoffRejected(getattr(exc, 'reason', 'helper_failed')) from None
        finally:
            self.operation_lock.release()

    def invalidate(self, reason='workspace_changed'):
        self.authority.invalidate(reason)
        self._emit()
        if self.attempt:
            self._schedule_abort()

    def _schedule_abort(self):
        with self.state_lock:
            identifier = self.attempt
            if self.abort_worker is not None and self.abort_worker.is_alive() and self.abort_for == identifier:
                return
            self.abort_for = identifier
            self.abort_worker = threading.Thread(target=self._abort_owned, args=(identifier,),
                name='Forge-update-handoff-abort', daemon=True)
            self.abort_worker.start()

    def _abort_owned(self, identifier):
        # Invalidation already revoked future launch and actual COMMIT write.
        # Wait off the native UI thread until any snapshot/check finishes.
        with self.operation_lock:
            if identifier != self.attempt:
                return
            receipt = self.stopped_receipt
            try:
                if self.transport is not None:
                    if self.transport.stop() is not True:
                        raise HandoffRejected('helper_failed')
                    if self.authority.abort_requires_stopped(identifier):
                        if receipt is None:
                            receipt = self.authority.stopped_helper(self.transport)
                            self.stopped_receipt = receipt
                result = self.manager.native_abort(identifier, stopped_helper=receipt)
                if not isinstance(result, dict) or result.get('not_executed') is not True:
                    raise HandoffRejected('helper_failed')
                self.authority.acknowledge_abort(identifier, stopped_helper=receipt)
            except Exception:
                self.authority.invalidate('helper_failed')
            self._emit()

    def abort(self, data):
        if not isinstance(data, dict) or set(data) != {'attempt_id'} or data['attempt_id'] != self.attempt:
            raise HandoffRejected()
        if self.authority.status(self.attempt)['aborted']:
            return self.authority.status(self.attempt)
        self.invalidate()
        if self.abort_worker is not None:
            self.abort_worker.join(5)
        return self.authority.status(self.attempt)

    def status(self, data):
        if not isinstance(data, dict) or set(data) - {'attempt_id'}:
            raise HandoffRejected()
        return self.authority.status(data.get('attempt_id'))

    def dispatch(self, action, data):
        handlers = {'update_handoff_begin': self.begin, 'update_handoff_ack': self.acknowledge,
            'update_handoff_commit': self.confirm, 'update_handoff_abort': self.abort,
            'update_handoff_status': self.status}
        try:
            return handlers[action](data)
        except HandoffRejected as exc:
            if self.attempt:
                return {**self.authority.status(), 'blocked': True, 'reason': exc.reason}
            return {'blocked': True, 'phase': 'failed', 'reason': exc.reason,
                'aborted': True, 'not_executed': True}
        except Exception:
            if self.attempt:
                self.invalidate('helper_failed')
                return {**self.authority.status(), 'blocked': True, 'reason': 'helper_failed'}
            return {**unavailable(), 'reason': 'helper_failed'}
