#!/usr/bin/env python3
"""Single-file Shanghai Telecom IPTV configure/fetch/convert reference CLI.

Protocol evidence: pcg562240/pcg-iptv shctiptv.py at
38653f0bd722c4dececae434efa3593be97e8aca. That tree has no license;
this implementation does not vendor its AES or client implementation.
"""
import argparse
from contextlib import contextmanager
import fcntl
import getpass
import gzip
import io
import hashlib
from html import unescape
from html.parser import HTMLParser
import http.client
from http.cookiejar import CookieJar
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse as urlparse
import urllib.request as request

DEFAULT_ORIGIN = 'http://222.68.208.73:7001'
# Confirmed authentication hop, not an entire ISP subnet. Additional destinations
# require the user to explicitly allow an origin in the private configuration.
KNOWN_ORIGINS = {'http://124.75.29.176:7001', 'http://218.83.165.50:8084', 'http://218.83.165.36:8084'}
MAX_BODY = 8 * 1024 * 1024


class SafeError(Exception):
    """Messages contain only developer-authored text, never provider data."""


def regular(path, missing=False):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if missing:
            return None
        raise SafeError('所需文件不存在') from None
    if not stat.S_ISREG(info.st_mode):
        raise SafeError('拒绝符号链接或非普通文件')
    return info


def safe_parents(path):
    path = Path(os.path.abspath(path))
    for parent in (path, *path.parents):
        if parent.is_symlink() or not parent.is_dir():
            raise SafeError('目录必须存在且不能经过符号链接')
    return path


def private_dir(path):
    path = Path(path)
    safe_parents(path.parent)
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
    safe_parents(path)
    if path.stat().st_uid != os.geteuid():
        raise SafeError('私有目录所有者不匹配')
    path.chmod(0o700)


def read_json(path):
    regular(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SafeError('拒绝非普通文件')
        return json.load(stream, object_pairs_hook=unique_pairs)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SafeError('重复字段，拒绝不完整解析')
        result[key] = value
    return result


def atomic_json(path, value):
    path = Path(path)
    safe_parents(path.parent)
    regular(path, missing=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    name = None
    try:
        fd, name = tempfile.mkstemp(prefix='.iptv-', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if read_json(name) != value:
            raise SafeError('临时文件校验失败')
        regular(path, missing=True)
        os.replace(name, path)
        name = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if read_json(path) != value or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise SafeError('写入后校验失败')
    finally:
        if name is not None:
            os.unlink(name)


@contextmanager
def lock(directory, filename='auth.lock'):
    private_dir(directory)
    path = Path(directory) / filename
    regular(path, missing=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SafeError('锁不是普通文件')
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SafeError('配置或刷新正在执行，请稍后重试') from None
        yield
    finally:
        os.close(fd)


def aes_encrypt(data, key):
    if len(key) != 16:
        raise SafeError('AES key 长度错误')
    # -K takes hex text representing the 16 binary digest bytes. Identity and
    # plaintext travel only on stdin; this protocol key is public, not identity.
    result = subprocess.run(['openssl', 'enc', '-aes-128-ecb', '-K', key.hex(), '-nosalt'],
                            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=10, check=True)
    return result.stdout


def authenticator(token, config, ip):
    payload = dict(Randon=f'{secrets.randbelow(100000000):08d}', EncryToken=token,
                   UserID=config['userid'], SN=config['sn'],
                   IP=','.join(f'{int(x):03d}' for x in ip.split('.')),
                   MAC=config['mac'].upper(), MagicCode='CTC', UpdateTime='20230301175307')
    plain = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return aes_encrypt(plain, hashlib.md5(b'123456').digest()).hex()


class Forms(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.current = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            if self.current is not None:
                raise SafeError('认证表单结构无效')
            self.current = (attrs, {})
            self.forms.append(self.current)
        elif tag == 'input' and self.current is not None:
            name = attrs.get('name') or attrs.get('id')
            if name:
                if name in self.current[1]:
                    raise SafeError('认证表单字段重复')
                self.current[1][name] = attrs.get('value') or ''

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None


def form(text, name=None):
    forms = Forms(text).forms
    matches = [f for f in forms if name is None or f[0].get('id') == name or f[0].get('name') == name]
    if len(matches) != 1:
        raise SafeError('缺少唯一认证表单')
    return matches[0]


def js_string(text, start):
    """Decode a JS quoted literal without evaluating any JavaScript."""
    quote = text[start]
    if quote not in "\"'":
        raise SafeError('频道数组必须由字符串组成')
    out = []
    i = start + 1
    escapes = {'n':'\n', 'r':'\r', 't':'\t', 'b':'\b', 'f':'\f', '\\':'\\', '/':'/', "'":"'", '"':'"'}
    while i < len(text):
        ch = text[i]; i += 1
        if ch == quote:
            value = ''.join(out)
            try:
                value = value.encode('utf-16', 'surrogatepass').decode('utf-16')
            except UnicodeError:
                raise SafeError('字符串 Unicode 转义无效') from None
            return value, i
        if ch in '\r\n':
            raise SafeError('字符串包含未转义换行')
        if ch == '\\':
            if i >= len(text):
                break
            ch = text[i]; i += 1
            if ch in escapes:
                ch = escapes[ch]
            elif ch in ('\n', '\r'):
                if ch == '\r' and i < len(text) and text[i] == '\n': i += 1
                continue
            elif ch in ('u', 'x'):
                count = 4 if ch == 'u' else 2
                raw = text[i:i+count]
                if len(raw) != count or not re.fullmatch('[0-9a-fA-F]+', raw):
                    raise SafeError('字符串转义无效')
                ch = chr(int(raw,16)); i += count
            else:
                raise SafeError('不支持的字符串转义')
        out.append(ch)
    raise SafeError('字符串未闭合')


def js_var(text, name):
    matches = list(re.finditer(r'\bvar\s+' + re.escape(name) + r'\s*=\s*', text))
    if len(matches) != 1:
        raise SafeError('认证成功字段缺失或重复')
    value, _ = js_string(text, matches[0].end())
    if not value:
        raise SafeError('认证成功字段为空')
    return value


def channels(text):
    matches = list(re.finditer(r'\bvar\s+channelArray\s*=\s*\[', text))
    if len(matches) != 1:
        raise SafeError('频道数组缺失或重复')
    i = matches[0].end()
    rows = []
    while True:
        while i < len(text) and text[i].isspace(): i += 1
        if i >= len(text): raise SafeError('频道数组未闭合')
        if text[i] == ']':
            if not rows: raise SafeError('拒绝空频道数组')
            break
        raw, i = js_string(text, i)
        pairs = []; pos = 0
        # Decode the JS literal first, then the attribute values' HTML entities.
        # Accept adjacent attributes as well as comma/whitespace delimiters;
        # never silently skip an unknown part of a provider record.
        attribute = re.compile(r'([A-Za-z0-9_]+)="([^"\r\n]*)"')
        while pos < len(raw):
            match = attribute.match(raw, pos)
            if not match: raise SafeError('频道记录解析不完整')
            pairs.append((match[1], unescape(match[2])))
            pos = match.end()
            if pos < len(raw):
                sep = re.match(r'(?:\s*,\s*|\s+)', raw[pos:])
                if sep:
                    pos += sep.end()
                elif not attribute.match(raw, pos):
                    raise SafeError('频道字段分隔无效')
        if not pairs: raise SafeError('拒绝空频道记录')
        rows.append(unique_pairs(pairs))
        while i < len(text) and text[i].isspace(): i += 1
        if i < len(text) and text[i] == ',':
            i += 1
            # Trailing commas / holes are deliberately rejected.
            if re.match(r'\s*\]', text[i:]): raise SafeError('频道数组含空项')
        elif i < len(text) and text[i] == ']':
            break
        else:
            raise SafeError('频道数组分隔无效')
    if not re.match(r'\]\s*;', text[i:]): raise SafeError('频道数组结尾无效')
    return rows


def origin(url):
    p = urlparse.urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.fragment:
        raise SafeError('认证地址无效')
    port = p.port or (443 if p.scheme == 'https' else 80)
    return f'{p.scheme}://{p.hostname.lower()}:{port}'


def trusted(url, config):
    allowed = [config['auth_origin'], *KNOWN_ORIGINS, *config.get('allowed_origins', [])]
    if origin(url) not in {origin(x) for x in allowed}:
        raise SafeError('认证跳转目标未获许可；请核实并配置允许的 origin')
    return url


def bound_socket(interface, timeout):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        # Linux SO_BINDTODEVICE. Never silently fall back to default routing.
        sock.setsockopt(socket.SOL_SOCKET, 25, interface.encode('ascii') + b'\0')
        return sock
    except BaseException:
        sock.close()
        raise


class BoundConnection(http.client.HTTPConnection):
    def __init__(self, host, interface, **kwargs):
        super().__init__(host, **kwargs)
        self.interface = interface

    def connect(self):
        # Deadline alarm also bounds DNS resolution, not just TCP I/O.
        for _, _, _, _, address in socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM):
            sock = bound_socket(self.interface, self.timeout)
            try:
                sock.connect(address)
                self.sock = sock
                return
            except OSError:
                sock.close()
        raise SafeError('运营商连接失败')


class BoundTLSConnection(BoundConnection):
    def connect(self):
        super().connect()
        self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


class HTTP:
    def __init__(self, config):
        self.config = config
        self.deadline = time.monotonic() + 75
        interface = config['interface']
        class Handler(request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(lambda host, **kw: BoundConnection(host, interface, **kw), req)
        class TLSHandler(request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(lambda host, **kw: BoundTLSConnection(host, interface, **kw), req)
        class Redirect(request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                trusted(newurl, config)
                return super().redirect_request(req, fp, code, msg, headers, newurl)
        self.opener = request.build_opener(request.ProxyHandler({}), Handler(), TLSHandler(), Redirect(), request.HTTPCookieProcessor(CookieJar()))

    def request(self, method, url, fields=None, referer=None):
        trusted(url, self.config)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0: raise SafeError('认证总超时')
        headers = {'User-Agent':'webkit;Resolution(PAL,720P,1080P)',
                   'Accept-Language':'zh-CN,en-US;q=0.8', 'Accept-Charset':'UTF-8',
                   'X-Requested-With':'com.android.smart.terminal.ctsh.iptv',
                   'Accept-Encoding':'identity'}
        if referer: headers['Referer'] = referer
        data = None
        if fields is not None:
            data = urlparse.urlencode(fields).encode('utf-8')
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        req = request.Request(url, data=data, headers=headers, method=method)
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        started = time.monotonic()
        def expired(*_): raise SafeError('认证请求超时')
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, min(15, remaining, previous_timer[0] or 15))
        try:
            with self.opener.open(req, timeout=min(15, remaining)) as response:
                raw = response.read(MAX_BODY+1)
                if len(raw) > MAX_BODY: raise SafeError('认证响应过大')
                encoding = response.headers.get('Content-Encoding', 'identity').lower()
                if encoding == 'gzip':
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                        raw = compressed.read(MAX_BODY+1)
                    if len(raw) > MAX_BODY: raise SafeError('认证响应过大')
                elif encoding not in ('', 'identity'):
                    raise SafeError('认证响应压缩不支持')
                charset = response.headers.get_content_charset() or 'utf-8'
                if charset.lower() not in ('utf-8','gbk','gb2312','gb18030'):
                    raise SafeError('认证响应编码不支持')
                return raw.decode(charset, errors='strict'), response.geturl()
        except SafeError:
            raise
        except Exception:
            raise SafeError('认证请求失败（网络、超时或响应无效）') from None
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0]:
                signal.setitimer(signal.ITIMER_REAL, max(0.001, previous_timer[0] - (time.monotonic()-started)), previous_timer[1])


def login(config, ip, http=None, context=None):
    http = http or HTTP(config)
    params = dict(UserID=config['userid'], Action='Login', SN=config['sn'], Type='iptv4k', Mode='MENU.SMG-4K', FCCSupport='1')
    url = config['auth_origin'].rstrip('/') + '/iptv3a/4kLogAuth.do?' + urlparse.urlencode(params)
    text, base = http.request('GET', trusted(url, config))
    attrs, fields = form(text)
    dynamic_ip = fields.get('DynamicAuthIP', '')
    action = trusted(urlparse.urljoin(base, attrs.get('action') or ''), config)
    text, base = http.request('POST', action, fields, referer=url)
    attrs, fields = form(text, 'initform')
    fields['authenticator'] = authenticator(js_var(text, 'encrytoken'), config, ip)
    action = trusted(urlparse.urljoin(base, attrs.get('action') or '/iptv3a/ottauth'), config)
    text, final_url = http.request('POST', action, fields, referer=base)
    # These markers are the reference client's success criteria after OTT auth.
    js_var(text, 'usertoken')
    if not re.search(r'EPGDomain=([^"\']+)', text) and not re.search(r'\bvar\s+EPGDomain\s*=\s*["\'][^"\']+', text):
        raise SafeError('认证成功标记缺失')
    if context is not None:
        context.update(text=text, referer=final_url, dynamic_ip=dynamic_ip, http=http)
    return channels(text)


def local_ipv4(interface):
    result = subprocess.run(['ip','-j','-4','addr','show','dev',interface,'scope','global'],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=True)
    ips = [str(ipaddress.IPv4Address(x['local'])) for item in json.loads(result.stdout)
           for x in item.get('addr_info',[]) if x.get('family') == 'inet']
    if len(ips) != 1: raise SafeError('所选 IPTV 接口必须有唯一全局 IPv4 地址')
    return ips[0]


# Portal metadata protocol; independent implementation, no programme EPG requests.
PORTAL_ORIGINS = {'http://218.83.165.50:8084', 'http://218.83.165.36:8084'}
CATEGORIES = [('000406',''), ('000406','tvod'), ('00040A',''), ('000403',''),
              ('000404',''), ('000404','tvod'), ('000405',''), ('000408',''),
              ('000409',''), ('000401',''), ('00040B',''), ('00040B','tvod')]


def set_cookie(http, name, value, url):
    from http.cookiejar import Cookie
    p = urlparse.urlsplit(url)
    jar = next(x.cookiejar for x in http.opener.handlers if isinstance(x, request.HTTPCookieProcessor))
    jar.set_cookie(Cookie(0, name, value, None, False, p.hostname, False, False,
                          '/', True, False, None, True, None, None, {}, False))


def portal_directory(config, context):
    http = context['http']
    http.deadline = time.monotonic() + 180
    body = context['text']
    token = js_var(body, 'usertoken')
    match = re.search(r'EPGDomain=([^"\']+)', body)
    epg = match.group(1).replace('\\/', '/') if match else js_var(body, 'EPGDomain')
    trusted(epg, config)
    fields = dict(UserID=config['userid'], Action='Login', UserToken=token,
                  UserGroupNMB='8888', EPGGroupNMB=js_var(body, 'epggroup'),
                  stbid='0', Mode='MENU.SMG', EPGProviderDomain='', DynamicAuthIP=context['dynamic_ip'])
    body, base = http.request('POST', epg, fields, referer=context['referer'])
    matches = list(re.finditer(r'top\.document\.location\s*=\s*', body))
    if len(matches) != 1: raise SafeError('门户登录跳转缺失或重复')
    balanced = trusted(urlparse.urljoin(base, unescape(js_string(body, matches[0].end())[0])), config)
    body, base = http.request('GET', balanced, referer=epg)
    attrs, fields = form(body)
    query = urlparse.parse_qs(urlparse.urlsplit(balanced).query)
    defaults = dict(UserToken=token, UserID=config['userid'], STBID='0', stbinfo='', prmid='',
                    stbtype='', drmsupplier='', easip=query.get('easip',[''])[0], networkid=query.get('networkid',['1'])[0])
    for k, v in defaults.items(): fields.setdefault(k, v)
    target = trusted(urlparse.urljoin(base, attrs.get('action') or 'funcportalauth.jsp'), config)
    body, _ = http.request('POST', target, fields, referer=balanced)
    info = {}
    for m in re.finditer(r'jsSetConfig\(\s*', body):
        key, end = js_string(body, m.end())
        sep = re.match(r'\s*,\s*', body[end:])
        if sep is None or key in info: raise SafeError('门户会话字段无效')
        info[key], _ = js_string(body, end + sep.end())
    if not all(info.get(k) for k in ('IpPort','framecode','SessionID')): raise SafeError('门户会话缺失')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', info['framecode']): raise SafeError('门户路径无效')
    host = trusted('http://' + info['IpPort'] + '/iptvepg/' + info['framecode'], config)
    set_cookie(http, 'JSESSIONID', info['SessionID'], host)
    http.request('GET', host + '/portal.jsp', referer=target)
    body, _ = http.request('GET', host + '/service/auth/AuthByAjax.jsp?action=auth',
                           referer=host + '/function/auth/bimsPortalAuth.html?jumpUrl=epgEntry.jsp')
    bims = json.loads(body, object_pairs_hook=unique_pairs)
    if not isinstance(bims, dict): raise SafeError('门户授权响应无效')
    for key, value in [('bims_user_token', bims.get('bimsUserToken')), ('bims_token_exp', bims.get('bimsTokenExp')),
                       ('BimsAuthenticationFlag','SUCCESS' if bims.get('bimsUserToken') else None), ('PRE_ADVERTISEMENT_FLAG','1')]:
        if value: set_cookie(http, key, str(value), host)
    batches = []
    for category, kind in CATEGORIES:
        body, _ = http.request('POST', host + '/function/ajax/epg7getChannelByAjax.jsp',
                               dict(action='getChannelList', cateID=category, type=kind), referer=host + '/portal.jsp')
        response = json.loads(body, object_pairs_hook=unique_pairs)
        batches.append(dict(category=category, type=kind, response=response))
    result = dict(schema=1, categories=batches)
    directory_index(result)
    return result


def directory_index(value):
    if not isinstance(value, dict) or value.get('schema') != 1 or not isinstance(value.get('categories'), list):
        raise SafeError('目录结构无效')
    if len(value['categories']) != len(CATEGORIES): raise SafeError('目录分类不完整')
    index = {}
    for batch, expected in zip(value['categories'], CATEGORIES):
        if not isinstance(batch, dict) or (batch.get('category'),batch.get('type')) != expected:
            raise SafeError('目录分类无效')
        response = batch.get('response')
        if not isinstance(response, dict) or not isinstance(response.get('data'), list): raise SafeError('目录响应无效')
        if response.get('status', '1') not in ('1', 1) or response.get('errCode', '') not in ('', None, '0', 0):
            raise SafeError('目录返回失败状态')
        for item in response['data']:
            if not isinstance(item, dict): raise SafeError('目录记录无效')
            mix, name = item.get('mixNo'), item.get('name')
            if not isinstance(mix, (str,int)) or isinstance(mix,bool) or str(mix) == '' or not isinstance(name,str) or not name:
                raise SafeError('目录名称或编号无效')
            # Distinct names are candidates; no category preference or normalization.
            candidates = index.setdefault(str(mix), [])
            if name not in candidates: candidates.append(name)
    if not index: raise SafeError('目录为空')
    return index


def resolve_names(rows, directory=None):
    index = directory_index(directory) if directory is not None else {}
    counts = dict(source=0, matched=0, ambiguous=0, unmatched=0)
    names = []
    for row in rows:
        own = row.get('ChannelName')
        candidates = index.get(str(row.get('UserChannelID', '')), [])
        if own is not None and own not in ('','null'):
            name = own; counts['source'] += 1
        elif len(candidates) == 1:
            name = candidates[0]; counts['matched'] += 1
        else:
            name = row.get('ChannelID')
            counts['ambiguous' if len(candidates) > 1 else 'unmatched'] += 1
        names.append(text(name))
    return names, counts



#!/usr/bin/env python3
"""Private, instance-scoped Shanghai Telecom IPTV CLI."""
import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib.parse import urlsplit, quote
auth = sys.modules[__name__]  # Internal namespace; single runtime file.

SEEK = '{utc:YmdHMS}-{utcend:YmdHMS}'


def text(value):
    if not isinstance(value, str) or any(ord(c) < 32 or c == '"' for c in value):
        raise auth.SafeError('无效文本字段')
    return value


def instance_paths(directory, instance):
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,63}', instance):
        raise auth.SafeError('实例名称无效')
    d = Path(directory).absolute()
    return d, d / f'{instance}.auth.json', d / f'catchup_{instance}.json', d / f'raw_{instance}.m3u'


def endpoint(value, schemes):
    text(value)
    p = urlsplit(value)
    if p.scheme not in schemes or not p.hostname or p.username or p.password or p.fragment:
        raise auth.SafeError('流地址无效')
    if p.port is not None and not 0 < p.port < 65536:
        raise auth.SafeError('流端口无效')
    return p


def validate(c):
    if not isinstance(c.get('directory_metadata', False), bool): raise auth.SafeError('目录开关无效')
    if c.get('m3u_output') is not None and (not text(c['m3u_output']) or not Path(c['m3u_output']).is_absolute()): raise auth.SafeError('M3U配置路径必须绝对')
    if c.get('provider') != 'shanghai-telecom':
        raise auth.SafeError('目前仅支持上海电信')
    for k in ('userid', 'sn', 'mac', 'interface', 'base_url', 'auth_origin'):
        if not text(c.get(k)):
            raise auth.SafeError('配置字段缺失')
    if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', c['mac']):
        raise auth.SafeError('盒子 MAC 格式无效')
    for k in ('interface', 'fcc_interface', 'multicast_interface'):
        if c.get(k) and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', c[k]):
            raise auth.SafeError('接口名称无效')
    for value in [c['auth_origin'], *c.get('allowed_origins', [])]:
        auth.origin(value)
        p = urlsplit(value)
        if p.path not in ('', '/') or p.query: raise auth.SafeError('鉴权入口必须是 origin')
    p = endpoint(c['base_url'], ('http', 'https'))
    if p.query: raise auth.SafeError('服务基址不接受查询参数')
    for k in ('live_path', 'replay_path'):
        if not re.fullmatch(r'/[A-Za-z0-9_/-]+/', c[k]):
            raise auth.SafeError('服务接口路径无效')
    template = text(c['seek_template'])
    if not template or '&' in template or '#' in template or '?' in template:
        raise auth.SafeError('回放时间模板无效')


def load(path):
    info = auth.regular(path)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise auth.SafeError('身份配置必须属当前用户且为0600')
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise auth.SafeError('身份目录必须为0700')
    c = auth.read_json(path)
    validate(c)
    return c


def configure(args, paths):
    d, path, _, _ = paths
    with auth.lock(d, args.instance + '.lock'):
        c = load(path) if auth.regular(path, missing=True) else {}
        c['provider'] = 'shanghai-telecom'
        # Explicit pipe mode consumes exactly three lines, never argv/env identity.
        for key, label in [('userid','UserID'),('sn','盒子 SN'),('mac','盒子 MAC')]:
            value = sys.stdin.readline().rstrip('\r\n') if args.identity_stdin else getpass.getpass(label + '（隐藏输入，回车保留）: ')
            if value: c[key] = value.strip()
        defaults = dict(interface='eth0', multicast_interface='', fcc_interface='', auth_origin=auth.DEFAULT_ORIGIN,
                        base_url='http://127.0.0.1:5140', live_path='/rtp/', replay_path='/rtsp/', seek_template=SEEK)
        for key, default in defaults.items():
            value = getattr(args, key, None)
            c[key] = value if value is not None else c.get(key, default)
        if args.allow_origin is not None: c['allowed_origins'] = args.allow_origin
        c.setdefault('allowed_origins', [])
        if args.directory_metadata is not None: c['directory_metadata'] = args.directory_metadata
        if args.m3u_output is not None: c['m3u_output'] = str(Path(args.m3u_output).absolute())
        validate(c)
        auth.atomic_json(path, c)
        print('配置已保存：私有目录0700，身份文件0600')


def append_query(url, params):
    if not params: return url
    return url + ('&' if '?' in url and not url.endswith(('?', '&')) else '' if '?' in url else '?') + '&'.join(params)


def replay_url(value, c):
    if value is None or value == '' or isinstance(value, str) and value.strip().lower() == 'null':
        return None
    p = endpoint(value, ('rtsp',))
    host = '[' + p.hostname + ']' if ':' in p.hostname else p.hostname
    # Keep raw path and query verbatim, including repeated keys, escapes and '+'.
    url = c['base_url'].rstrip('/') + c['replay_path'] + host + ':' + str(p.port or 554) + (p.path or '/')
    if '?' in value: url += '?' + value.split('?', 1)[1]
    if any(k.lower() == 'playseek' for k in [x.split('=',1)[0] for x in p.query.split('&')]):
        raise auth.SafeError('原始回放已有playseek，拒绝覆盖认证查询')
    return append_query(url, ['playseek=' + c['seek_template'], 'r2h-ifname=' + quote(c['interface'], safe='')])


def live_url(row, c):
    p = endpoint(row.get('ChannelURL'), ('igmp', 'rtp', 'udp'))
    if not ipaddress.ip_address(p.hostname).is_multicast or not p.port:
        raise auth.SafeError('直播必须为组播地址及端口')
    url = c['base_url'].rstrip('/') + c['live_path'] + p.netloc + p.path
    if '?' in row['ChannelURL']: url += '?' + row['ChannelURL'].split('?', 1)[1]
    params = []
    ip, port = row.get('ChannelFCCIP'), row.get('ChannelFCCPort')
    if ip and port and str(ip).lower() != 'null' and str(port).lower() != 'null':
        ipaddress.ip_address(ip)
        if not str(port).isdigit() or not 0 < int(port) < 65536: raise auth.SafeError('FCC端口无效')
        params.append('fcc=' + str(ip) + ':' + str(port))
        if c.get('fcc_interface'): params.append('r2h-ifname-fcc=' + quote(c['fcc_interface'], safe=''))
    params.append('r2h-ifname=' + quote(c.get('multicast_interface') or c['interface'], safe=''))
    return append_query(url, params)


def render(rows, c, directory=None):
    validate(c)
    if not isinstance(rows, list) or not rows: raise auth.SafeError('原始频道数组为空或无效')
    lines = ['#EXTM3U', '# Raw channelArray order; ChannelID is a placeholder when ChannelName is absent. Apply metadata/templates separately.']
    if any(not isinstance(row, dict) for row in rows): raise auth.SafeError('无效频道记录')
    names, _ = resolve_names(rows, directory)
    for row, name in zip(rows, names):
        if not isinstance(row, dict): raise auth.SafeError('无效频道记录')
        channel_id = text(row.get('ChannelID'))
        if not channel_id: raise auth.SafeError('缺少ChannelID')
        replay = replay_url(row.get('TimeShiftURL'), c)
        catchup = 'catchup="none"' if replay is None else f'catchup="default" catchup-source="{replay}"'
        lines.extend([f'#EXTINF:-1 tvg-id="{channel_id}" tvg-name="{name}" {catchup},{name}', live_url(row, c)])
    return '\n'.join(lines) + '\n'


def atomic_text(path, value):
    auth.safe_parents(path.parent); auth.regular(path, missing=True)
    name = None
    try:
        fd, name = tempfile.mkstemp(prefix='.iptv-', dir=path.parent)
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), 0o600); f.write(value.encode('utf-8')); f.flush(); os.fsync(f.fileno())
        if Path(name).read_text() != value: raise auth.SafeError('临时文件校验失败')
        auth.regular(path, missing=True); os.replace(name, path); name = None
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if name is not None: os.unlink(name)


def metadata_path(paths, instance):
    return paths[0] / f'channels_{instance}.json'


def fetch(paths, instance):
    d, config, output, _ = paths
    with auth.lock(d, instance + '.lock'):
        c = load(config)
        if c.get('directory_metadata', False):
            context = {}
            rows = auth.login(c, auth.local_ipv4(c['interface']), context=context)
            try:
                directory = portal_directory(c, context)
            except Exception:
                print('fetch失败：目录获取/校验失败，保留已有原始及目录文件', file=sys.stderr)
                raise auth.SafeError('目录获取失败') from None
            auth.atomic_json(metadata_path(paths, instance), directory)
        else:
            rows = auth.login(c, auth.local_ipv4(c['interface']))
        auth.atomic_json(output, rows)
        print(f'fetch成功：{len(rows)}条完整原始记录；目录' + ('已刷新' if c.get('directory_metadata') else '未启用'))


def convert(paths, instance, m3u_output=None):
    d, config, source, default_output = paths
    with auth.lock(d, instance + '.lock'):
        c = load(config)
        rows = auth.read_json(source)
        metadata = metadata_path(paths, instance)
        directory = auth.read_json(metadata) if auth.regular(metadata, missing=True) else None
        result = render(rows, c, directory)
        output = Path(m3u_output or c.get('m3u_output') or default_output).absolute()
        protected = [config, source, metadata, d / (instance + '.lock'), Path(__file__)]
        if output.resolve() in [p.resolve() for p in protected]: raise auth.SafeError('M3U输出不能覆盖配置或原始数据')
        atomic_text(output, result)
        _, counts = resolve_names(rows, directory)
        print(f'convert成功：{len(rows)}条M3U记录；名称 ' + json.dumps(counts, ensure_ascii=False) + '；未匹配/歧义以ChannelID占位；分组待核查')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('configure', 'fetch', 'convert', 'run'))
    parser.add_argument('--directory', default=str(Path(__file__).resolve().parent))
    parser.add_argument('--instance', required=True)
    parser.add_argument('--identity-stdin', action='store_true', help='configure only: read UserID, SN, MAC as three private stdin lines')
    for key in ('interface', 'multicast-interface', 'fcc-interface', 'base-url', 'live-path', 'replay-path', 'auth-origin', 'seek-template'):
        parser.add_argument('--' + key)
    parser.add_argument('--allow-origin', action='append')
    parser.add_argument('--directory-metadata', action=argparse.BooleanOptionalAction, default=None, help='configure: enable portal raw channel directory')
    parser.add_argument('--m3u-output', help='configure/convert/run: M3U path; CLI overrides saved setting')
    args = parser.parse_args(argv)
    try:
        paths = instance_paths(args.directory, args.instance)
        if args.command == 'configure': configure(args, paths)
        else:
            if args.command in ('fetch', 'run'): fetch(paths, args.instance)
            if args.command in ('convert', 'run'): convert(paths, args.instance, args.m3u_output)
        return 0
    except (Exception, KeyboardInterrupt):
        print('IPTV操作失败：请检查配置、权限、原始数据、接口与网络（详情已脱敏）', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
