from http.cookiejar import MozillaCookieJar, HTTPONLY_ATTR
import json
from pathlib import Path
import pytest
from downloadinux import cookiejar as cj
from downloadinux.errors import DownloadinuxError


def row(domain='.instagram.com', sub='TRUE', path='/', secure='TRUE', expiry='0', name='sessionid', value='CANARY_VALUE'):
    return '\t'.join((domain, sub, path, secure, expiry, name, value))


def parse(*rows):
    return cj.parse((cj.HEADER + '\n'.join(rows) + '\n').encode(), Path('synthetic.txt'))


def semantics(jar):
    return sorted((c.domain, c.path, '' if c.value is None else c.name,
                   c.name if c.value is None else c.value, c.secure, c.expires,
                   c.discard, c.has_nonstandard_attr(HTTPONLY_ATTR)) for c in jar)


@pytest.mark.parametrize('header', ['# Netscape HTTP Cookie File', '# HTTP Cookie File'])
@pytest.mark.parametrize('ending', ['\n', '\r\n'])
@pytest.mark.parametrize('bom', ['', '\ufeff'])
def test_formats_roundtrip(tmp_path, header, ending, bom):
    rows = [header, '', '# comment', '#HttpOnly_' + row(), row('youtube.com', 'FALSE', expiry='1', name='SAPISID'),
            row('x.test', 'FALSE', expiry='', name='', value='unnamed'), row('x.test', 'FALSE', expiry='9999999999', name='named', value=''),
            row('x.test', 'FALSE', path='/empty', secure='FALSE', name='', value='')]
    material = cj.parse((bom + ending.join(rows) + ending).encode(), tmp_path/'in')
    assert len(material.jar) == 5
    text = cj.serialize(material, tmp_path/'out')
    assert semantics(cj.parse(text.encode(), tmp_path/'out').jar) == semantics(material.jar)
    target = tmp_path/'real.txt'
    target.write_text(text)
    real = MozillaCookieJar(str(target))
    real.load(ignore_discard=True, ignore_expires=True)
    assert semantics(real) == semantics(material.jar)
    real.save(ignore_discard=True, ignore_expires=True)
    assert semantics(cj.read(target).jar) == semantics(material.jar)
    assert 'CANARY_VALUE' not in repr(material)


@pytest.mark.parametrize('changes,field', [
    ({'domain':''}, 'domain'), ({'domain':'bad domain'},'domain'), ({'domain':'$bad'},'domain'),
    ({'sub':'true'},'subdomain flag'), ({'sub':'FALSE'},'domain/subdomain'),
    ({'domain':'x.test','sub':'TRUE'},'domain/subdomain'), ({'path':''},'path'), ({'path':'relative'},'path'),
    ({'secure':'1'},'secure flag'), ({'expiry':'-1'},'expiry'), ({'expiry':'1.0'},'expiry'),
    ({'expiry':'１２'},'expiry'), ({'name':'CANARY_NAME\r'},'name'), ({'value':'CANARY_VALUE\x00'},'value')])
def test_invalid_fields(changes, field):
    with pytest.raises(cj.CookieFormatError) as error:
        parse(row(**changes))
    assert 'synthetic.txt: line 2:' in str(error.value)
    assert field in str(error.value)
    assert 'CANARY' not in str(error.value)


@pytest.mark.parametrize('data,field,line', [(b'', 'header',1), (b'wrong\n', 'header',1),
    (cj.HEADER.encode()+b'\xff', 'UTF-8',2), (cj.HEADER.encode()+b'CANARY_NAME\tCANARY_VALUE\n','field count',2)])
def test_invalid_input(data,field,line):
    with pytest.raises(cj.CookieFormatError) as error:
        cj.parse(data,Path('jar'))
    assert f'jar: line {line}: invalid {field}' in str(error.value)
    assert 'CANARY' not in str(error.value)


def test_duplicates():
    with pytest.raises(cj.CookieFormatError,match='line 3: invalid duplicate'):
        parse(row(),row(value='other'))


@pytest.mark.parametrize('domain,sub', [('Example.com', 'FALSE'), ('.Example.com', 'TRUE')])
@pytest.mark.parametrize('httponly', ['', '#HttpOnly_'])
@pytest.mark.parametrize('name', ['CANARY_NAME', ''])
def test_normalized_domain_duplicates(domain, sub, httponly, name):
    with pytest.raises(cj.CookieFormatError) as error:
        parse(httponly + row(domain, sub, name=name),
              row(domain.lower(), sub, name=name, value='CANARY_OTHER'))
    assert 'synthetic.txt: line 3: invalid duplicate domain/path/name' in str(error.value)
    assert 'CANARY' not in str(error.value)


@pytest.mark.parametrize('second', [dict(path='/Case'), dict(name='canary_name')])
def test_normalized_domains_keep_case_sensitive_paths_and_names(second):
    material = parse(row('Example.com', 'FALSE', path='/case', name='CANARY_NAME'),
                     row('example.com', 'FALSE', path=second.get('path', '/case'),
                         name=second.get('name', 'CANARY_NAME')))
    assert len(material.jar) == 2
    assert {c.domain for c in material.jar} == {'example.com'}
    assert semantics(cj.parse(cj.serialize(material, Path('jar')).encode(), Path('jar')).jar) == semantics(material.jar)


@pytest.mark.parametrize('bom', [b'', b'\xef\xbb\xbf'])
@pytest.mark.parametrize('ending', [b'\n', b'\r\n'])
@pytest.mark.parametrize('line', [1, 2, 3, 4])
def test_utf8_error_lines(bom, ending, line):
    prefix = [cj.HEADER.rstrip('\n').encode(), b'# CANARY_COMMENT', b''][:line - 1]
    data = bom + b''.join(part + ending for part in prefix) + b'\xffCANARY_VALUE' + ending
    with pytest.raises(cj.CookieFormatError) as error:
        cj.parse(data, Path('synthetic.txt'))
    assert f'synthetic.txt: line {line}: invalid UTF-8' in str(error.value)
    assert 'CANARY' not in str(error.value)


def test_empty_and_counts():
    assert cj.summary(parse(),now=100) == dict(total=0,session=0,expired=0,unexpired=0,domains=[])
    material = parse(row(expiry='100'), row(path='/a',expiry='101'), row(path='/b',expiry='99'),row(path='/c',expiry=''),
                     row('a.youtube.com','FALSE',name='__Secure-3PSID',expiry='1'),
                     row('.youtube.com',name='SAPISID'),row('notyoutube.com','FALSE',name='SAPISID'),
                     row('instagram.com.evil','FALSE'),row('instagram.com','FALSE'),row('a.instagram.com','FALSE'))
    result=cj.summary(material,now=100)
    assert [result[k] for k in ['total','session','expired','unexpired']] == [10,6,3,1]
    domains=result['domains']
    assert [d['domain'] for d in domains] == sorted(d['domain'] for d in domains)
    assert {d['domain'] for d in domains if d['session_cookie_present']} == {'.instagram.com','.youtube.com','a.youtube.com','instagram.com','a.instagram.com'}
    text=json.dumps(result)
    assert all(s not in text for s in ['CANARY_VALUE','sessionid','SAPISID','__Secure-3PSID','authenticated'])
    for d in domains:
        assert d['total'] == d['session']+d['expired']+d['unexpired']


def test_actual_read_failure(tmp_path):
    with pytest.raises(DownloadinuxError,match=str(tmp_path)) as error:
        cj.read(tmp_path)
    assert error.value.exit_status == 1


@pytest.mark.parametrize('exception',[ValueError('CANARY_VALUE'),AttributeError('CANARY_NAME')])
def test_library_failure_safe(monkeypatch,exception):
    def fail(*args):
        raise exception
    monkeypatch.setattr(MozillaCookieJar,'_really_load',fail)
    with pytest.raises(cj.CookieFormatError) as error:
        parse(row())
    assert 'line 2: invalid cookie record' in str(error.value)
    assert 'CANARY' not in str(error.value)


def test_empty_name_library_key_collision_is_retained():
    material = parse(row(name='',value='named'),row(name='named',value=''))
    assert len(material.jar) == 2
    assert semantics(parse(*cj.serialize(material,Path('jar')).splitlines()[1:]).jar) == semantics(material.jar)
