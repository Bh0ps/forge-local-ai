"""Streaming estimates use producer time, independent of UI buffering/replays."""
from collections import deque
import math
import time


class TokenSpeedEstimator:
    def __init__(self,window=5,minimum=1.5,clock=time.monotonic):
        self.window=window; self.minimum=minimum; self.clock=clock; self.samples=deque(); self.total=0.0; self.first=None

    def observe(self,text,now=None):
        if not text: return
        now=self.clock() if now is None else now
        if not math.isfinite(now): return
        if self.first is None: self.first=now
        self.total+=len(text.encode('utf-8'))/4
        self.samples.append((now,self.total))
        # Retain the last point before the cutoff for a stable full interval.
        while len(self.samples)>2 and self.samples[1][0]<now-self.window: self.samples.popleft()

    def estimate(self,now=None):
        now=self.clock() if now is None else now
        if self.first is None or now-self.first<self.minimum or len(self.samples)<2: return None
        first=self.samples[0]
        duration=now-first[0]
        if duration<self.minimum: return None
        return max(0,(self.total-first[1])/duration)
