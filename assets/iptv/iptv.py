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
def lock(directory, filename='.iptv.lock'):
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


def config_paths(directory):
    d = Path(os.path.abspath(directory))
    if Path(__file__).resolve() in [(d / name).resolve() for name in
                                    ('config.json', 'catchup.json', 'channels.json', 'playlist.m3u', '.iptv.lock')]:
        raise SafeError('实例固定文件不能覆盖运行脚本')
    return d, d / 'config.json', d / 'catchup.json', d / 'playlist.m3u'


def endpoint(value, schemes):
    text(value)
    p = urlsplit(value)
    if p.scheme not in schemes or not p.hostname or p.username or p.password or p.fragment:
        raise auth.SafeError('流地址无效')
    if p.port is not None and not 0 < p.port < 65536:
        raise auth.SafeError('流端口无效')
    return p


def validate(c):
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
    auth.safe_parents(path.parent)
    c = auth.read_json(path)
    validate(c)
    return c


def configure(paths):
    d, path, _, _ = paths
    with auth.lock(d, '.iptv.lock'):
        c = load(path) if auth.regular(path, missing=True) else {}
        c['provider'] = 'shanghai-telecom'

        def ask(label, current=''):
            prompt = label + '（隐藏输入，回车保留）: '
            return getpass.getpass(prompt) or current

        for key, label in [('userid','UserID'), ('sn','盒子 SN'), ('mac','盒子 MAC')]:
            c[key] = ask(label, c.get(key, ''))
        fields = [
            ('interface', '鉴权/回放接口', ''),
            ('multicast_interface', '组播接口（空值沿用鉴权接口）', ''),
            ('fcc_interface', 'FCC接口（空值不指定）', ''),
            ('auth_origin', '鉴权入口origin', DEFAULT_ORIGIN),
            ('base_url', 'rtp2httpd base URL', 'http://127.0.0.1:5140'),
            ('live_path', '直播路径', '/rtp/'),
            ('replay_path', '回放路径', '/rtsp/'),
            ('seek_template', '回放时间模板', SEEK),
        ]
        for key, label, default in fields:
            value = ask(label, c.get(key, default))
            c[key] = '' if value == '-' and key in ('multicast_interface', 'fcc_interface') else value
        origins = ask('额外允许origin（空格分隔，-清空）', ' '.join(c.get('allowed_origins', [])))
        c['allowed_origins'] = [] if origins == '-' else origins.split()
        c.pop('directory_metadata', None)  # Retire the legacy key only on explicit save.
        validate(c)
        auth.atomic_json(path, c)
        print('配置已保存：config.json为0600，现存目录权限保持不变')


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


def metadata_path(paths):
    return paths[0] / 'channels.json'


def fetch(paths):
    d, config, output, _ = paths
    with auth.lock(d, '.iptv.lock'):
        c = load(config)
        context = {}
        rows = auth.login(c, auth.local_ipv4(c['interface']), context=context)
        try:
            directory = portal_directory(c, context)
        except Exception:
            raise auth.SafeError('目录获取/校验失败，保留已有原始及目录文件') from None
        auth.atomic_json(metadata_path(paths), directory)
        auth.atomic_json(output, rows)
        print(f'fetch成功：{len(rows)}条完整原始记录；目录已刷新')


def export_path(value, directory):
    """A .m3u suffix is a file; every other value is a directory."""
    if not value or '\x00' in value:
        raise SafeError('output不能为空')
    raw = Path(value)
    probe = raw if raw.is_absolute() else Path(os.getcwd()) / raw
    file_output = raw.name.lower().endswith('.m3u')
    try:
        info = probe.lstat()
    except (FileNotFoundError, NotADirectoryError):
        info = None
    if info is not None and (stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode) == file_output):
        raise SafeError('output不是目录，也不能覆盖已有文件；目录请用目录路径，文件请用.m3u')
    path = Path(value) if file_output else Path(value) / (directory.name + '.m3u')
    if not path.is_absolute():
        path = Path(os.getcwd()) / path
    return Path(os.path.abspath(path))


def output_basename(value):
    if (not value or value in ('.', '..') or value.startswith('.') or
            '/' in value or '\\' in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise SafeError('输出名必须是非隐藏安全文件名，不能包含路径')
    return value


def ensure_output_dir(path):
    parent = path.parent
    if parent.exists() and not parent.is_symlink() and parent.is_dir():
        return True
    try:
        parent.mkdir(mode=0o700, parents=True)
    except OSError:
        return False
    return True


def protected_export(export, paths):
    d, config, source, output = paths
    metadata = metadata_path(paths)
    protected = [config, source, metadata, d / '.iptv.lock', Path(__file__), output]
    if (export.resolve() in [p.resolve() for p in protected] or
            export.name in ('config.json', 'catchup.json', 'channels.json', '.iptv.lock') or
            (export.parent != d and (export.parent / 'config.json').exists())):
        raise SafeError('导出不能覆盖配置、源数据、其他实例或脚本')


def convert(paths, destination=None):
    d, config, source, output = paths
    with auth.lock(d, '.iptv.lock'):
        c = load(config)
        if auth.regular(source, missing=True) is None:
            raise SafeError('缺少catchup.json；请先执行fetch主动获取，再离线转换')
        rows = auth.read_json(source)
        metadata = metadata_path(paths)
        directory = auth.read_json(metadata) if auth.regular(metadata, missing=True) else None
        result = render(rows, c, directory)
        if output.resolve() == Path(__file__).resolve():
            raise SafeError('主输出不能覆盖运行脚本')
        atomic_text(output, result)
        shown = output.resolve()
        export_skipped = False
        if destination is not None and destination.resolve() != output.resolve():
            if not ensure_output_dir(destination):
                export_skipped = True
                print('新建目录失败，未导出播放列表。原播放列表在：' + str(output.resolve()))
            else:
                try:
                    output_basename(destination.name)
                    protected_export(destination, paths)
                    atomic_text(destination, result)
                    shown = destination.resolve()
                except SafeError:
                    raise
                except OSError:
                    print('导出写入失败。原播放列表在：' + str(output.resolve()), file=sys.stderr)
                    raise SafeError('导出写入失败。原播放列表在：' + str(output.resolve())) from None
        if not export_skipped:
            print('本次生成的播放列表在：' + str(shown))
        _, counts = resolve_names(rows, directory)
        print(f'convert成功：{len(rows)}条M3U记录；名称 ' + json.dumps(counts, ensure_ascii=False))


COMMANDS = ('configure', 'fetch', 'convert', 'run')
FIELDS = {'userid', 'sn', 'mac', 'interface', 'multicast_interface', 'fcc_interface',
          'auth_origin', 'base_url', 'live_path', 'replay_path', 'seek_template',
          'allowed_origins'}
ALIASES = {'userinfo': 'userid', 'SN': 'sn', 'MAC': 'mac', 'baseURL': 'base_url'}


def parse_pairs(argv):
    values = {}
    for arg in argv:
        if '=' not in arg: raise SafeError('参数必须使用key=value，例如 config=tel')
        key, value = arg.split('=', 1)
        key = ALIASES.get(key, key)
        if key == 'directory_metadata':
            raise SafeError('directory_metadata参数已移除，无需设置；fetch/run及全参数获取会同时刷新完整频道目录')
        if key not in FIELDS | {'config', 'output'} or key in values:
            raise SafeError('未知或重复参数键')
        values[key] = value
    return values


def config_directory(location):
    if not location: raise SafeError('config不能为空')
    directory = Path(location)
    if not directory.is_absolute(): directory = Path(__file__).resolve().parent / directory
    return directory


def save_fields(paths, values):
    with lock(paths[0]):
        c = load(paths[1]) if regular(paths[1], missing=True) else {}
        defaults = dict(provider='shanghai-telecom', multicast_interface='', fcc_interface='',
                        auth_origin=DEFAULT_ORIGIN, base_url='http://127.0.0.1:5140',
                        live_path='/rtp/', replay_path='/rtsp/', seek_template=SEEK,
                        allowed_origins=[])
        for key, value in defaults.items(): c.setdefault(key, value)
        if 'allowed_origins' in values:
            values = dict(values)
            values['allowed_origins'] = values['allowed_origins'].split()
        c.pop('directory_metadata', None)
        c.update(values)
        validate(c)
        atomic_json(paths[1], c)


def key_value_main(argv):
    values = parse_pairs(argv)
    configuring = bool(FIELDS & values.keys())
    location = values.pop('config', None)
    output_value = values.pop('output', None)
    if location is None:
        if not values.get('interface'): raise SafeError('首次省略config时必须明确提供interface')
        location = values['interface']
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', location) or location in ('.', '..'):
            raise SafeError('接口名称无效')
    paths = config_paths(config_directory(location))
    if configuring:
        save_fields(paths, values)
        fetch(paths)
    destination = export_path(output_value, paths[0]) if output_value is not None else None
    convert(paths, destination=destination)
    return 0


def reject_retired(argv):
    retired = {
        '--config-dir': '请改用 config=tel',
        '--output-dir': '请改用 output=/path/file.m3u 或 output=exports',
        '--output-name': '请把文件名写在 output= 里',
        '--stdin': 'configure config=tel 会逐项隐藏询问，不再读取固定行',
        '--instance': '请改用 config=tel',
        '--directory': '请改用 config=tel',
        '--identity-stdin': 'configure config=tel 会逐项隐藏询问',
        '--m3u-output': '请改用 output=/path/file.m3u',
    }
    for arg in argv:
        name = arg.split('=', 1)[0]
        if name in retired:
            raise SafeError(name + '已不再使用。' + retired[name])


def usage():
    return '''用法：
  python3 iptv.py config=tel userinfo=YOUR_USER_ID SN=YOUR_BOX_SN MAC=YOUR_BOX_MAC interface=iptv85 baseURL=http://127.0.0.1:5140
  python3 iptv.py run config=tel
  python3 iptv.py config=tel
  python3 iptv.py config=tel output=exports

config 相对路径以脚本目录为基准。带身份或播放字段时，合并已有配置、保存并获取后生成。run 重新获取并生成。只给 config（可再加 output）时离线生成。
以 .m3u 结尾的 output 是文件，其他 output 是目录，副本名为配置目录名.m3u。当前仅上海电信。'''


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(arg in ('-h', '--help') for arg in argv):
        print(usage())
        return 0
    try:
        reject_retired(argv)
        command, pairs, = 'convert', []
        for arg in argv:
            if arg in COMMANDS and command == 'convert' and not pairs:
                command = arg
            else:
                pairs.append(arg)
        values = parse_pairs(pairs)
        configuring = bool(FIELDS & values.keys())
        if command != 'convert' and configuring:
            raise SafeError('配置字段与子命令不能同时使用；首次直接写 config=tel userinfo=...，之后用 run config=tel')
        if 'output' in values and command not in ('convert', 'run'):
            raise SafeError('output仅用于离线生成或run')
        location = values.pop('config', None)
        output_value = values.pop('output', None)
        if command == 'convert' and not argv:
            raise SafeError('请提供 config=tel；首次示例：' + usage().splitlines()[-3].strip())
        if location is None:
            if command != 'convert' or not values.get('interface'):
                raise SafeError('请提供 config=目录；仅首次全参数可省略config并用interface作目录名')
            location = values['interface']
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', location) or location in ('.', '..'):
                raise SafeError('接口名称无效')
        paths = config_paths(config_directory(location))
        if configuring:
            save_fields(paths, values)
            fetch(paths)
        if command == 'configure':
            configure(paths)
        else:
            if command in ('fetch', 'run'): fetch(paths)
            if command in ('convert', 'run'):
                destination = export_path(output_value, paths[0]) if output_value is not None else None
                convert(paths, destination=destination)
        return 0
    except SafeError as exc:
        print('IPTV操作失败：' + str(exc), file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        print('IPTV操作失败：请检查配置、权限、原始数据、接口与网络（详情已脱敏）', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
