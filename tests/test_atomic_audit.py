"""Filesystem publication failure tests; no terminal/account access."""
import json
import os
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest

from engine import execution_audit as audit


def windows_lock():
    error=PermissionError('simulated Windows sharing/access lock')
    error.winerror=5
    return error


def test_atomic_write_retries_only_rename_and_preserves_json(tmp_path,monkeypatch):
    path=tmp_path/'state.json'
    path.write_text('{"old":true}')
    real=Path.replace
    seen=[]
    def replace(source,target):
        seen.append(source)
        assert json.loads(target.read_text())=={'old':True}
        if len(seen)<3:
            raise windows_lock()
        return real(source,target)
    wait=Mock()
    monkeypatch.setattr(Path,'replace',replace)
    monkeypatch.setattr(audit.time,'sleep',wait)
    audit.atomic_json(path,{'new':[1,2,3]})
    assert json.loads(path.read_text())=={'new':[1,2,3]}
    assert len(set(seen))==1 and wait.call_count==2
    assert not list(tmp_path.glob('*.tmp'))


def test_persistent_lock_raises_and_keeps_old_state(tmp_path,monkeypatch):
    path=tmp_path/'state.json'
    path.write_text('{"old":true}')
    call=Mock(side_effect=windows_lock())
    monkeypatch.setattr(Path,'replace',call)
    monkeypatch.setattr(audit.time,'sleep',Mock())
    with pytest.raises(PermissionError):
        audit.atomic_json(path,{'new':True})
    assert call.call_count==6
    assert json.loads(path.read_text())=={'old':True}
    assert not list(tmp_path.glob('*.tmp'))


def test_nonsharing_error_is_not_retried(tmp_path,monkeypatch):
    call=Mock(side_effect=OSError('disk failure'))
    monkeypatch.setattr(Path,'replace',call)
    monkeypatch.setattr(audit.time,'sleep',Mock())
    with pytest.raises(OSError,match='disk failure'):
        audit.atomic_json(tmp_path/'state.json',{'new':True})
    assert call.call_count==1
    audit.time.sleep.assert_not_called()


def test_writers_have_separate_temporary_files(tmp_path,monkeypatch):
    path=tmp_path/'state.json'
    real=Path.replace
    names=[]
    def publish(source,target):
        names.append(source.name)
        return real(source,target)
    monkeypatch.setattr(Path,'replace',publish)
    audit.atomic_json(path,{'n':1})
    audit.atomic_json(path,{'n':2})
    assert len(set(names))==2


def test_invalid_payload_leaves_old_json_intact(tmp_path):
    path=tmp_path/'state.json'
    audit.atomic_json(path,{'n':1})
    with pytest.raises(ValueError):
        audit.atomic_json(path,{'n':float('nan')})
    assert json.loads(path.read_text())=={'n':1}
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.skipif(os.name!='nt',reason='Actual Windows file-sharing semantics')
def test_actual_windows_reader_lock_releases_then_publish_succeeds(tmp_path):
    path=tmp_path/'state.json'
    audit.atomic_json(path,{'n':1})
    reader=path.open('r',encoding='utf-8')
    timer=threading.Timer(.06,reader.close)
    timer.start()
    try:
        audit.atomic_json(path,{'n':2})
    finally:
        timer.join()
        reader.close()
    assert json.loads(path.read_text())=={'n':2}
