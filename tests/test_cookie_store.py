import errno
import json
from pathlib import Path
import subprocess
import sys
import pytest
from downloadinux import cookie_store as store, cookiejar
from downloadinux.errors import DownloadinuxError

RID='a'*32
STAMP='2026-10-09T12:00:00Z'
FILE=dict(kind='file',path='/abs/absent.txt',revision=RID,refreshed_at=STAMP)
BROWSER=dict(kind='browser',browser='firefox',profile=None,keyring=None,container=None,revision=RID,refreshed_at=STAMP)


def fixture(root, sources=None, default=None):
    sources={'file':FILE,'browser':BROWSER} if sources is None else sources
    root.mkdir(exist_ok=True)
    for name,record in sources.items():
        parent=root/'sources'/name
        parent.mkdir(parents=True,exist_ok=True)
        (parent/(record['revision']+'.txt')).write_text(cookiejar.HEADER + '.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tCANARY_VALUE\n')
    record=dict(version=1,default=default,sources=sources)
    (root/'sources.json').write_text(json.dumps(record))
    return record


def test_example_and_exact_metadata(tmp_path):
    fixture(tmp_path,default='file')
    snapshot=store.read(tmp_path)
    assert store.list_metadata(snapshot) == dict(default='file', sources=[dict(**BROWSER,name='browser',is_default=False),dict(**FILE,name='file',is_default=True)])
    result=store.inspect(tmp_path,snapshot,'file',now=100)
    assert result == dict(source=dict(**FILE,name='file',is_default=True),total=1,session=1,expired=0,unexpired=0,
                         domains=[dict(domain='.instagram.com',total=1,session=1,expired=0,unexpired=0,session_cookie_present=True)])
    assert 'CANARY_VALUE' not in json.dumps(result)
    assert not Path(FILE['path']).exists()
    with pytest.raises(TypeError):
        snapshot.sources['new']=snapshot.sources['file']


def test_missing_no_mutation_and_empty(tmp_path):
    root=tmp_path/'missing'
    assert store.read(root) is None
    assert not root.exists()
    fixture(root,{},None)
    assert store.list_metadata(store.read(root)) == dict(default=None,sources=[])


@pytest.mark.parametrize('name',['a','a'*64])
def test_name_boundaries(tmp_path,name):
    fixture(tmp_path,{name:FILE},name)
    assert store.read(tmp_path).default == name


@pytest.mark.parametrize('name',['','a'*65,'A','_a','a/b','é','a.CANARY_VALUE'])
def test_bad_names(tmp_path,name):
    record=dict(version=1,default=None,sources={name:FILE})
    assert_bad(tmp_path,record)


def assert_bad(root,record):
    path=root/'sources.json'
    path.write_text(json.dumps(record))
    before=path.read_bytes()
    with pytest.raises(DownloadinuxError) as error:
        store.read(root)
    assert str(path) in str(error.value)
    assert 'CANARY' not in str(error.value).removeprefix(str(root / 'sources.json'))
    assert error.value.exit_status == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize('record',[None,[],1,True,{},dict(version=True,default=None,sources={}),dict(version=2,default=None,sources={}),
    dict(version=1,default=None,sources=[]),dict(version=1,default=False,sources={}),dict(version=1,default='absent',sources={}),
    dict(version=1,default=None,sources={},CANARY_VALUE='secret')])
def test_bad_registry(tmp_path,record):
    assert_bad(tmp_path,record)


@pytest.mark.parametrize('change',[dict(kind='other'),dict(revision='a'*31),dict(revision='A'*32),dict(revision=True),dict(revision='../CANARY_VALUE'),
    dict(refreshed_at='2026-02-30T12:00:00Z'),dict(refreshed_at='2026-10-09T12:00:00+00:00'),dict(refreshed_at=1),
    dict(path='relative'),dict(path='~/jar'),dict(path=True),dict(path='/CANARY_VALUE\x00'),dict(browser='firefox'),dict(CANARY_VALUE='secret')])
def test_bad_file_record(tmp_path,change):
    assert_bad(tmp_path,dict(version=1,default=None,sources={'file':dict(FILE,**change)}))


@pytest.mark.parametrize('change',[dict(browser='safari'),dict(browser=True),dict(profile=[]),dict(keyring=1),dict(container=False),dict(path='/x')])
def test_bad_browser_record(tmp_path,change):
    assert_bad(tmp_path,dict(version=1,default=None,sources={'browser':dict(BROWSER,**change)}))


@pytest.mark.parametrize('missing',list(FILE))
def test_missing_record_fields(tmp_path,missing):
    record=FILE.copy();del record[missing]
    assert_bad(tmp_path,dict(version=1,default=None,sources={'file':record}))


@pytest.mark.parametrize('data',[b'CANARY_VALUE',b'\xff',b'{"version":1,"version":1,"default":null,"sources":{}}'])
def test_bad_json(tmp_path,data):
    (tmp_path/'sources.json').write_bytes(data)
    with pytest.raises(DownloadinuxError) as error:
        store.read(tmp_path)
    assert 'CANARY' not in str(error.value).removeprefix(str(tmp_path / 'sources.json'))
    assert 'sources.json' in str(error.value)


@pytest.mark.parametrize('failure',['missing','symlink','directory','invalid'])
def test_bad_managed_material(tmp_path,failure):
    fixture(tmp_path)
    path=store.revision_path(tmp_path,'file',RID)
    path.unlink()
    if failure == 'symlink':
        path.symlink_to(store.revision_path(tmp_path,'browser',RID))
    elif failure == 'directory':path.mkdir()
    elif failure == 'invalid':path.write_text('CANARY_VALUE')
    with pytest.raises(DownloadinuxError) as error:
        store.read(tmp_path)
    assert str(path) in str(error.value)
    assert 'CANARY' not in str(error.value)


def test_managed_parent_symlink(tmp_path):
    fixture(tmp_path)
    parent=tmp_path/'sources'/'file'
    parent.rename(tmp_path/'external')
    parent.symlink_to(tmp_path/'external')
    with pytest.raises(DownloadinuxError,match='managed directory'):
        store.read(tmp_path)


def test_normalized_domain_collision_in_managed_revision(tmp_path):
    fixture(tmp_path)
    snapshot = store.read(tmp_path)
    path = store.revision_path(tmp_path, 'file', RID)
    path.write_text(cookiejar.HEADER +
                    'Example.com\tFALSE\t/\tTRUE\t0\tCANARY_NAME\tCANARY_VALUE\n' +
                    'example.com\tFALSE\t/\tTRUE\t0\tCANARY_NAME\tCANARY_OTHER\n')
    for operation in (lambda: store.read(tmp_path),
                      lambda: store.inspect(tmp_path, snapshot, 'file')):
        with pytest.raises(DownloadinuxError) as error:
            operation()
        assert f'{path}: line 3: invalid duplicate domain/path/name' in str(error.value)
        assert 'CANARY' not in str(error.value)
        assert error.value.exit_status == 1


def test_registry_os_error(tmp_path):
    (tmp_path/'sources.json').mkdir()
    with pytest.raises(DownloadinuxError,match='sources.json'):
        store.read(tmp_path)


def test_revision_os_error(tmp_path,monkeypatch):
    fixture(tmp_path)
    def refuse(path,*args):
        raise PermissionError(errno.EACCES,'Permission denied',str(path))
    monkeypatch.setattr(store.os,'open',refuse)
    with pytest.raises(DownloadinuxError,match='Permission denied') as error:
        store.read(tmp_path)
    assert RID+'.txt' in str(error.value)


def test_imports_without_host_dependencies():
    script="""
import sys
from downloadinux import cookiejar, cookie_store
assert not any(m in sys.modules for m in ('downloadinux.settings','downloadinux.config','yt_dlp','tomlkit','downloadinux.compose'))
"""
    subprocess.run([sys.executable,'-c',script],check=True)


@pytest.mark.parametrize('browser',['firefox','chrome','chromium'])
def test_browser_selectors_are_metadata(tmp_path,browser):
    record=dict(BROWSER,browser=browser,profile='named-profile',keyring='gnomekeyring',container='work')
    fixture(tmp_path,{'b':record})
    assert store.read(tmp_path).sources['b'].record() == record


def test_external_origin_removed_does_not_invalidate_copy(tmp_path):
    origin=tmp_path/'export.txt';origin.write_text(cookiejar.HEADER)
    record=dict(FILE,path=str(origin))
    fixture(tmp_path,{'file':record})
    origin.unlink()
    assert store.inspect(tmp_path,store.read(tmp_path),'file')['total'] == 1


def test_bad_source_record_type(tmp_path):
    assert_bad(tmp_path,dict(version=1,default=None,sources={'file':True}))


@pytest.mark.parametrize('missing',['version','default','sources'])
def test_missing_registry_fields(tmp_path,missing):
    record=dict(version=1,default=None,sources={});del record[missing]
    assert_bad(tmp_path,record)


@pytest.mark.parametrize('missing',list(BROWSER))
def test_missing_browser_fields(tmp_path,missing):
    record=BROWSER.copy();del record[missing]
    assert_bad(tmp_path,dict(version=1,default=None,sources={'b':record}))
