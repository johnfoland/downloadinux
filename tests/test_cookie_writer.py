from datetime import datetime, timezone
import errno
import json
import multiprocessing as mp
import os
from pathlib import Path
import stat
import time
import pytest
from downloadinux import cookiejar, cookie_store as store, cookie_writer as writer, files
from downloadinux.errors import DownloadinuxError

NOW=datetime(2026,10,9,12,tzinfo=timezone.utc)
ORIGIN=dict(kind='file',path='/external/export.txt')


def material(value='SYNTHETIC_SECRET'):
    return cookiejar.parse((cookiejar.HEADER+'.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t'+value+'\n').encode(),Path('fixture'))


def publish(root,name='a',rid='1'*32,expected=None,select=False,origin=None):
    return writer.publish(root,name,material(),origin or ORIGIN,expected=expected,select=select,revision_factory=lambda:rid,clock=lambda:NOW)


def bytes_state(root):
    snapshot=store.read(root)
    return ((root/'sources.json').read_bytes(),{str(store.revision_path(root,n,s.revision)):store.revision_path(root,n,s.revision).read_bytes() for n,s in snapshot.sources.items()})


def test_create_replace_retention_and_defaults(tmp_path):
    root=tmp_path/'store'
    assert writer.initialize(root).record() == dict(version=1,default=None,sources={})
    first=publish(root)
    assert store.read(root).default is None
    before=store.revision_path(root,'a',first.revision).read_bytes()
    second=publish(root,rid='2'*32,expected=first,origin=dict(kind='browser',browser='chrome',profile='work',keyring=None,container=None))
    assert second.revision != first.revision and second.refreshed_at == '2026-10-09T12:00:00Z'
    assert store.revision_path(root,'a',first.revision).read_bytes() == before
    assert store.load_revision(root,'a',first.revision).jar
    assert store.read(root).default is None
    old_files=sorted(root.rglob('*.txt'))
    assert writer.set_default(root,'a').default == 'a'
    assert writer.set_default(root,None).default is None
    assert sorted(root.rglob('*.txt')) == old_files
    assert store.read(root).sources['a'] == second
    third=publish(root,rid='3'*32,expected=second,select=True)
    assert store.read(root).default == 'a'
    fourth=publish(root,rid='4'*32,expected=third)
    assert store.read(root).default == 'a'
    assert writer.initialize(root).sources['a'] == fourth


def test_identical_content_new_identity_and_timestamp(tmp_path):
    first=publish(tmp_path)
    instant=datetime(2026,10,10,1,2,3,tzinfo=timezone.utc)
    second=writer.publish(tmp_path,'a',material(),ORIGIN,expected=first,clock=lambda:instant,revision_factory=lambda:'2'*32)
    assert first.refreshed_at != second.refreshed_at
    assert store.revision_path(tmp_path,'a',first.revision).read_bytes() == store.revision_path(tmp_path,'a',second.revision).read_bytes()


def test_modes_under_permissive_umask_and_no_external_chmod(tmp_path):
    external=tmp_path/'external';external.write_text(cookiejar.HEADER);external.chmod(0o644)
    root=tmp_path/'managed'
    old=os.umask(0)
    try:publish(root,origin=dict(kind='file',path=str(external)))
    finally:os.umask(old)
    for path in [root,*root.rglob('*')]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    assert stat.S_IMODE(external.stat().st_mode) == 0o644


def test_unknown_default_and_stale_rejected(tmp_path):
    first=publish(tmp_path)
    before=bytes_state(tmp_path)
    with pytest.raises(DownloadinuxError,match='not registered'):writer.set_default(tmp_path,'unknown')
    with pytest.raises(DownloadinuxError,match='changed'):publish(tmp_path,rid='2'*32)
    second=publish(tmp_path,rid='2'*32,expected=first)
    after=bytes_state(tmp_path)
    with pytest.raises(DownloadinuxError,match='changed'):publish(tmp_path,rid='3'*32,expected=first)
    assert bytes_state(tmp_path) == after
    assert before != after and store.read(tmp_path).sources['a'] == second


def test_no_clobber_collision(tmp_path):
    first=publish(tmp_path)
    before=bytes_state(tmp_path)
    with pytest.raises(DownloadinuxError,match='File exists'):
        publish(tmp_path,expected=first)
    assert bytes_state(tmp_path) == before


@pytest.mark.parametrize('stage',['lock_open','lock_mode','lock_acquire','revision_create','revision_write','revision_sync',
                                 'registry_stage','registry_write','registry_sync','registry_replace','cleanup'])
def test_precommit_failures_preserve_snapshot(tmp_path,monkeypatch,stage):
    first=publish(tmp_path,select=True)
    before=bytes_state(tmp_path)
    original_open=writer.os.open
    original_chmod=writer.os.fchmod
    original_sync=writer.os.fsync
    original_fdopen=writer.os.fdopen
    original_mkstemp=files.tempfile.mkstemp
    original_replace=writer.os.replace
    original_unlink=writer.os.unlink
    target=store.revision_path(tmp_path,'a','2'*32)
    fd_kinds={}
    def fail(path=None):
        raise OSError(errno.EIO,os.strerror(errno.EIO),None if path is None else str(path))
    def open_file(path,flags,*args,**kwargs):
        if stage=='lock_open' and Path(path).name=='.lock':fail(path)
        return original_open(path,flags,*args,**kwargs)
    def chmod(fd,mode):
        if stage=='lock_mode':fail()
        return original_chmod(fd,mode)
    def flock(handle,operation):
        fail()
    def stage_file(*args,**kwargs):
        kind='registry' if kwargs['prefix'].startswith('.sources.json.') else 'revision'
        if stage in ('revision_create','registry_stage') and kind==('revision' if stage=='revision_create' else 'registry'):
            fail(Path(kwargs['dir'])/kwargs['prefix'])
        fd,path=original_mkstemp(*args,**kwargs);fd_kinds[fd]=kind
        return fd,path
    def sync(fd):
        if stage==fd_kinds.get(fd,'')+'_sync':fail()
        return original_sync(fd)
    class FailedWrite:
        def __init__(self,handle):self.handle=handle
        def __enter__(self):return self
        def __exit__(self,*args):return self.handle.__exit__(*args)
        def fileno(self):return self.handle.fileno()
        def write(self,text):fail()
    def fdopen(fd,*args,**kwargs):
        handle=original_fdopen(fd,*args,**kwargs)
        return FailedWrite(handle) if stage==fd_kinds.get(fd,'')+'_write' else handle
    def replace(source,destination):
        if Path(destination).name=='sources.json' and stage in ('registry_replace','cleanup'):fail(source)
        return original_replace(source,destination)
    def unlink(path,*args,**kwargs):
        if stage=='cleanup' and Path(path).name.startswith('.sources.json.'):
            raise PermissionError(errno.EACCES,os.strerror(errno.EACCES),str(path))
        return original_unlink(path,*args,**kwargs)
    monkeypatch.setattr(writer.os,'open',open_file)
    monkeypatch.setattr(writer.os,'fchmod',chmod)
    if stage=='lock_acquire':monkeypatch.setattr(writer.fcntl,'flock',flock)
    monkeypatch.setattr(files.tempfile,'mkstemp',stage_file)
    monkeypatch.setattr(writer.os,'fsync',sync)
    monkeypatch.setattr(writer.os,'fdopen',fdopen)
    monkeypatch.setattr(writer.os,'replace',replace)
    monkeypatch.setattr(writer.os,'unlink',unlink)
    with pytest.raises(DownloadinuxError) as error:
        publish(tmp_path,rid='2'*32,expected=first)
    assert 'SYNTHETIC_SECRET' not in str(error.value)
    expected_path=tmp_path/'.lock' if stage.startswith('lock') else tmp_path/'sources.json' if stage.startswith('registry') or stage=='cleanup' else target
    assert str(expected_path) in str(error.value)
    assert bytes_state(tmp_path)==before
    residue=list(tmp_path.glob('.sources.json.*.tmp'))
    assert bool(residue) == (stage=='cleanup')
    if stage=='cleanup':assert 'Permission denied' in str(error.value)
    # Completed unreferenced copies are safe; only pre-copy failures lack one.
    assert target.exists() == (stage.startswith('registry') or stage=='cleanup')


def _race(root,name,expected,rid,ready,start,result):
    ready.set()
    if not start.wait(15):raise RuntimeError('barrier timed out')
    try:
        source=publish(Path(root),name,rid,expected)
        result.put(('ok',source.revision))
    except DownloadinuxError as error:result.put(('error',str(error)))


@pytest.mark.parametrize('refresh',[False,True])
def test_competing_writers_one_winner(tmp_path,refresh):
    ctx=mp.get_context('spawn')
    expected=publish(tmp_path) if refresh else None
    start=ctx.Event();result=ctx.Queue();ready=[ctx.Event(),ctx.Event()]
    processes=[ctx.Process(target=_race,args=(str(tmp_path),'a',expected,str(i)*32,ready[i-2],start,result)) for i in (2,3)]
    for p in processes:p.start()
    try:
        assert all(event.wait(15) for event in ready)
        start.set()
        outcomes=[result.get(timeout=15) for _ in processes]
        assert sorted(x[0] for x in outcomes)==['error','ok']
        assert store.read(tmp_path).sources['a'].revision == next(x[1] for x in outcomes if x[0]=='ok')
    finally:
        start.set()
        for p in processes:
            p.join(15)
            if p.is_alive():p.kill();p.join()
            assert p.exitcode == 0


def _hold_lock(root,acquired,release):
    with writer.locked(Path(root)):
        acquired.set()
        if not release.wait(15):raise RuntimeError('release timed out')


def _second_lock(root,attempting,acquired):
    attempting.set()
    with writer.locked(Path(root)):acquired.set()


def test_mutual_exclusion(tmp_path):
    ctx=mp.get_context('spawn')
    first=ctx.Event();release=ctx.Event();attempt=ctx.Event();second=ctx.Event()
    p=ctx.Process(target=_hold_lock,args=(str(tmp_path),first,release))
    q=ctx.Process(target=_second_lock,args=(str(tmp_path),attempt,second))
    p.start()
    try:
        assert first.wait(15)
        q.start();assert attempt.wait(15)
        assert not second.wait(.2)
        release.set();assert second.wait(15)
    finally:
        release.set()
        for process in (p,q):
            if process.pid is not None:
                process.join(15)
                if process.is_alive():process.kill();process.join()
                assert process.exitcode==0


def test_reread_preserves_unrelated_changes(tmp_path):
    first=publish(tmp_path)
    ctx=mp.get_context('spawn');ready=ctx.Event();start=ctx.Event();result=ctx.Queue()
    p=ctx.Process(target=_race,args=(str(tmp_path),'a',first,'2'*32,ready,start,result));p.start()
    try:
        assert ready.wait(15)
        other=publish(tmp_path,'b','3'*32,select=True)
        start.set();assert result.get(timeout=15)[0]=='ok'
        snapshot=store.read(tmp_path)
        assert snapshot.default=='b' and snapshot.sources['b']==other
        assert snapshot.sources['a'].revision=='2'*32
    finally:
        start.set();p.join(15)
        if p.is_alive():p.kill();p.join()
        assert p.exitcode==0


def _interrupted(root,expected,reached):
    original=writer._commit
    def pause(*args,**kwargs):
        reached.set()
        # Parent kills this writer at the exact pre-registry-commit boundary.
        mp.get_context('spawn').Event().wait(60)
        return original(*args,**kwargs)
    writer._commit=pause
    publish(Path(root),rid='2'*32,expected=expected)


def test_killed_writer_retains_old_snapshot_and_releases_lock(tmp_path):
    first=publish(tmp_path,select=True);before=bytes_state(tmp_path)
    ctx=mp.get_context('spawn');reached=ctx.Event()
    p=ctx.Process(target=_interrupted,args=(str(tmp_path),first,reached));p.start()
    try:
        assert reached.wait(15)
        assert store.revision_path(tmp_path,'a','2'*32).exists()
        assert bytes_state(tmp_path)==before
        p.kill();p.join(15);assert not p.is_alive()
        assert bytes_state(tmp_path)==before
        assert publish(tmp_path,rid='3'*32,expected=first).revision=='3'*32
        assert store.revision_path(tmp_path,'a','2'*32).exists()
    finally:
        if p.is_alive():p.kill();p.join()


def _many_publications(root,expected,start,done,result):
    try:
        if not start.wait(15):raise RuntimeError('barrier timed out')
        for i in range(2,22):expected=publish(Path(root),rid=f'{i:032x}',expected=expected)
        result.put('ok')
    except Exception as error:result.put(type(error).__name__)
    finally:done.set()


def test_readers_observe_complete_snapshots_during_publication(tmp_path):
    first=publish(tmp_path);ctx=mp.get_context('spawn');start=ctx.Event();done=ctx.Event();result=ctx.Queue()
    p=ctx.Process(target=_many_publications,args=(str(tmp_path),first,start,done,result));p.start()
    observed=set()
    try:
        start.set()
        deadline=time.monotonic()+15
        while not done.is_set() and time.monotonic()<deadline:
            snapshot=store.read(tmp_path)
            source=snapshot.sources['a']
            assert store.inspect(tmp_path,snapshot,'a')['total']==1
            observed.add(source.revision)
        assert done.is_set()
        assert result.get(timeout=15)=='ok'
        assert store.read(tmp_path).sources['a'].revision==f'{21:032x}'
        assert first.revision in observed
    finally:
        start.set();p.join(15)
        if p.is_alive():p.kill();p.join()
        assert p.exitcode==0


def test_real_lock_failure_and_managed_symlink(tmp_path):
    root=tmp_path/'store';root.mkdir();(root/'.lock').mkdir()
    with pytest.raises(DownloadinuxError,match='.lock'):writer.initialize(root)
    (root/'.lock').rmdir()
    external=tmp_path/'external';external.mkdir();external.chmod(0o755)
    (root/'sources').symlink_to(external)
    with pytest.raises(DownloadinuxError,match='managed directory'):publish(root)
    assert not list(external.iterdir()) and stat.S_IMODE(external.stat().st_mode)==0o755


def test_configured_root_symlink(tmp_path):
    actual=tmp_path/'actual';actual.mkdir();root=tmp_path/'root';root.symlink_to(actual)
    publish(root)
    assert store.read(root).sources['a'].revision=='1'*32


def _controlled_publications(root,expected,connection):
    real_write=writer._write
    real_commit=writer._commit
    def write(path,*args,**kwargs):
        real_write(path,*args,**kwargs)
        if path.suffix=='.txt':
            connection.send('revision')
            assert connection.poll(15) and connection.recv()=='continue'
    def commit(*args,**kwargs):
        snapshot=real_commit(*args,**kwargs)
        connection.send('registry')
        assert connection.poll(15) and connection.recv()=='continue'
        return snapshot
    writer._write=write;writer._commit=commit
    for i in (2,3,4):
        expected=publish(Path(root),rid=str(i)*32,expected=expected)


def test_read_across_explicit_commit_barriers(tmp_path):
    first=publish(tmp_path);ctx=mp.get_context('spawn');parent,child=ctx.Pipe()
    p=ctx.Process(target=_controlled_publications,args=(str(tmp_path),first,child));p.start()
    try:
        previous=first.revision
        for i in (2,3,4):
            assert parent.poll(15) and parent.recv()=='revision'
            old=store.read(tmp_path)
            assert old.sources['a'].revision==previous
            assert store.inspect(tmp_path,old,'a')['total']==1
            parent.send('continue')
            assert parent.poll(15) and parent.recv()=='registry'
            new=store.read(tmp_path)
            assert new.sources['a'].revision==str(i)*32
            assert store.inspect(tmp_path,new,'a')['total']==1
            # Retained old snapshots remain fully usable after the commit.
            assert store.inspect(tmp_path,old,'a')['total']==1
            previous=str(i)*32;parent.send('continue')
    finally:
        p.join(15)
        if p.is_alive():p.kill();p.join()
        parent.close();child.close()
        assert p.exitcode==0


def test_failed_source_and_default_switch_is_atomic(tmp_path,monkeypatch):
    publish(tmp_path,select=True);before=bytes_state(tmp_path)
    real_replace=writer.os.replace
    def refuse(source,destination):
        if Path(destination).name=='sources.json':
            raise OSError(errno.EIO,os.strerror(errno.EIO),str(source),None,str(destination))
        return real_replace(source,destination)
    monkeypatch.setattr(writer.os,'replace',refuse)
    with pytest.raises(DownloadinuxError,match='sources.json'):
        publish(tmp_path,'b','2'*32,select=True)
    assert bytes_state(tmp_path)==before
    assert 'b' not in store.read(tmp_path).sources
    assert store.revision_path(tmp_path,'b','2'*32).exists()


@pytest.mark.parametrize('clock',[lambda:datetime(2026,10,9),lambda:'CANARY'])
def test_invalid_clock_no_registry_publication(tmp_path,clock):
    with pytest.raises(DownloadinuxError,match='refresh clock'):
        writer.publish(tmp_path,'a',material(),ORIGIN,expected=None,clock=clock)
    assert not (tmp_path/'sources.json').exists()


@pytest.mark.parametrize('origin',[dict(ORIGIN,revision='1'*32),dict(ORIGIN,refreshed_at='invalid'),dict(ORIGIN,unexpected='CANARY')])
def test_invalid_origin_no_publication(tmp_path,origin):
    with pytest.raises(DownloadinuxError) as error:
        publish(tmp_path,origin=origin)
    assert 'CANARY' not in str(error.value)
    assert not (tmp_path/'sources.json').exists()


def test_first_registry_publication_uses_no_clobber(tmp_path,monkeypatch):
    real=files.write_atomic;calls=[]
    def record(path,text,mode,*,replace=True):
        calls.append((path.name,replace))
        return real(path,text,mode,replace=replace)
    monkeypatch.setattr(files,'write_atomic',record)
    first=publish(tmp_path)
    publish(tmp_path,rid='2'*32,expected=first)
    assert [replace for name,replace in calls if name=='sources.json']==[False,True]


def test_empty_material_publish_and_default_only_failure(tmp_path,monkeypatch):
    empty=cookiejar.parse(cookiejar.HEADER.encode(),Path('empty'))
    source=writer.publish(tmp_path,'empty',empty,ORIGIN,expected=None)
    assert store.inspect(tmp_path,store.read(tmp_path),'empty')['total']==0
    before=bytes_state(tmp_path)
    def fail(path,text,mode,*,replace=True):
        raise PermissionError(errno.EACCES,os.strerror(errno.EACCES),str(path))
    monkeypatch.setattr(files,'write_atomic',fail)
    with pytest.raises(DownloadinuxError,match='sources.json'):writer.set_default(tmp_path,'empty')
    assert bytes_state(tmp_path)==before
    assert store.read(tmp_path).sources['empty']==source


@pytest.mark.parametrize('migration',[False,True])
def test_source_writer_reuses_portable_reservation_fallback(tmp_path,monkeypatch,migration):
    def no_links(source,destination):
        raise OSError(errno.EOPNOTSUPP,os.strerror(errno.EOPNOTSUPP),str(source),None,str(destination))
    monkeypatch.setattr(files.os,'link',no_links)
    if migration:
        (tmp_path/'cookies.txt').write_text(cookiejar.serialize(material(),Path('fixture')))
        writer.ensure_store(tmp_path)
    else:
        publish(tmp_path,select=True)
    assert store.read(tmp_path).default==('legacy' if migration else 'a')
    assert all(stat.S_IMODE(p.stat().st_mode)==0o600 for p in tmp_path.rglob('*') if p.is_file() and p.name!='cookies.txt')
    assert not list(tmp_path.rglob('*.tmp'))


@pytest.mark.parametrize('migration',[False,True])
def test_unverifiable_reservation_is_preserved_and_reported(tmp_path,monkeypatch,migration):
    if migration:
        (tmp_path/'cookies.txt').write_text(cookiejar.serialize(material(),Path('fixture')))
        before=None
    else:
        first=publish(tmp_path);before=bytes_state(tmp_path)
    target=store.revision_path(tmp_path,'legacy' if migration else 'a','2'*32)
    real_replace=files.os.replace;real_lstat=Path.lstat
    def no_links(source,destination):
        raise OSError(errno.EOPNOTSUPP,os.strerror(errno.EOPNOTSUPP),str(source),None,str(destination))
    def refuse_replace(source,destination):
        if Path(destination)==target:
            raise OSError(errno.EIO,os.strerror(errno.EIO),str(source),None,str(destination))
        return real_replace(source,destination)
    def refuse_identity(path,*args,**kwargs):
        if path==target:
            raise PermissionError(errno.EACCES,os.strerror(errno.EACCES),str(path))
        return real_lstat(path,*args,**kwargs)
    monkeypatch.setattr(files.os,'link',no_links)
    monkeypatch.setattr(files.os,'replace',refuse_replace)
    monkeypatch.setattr(Path,'lstat',refuse_identity)
    with pytest.raises(DownloadinuxError) as error:
        if migration:writer.ensure_store(tmp_path,revision_factory=lambda:'2'*32)
        else:publish(tmp_path,rid='2'*32,expected=first)
    assert str(target) in str(error.value) and 'Permission denied' in str(error.value)
    assert stat.S_IMODE(target.stat().st_mode)==0
    if migration:assert store.read(tmp_path) is None
    else:assert bytes_state(tmp_path)==before
    assert not list(target.parent.glob('*.tmp'))


@pytest.mark.parametrize('expiry',[9007199254740993,10**400])
def test_publication_preserves_exact_integer_expiry(tmp_path,expiry):
    jar=cookiejar.parse((cookiejar.HEADER+f'x.test\tFALSE\t/\tFALSE\t{expiry}\tn\tv\n').encode(),Path('input'))
    source=writer.publish(tmp_path,'a',jar,ORIGIN,expected=None)
    loaded=store.load_revision(tmp_path,'a',source.revision)
    assert next(iter(loaded.jar)).expires == expiry
    assert cookiejar.summary(loaded,now=expiry-1)['unexpired']==1
    assert cookiejar.summary(loaded,now=expiry)['expired']==1
