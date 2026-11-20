"""Approval broker clients. No local UI or network failure can auto-approve."""
import asyncio
import json
import logging
import time
import urllib.request
from .protocol import encode


log = logging.getLogger(__name__)


class ApprovalClient:
    def __init__(self, cfg):
        self.url = f"http://127.0.0.1:{cfg['broker_port']}/requests"
        self.headers = {'X-Guardian-Local-Key':cfg['broker_key'], 'Content-Type':'application/json'}
        self.timeout = cfg['timeout']
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method, url, data=None):
        with self.opener.open(urllib.request.Request(url, data=data, headers=self.headers, method=method), timeout=2) as r:
            return json.load(r)

    def wait(self, info, stop):
        key = None
        try:
            if stop.is_set(): return False
            key = self._call('POST', self.url, encode(info))['id']
            deadline = time.monotonic() + self.timeout
            while not stop.is_set() and time.monotonic() < deadline:
                decision = self._call('GET', self.url+'/'+key)['decision']
                if decision != 'pending': return decision == 'allow' and not stop.is_set()
                stop.wait(.2)
            return False
        except Exception as exc:
            log.warning("Approval broker unavailable or communication failed: %s", type(exc).__name__)
            return False
        finally:
            if key:
                try: self._call('DELETE', self.url+'/'+key)
                except Exception: pass


class AsyncApprovalClient:
    def __init__(self, cfg, session):
        self.url = f"http://127.0.0.1:{cfg['broker_port']}/requests"
        self.headers = {'X-Guardian-Local-Key':cfg['broker_key'], 'Content-Type':'application/json'}
        self.timeout, self.session = cfg['timeout'], session

    async def wait(self, info, stop):
        key = None
        try:
            if stop.is_set(): return False
            async with self.session.post(self.url, data=encode(info), headers=self.headers, timeout=2) as r:
                r.raise_for_status()
                key = (await r.json())['id']
            deadline = time.monotonic()+self.timeout
            while not stop.is_set() and time.monotonic() < deadline:
                async with self.session.get(self.url+'/'+key, headers=self.headers, timeout=2) as r:
                    r.raise_for_status()
                    decision = (await r.json())['decision']
                if decision != 'pending': return decision == 'allow' and not stop.is_set()
                try: await asyncio.wait_for(stop.wait(), .2)
                except asyncio.TimeoutError: pass
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Approval broker unavailable or communication failed: %s", type(exc).__name__)
            return False
        finally:
            if key:
                try:
                    async with self.session.delete(self.url+'/'+key, headers=self.headers, timeout=2): pass
                except Exception: pass
