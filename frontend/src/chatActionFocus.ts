import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { updateReadiness } from './updateReadiness';

interface ChatFocusScope {
  page: string; chatId: string | null; projectId: string | null; runId?: string;
  navigation: number; chatGeneration: number; workspaceGeneration: number;
  transport: unknown; bridge: unknown; hud: boolean; peek: boolean; visible: boolean;
}
interface FocusRequest {
  kind: 'steer' | 'history'; scope: ChatFocusScope; origin: HTMLElement;
  disabledBlur: boolean; destination?: () => HTMLElement | null;
}
const sameScope = (left: ChatFocusScope, right: ChatFocusScope) =>
  left.page === right.page && left.chatId === right.chatId && left.projectId === right.projectId && left.runId === right.runId
  && left.navigation === right.navigation && left.chatGeneration === right.chatGeneration && left.workspaceGeneration === right.workspaceGeneration
  && left.transport === right.transport && left.bridge === right.bridge && left.hud === right.hud && left.peek === right.peek;
const available = (scope: ChatFocusScope) => scope.page === 'chat' && Boolean(scope.chatId) && scope.visible
  && document.visibilityState !== 'hidden' && document.hasFocus() && updateReadiness.canEdit();

/** One explicit action may restore its own local focus after an accepted UI commit. */
export function useChatActionFocus(getScope: () => ChatFocusScope) {
  const readScope = useRef(getScope); readScope.current = getScope;
  const pending = useRef<FocusRequest | null>(null);
  const [revision, setRevision] = useState(0);
  const retire = () => { pending.current = null; };
  useEffect(() => {
    const focus = (event: FocusEvent) => {
      if (event.target !== document.body && event.target !== pending.current?.origin) retire();
    };
    const blur = (event: FocusEvent) => {
      const request = pending.current;
      if (request && event.target === request.origin && (!event.relatedTarget || event.relatedTarget === document.body)) {
        if (request.origin.matches(':disabled')) request.disabledBlur = true;
        else retire();
      }
    };
    document.addEventListener('focusin', focus, true); document.addEventListener('focusout', blur, true);
    for (const name of ['pointerdown', 'keydown', 'click', 'input', 'change', 'wheel', 'touchmove', 'scroll', 'visibilitychange']) document.addEventListener(name, retire, true);
    for (const name of ['blur', 'forge:navigate', 'forge:visibility', 'pywebviewready']) window.addEventListener(name, retire);
    const unsubscribe = updateReadiness.subscribe(() => { if (!updateReadiness.canEdit()) retire(); });
    return () => {
      retire(); unsubscribe(); document.removeEventListener('focusin', focus, true); document.removeEventListener('focusout', blur, true);
      for (const name of ['pointerdown', 'keydown', 'click', 'input', 'change', 'wheel', 'touchmove', 'scroll', 'visibilitychange']) document.removeEventListener(name, retire, true);
      for (const name of ['blur', 'forge:navigate', 'forge:visibility', 'pywebviewready']) window.removeEventListener(name, retire);
    };
  }, []);
  useLayoutEffect(() => {
    const request = pending.current;
    if (!request) return;
    const current = readScope.current();
    if (!sameScope(request.scope, current) || !available(current)) { retire(); return; }
    const active = document.activeElement;
    if (active === document.body && request.origin.matches(':disabled')) request.disabledBlur = true;
    if (!request.destination) return;
    retire();
    if (active !== request.origin && (active !== document.body || !request.disabledBlur && !(request.kind === 'history' && !request.origin.isConnected))) return;
    const destination = request.destination();
    if (destination?.isConnected && !destination.matches(':disabled') && !destination.closest('[inert], [data-visibility="hidden"]')) destination.focus({preventScroll:true});
  }, [revision, getScope]);
  return {
    admit(kind: FocusRequest['kind'], origin?: HTMLElement): FocusRequest | null {
      retire();
      const scope = readScope.current();
      if (!origin?.isConnected || document.activeElement !== origin || !available(scope) || kind === 'steer' && !scope.runId) return null;
      const request: FocusRequest = {kind, scope, origin, disabledBlur:false}; pending.current = request;
      return request;
    },
    complete(request: FocusRequest | null, destination: () => HTMLElement | null) {
      if (!request || pending.current !== request) return;
      request.destination = destination; setRevision(value => value + 1);
    },
  };
}
