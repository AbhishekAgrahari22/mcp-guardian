import json
import subprocess
import threading
import time

import pytest

from guardian.broker import ApprovalStore
from guardian import mac_ui
from guardian.desktop_config import update


def item():
    return {'source':'test','server':'test','method':'tools/call','request_id':7,'tool':'create_file','params':{'name':'safe.txt'}}


def test_config_merge_remove(tmp_path):
    path = tmp_path / 'config.json'
    original = {'preferences': {'hello': True}, 'mcpServers': {'other': {'command':'test'}}}
    path.write_text(json.dumps(original))
    update(path=path)
    data = json.loads(path.read_text())
    assert data['preferences'] == original['preferences'] and 'other' in data['mcpServers']
    assert 'guardian-demo' in data['mcpServers']
    update(remove=True, path=path)
    assert json.loads(path.read_text()) == original
    assert len(list(tmp_path.glob('*.bak'))) == 2


@pytest.mark.parametrize('reply,code,expected', [('allow',0,'allow'),('block',0,'block'),('allow',1,'block'),('unexpected',0,'block')])
def test_native_review_decisions(tmp_path, monkeypatch, reply, code, expected):
    store = ApprovalStore(2, 5, tmp_path/'audit')
    monkeypatch.setattr(mac_ui,'STATE',tmp_path)
    class Process:
        returncode = code
        def __init__(self, argv, **kwargs):
            assert argv[0] == '/usr/bin/osascript'
            assert 'safe.txt' not in str(argv)
            assert 'safe.txt' in open(argv[2]).read()
        def communicate(self, timeout): return reply, ''
        def poll(self): return self.returncode
    monkeypatch.setattr(subprocess,'Popen',Process)
    try:
        key=store.create(item()); mac_ui.review(store,store.pending()[0],threading.Event())
        assert store.status(key)==expected
        assert not list(tmp_path.glob('guardian-dialog-*'))
    finally: store.close()


def test_native_rejects_unreviewable_request(tmp_path, monkeypatch):
    store=ApprovalStore(2,5,tmp_path/'audit')
    monkeypatch.setattr(mac_ui,'STATE',tmp_path)
    data=item(); data['params']['big']='x'*2000
    try:
        key=store.create(data); mac_ui.review(store,store.pending()[0],threading.Event())
        assert store.status(key)=='block'
    finally: store.close()
