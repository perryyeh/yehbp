#!/usr/bin/env python3
"""Single-file Shanghai Telecom IPTV configure/fetch/convert reference CLI.

Protocol evidence: pcg562240/pcg-iptv shctiptv.py at
38653f0bd722c4dececae434efa3593be97e8aca. That tree has no license;
this implementation does not vendor its AES or client implementation.
"""
import argparse
import concurrent.futures
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
import shutil
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


def apply_auth(c, value):
    """First address is the only login entry. Later ones only permit hops."""
    origins = value.split()
    if not origins:
        raise SafeError('鉴权地址不能为空')
    c['auth_origin'] = origins[0]
    c['allowed_origins'] = origins[1:]
    return c


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
    if context is not None:
        context['portal_host'] = info['IpPort']
        context['framecode'] = info['framecode']
    return directory_from_portal(http, host, host + '/portal.jsp')


EPG_DAYS_BACK = 7
EPG_DAYS_FORWARD = 3
EPG_PROG_LIMIT = '2000'
SESSION_SCHEMA = 1


def cookie_value(http, name, url):
    jar = next(x.cookiejar for x in http.opener.handlers if isinstance(x, request.HTTPCookieProcessor))
    host = urlparse.urlsplit(url).hostname
    for cookie in jar:
        if cookie.name == name and cookie.domain == host:
            return cookie.value
    return None


def portal_session_payload(config, context):
    """Portal cookies only. Credentials stay in config.json."""
    http = context['http']
    body = context['text']
    match = re.search(r'EPGDomain=([^"\']+)', body)
    epg = match.group(1).replace('\\/', '/') if match else js_var(body, 'EPGDomain')
    host = trusted('http://' + context['portal_host'] + '/iptvepg/' + context['framecode'], config)
    names = ('JSESSIONID', 'bims_user_token', 'bims_token_exp', 'BimsAuthenticationFlag', 'PRE_ADVERTISEMENT_FLAG')
    cookies = {name: cookie_value(http, name, host) for name in names}
    cookies = {name: value for name, value in cookies.items() if value}
    if 'JSESSIONID' not in cookies:
        raise SafeError('门户会话缺失')
    return dict(schema=SESSION_SCHEMA, portal_host=host, epg_domain=epg,
                referer=host + '/portal.jsp', cookies=cookies)


def restore_portal(config, session, http):
    if not isinstance(session, dict) or session.get('schema') != SESSION_SCHEMA:
        raise SafeError('门户会话无效')
    host = session.get('portal_host')
    cookies = session.get('cookies')
    referer = session.get('referer')
    if not isinstance(host, str) or not isinstance(referer, str) or not isinstance(cookies, dict):
        raise SafeError('门户会话无效')
    trusted(host, config)
    trusted(referer, config)
    if not host.startswith('http://') or '/iptvepg/' not in host or not cookies.get('JSESSIONID'):
        raise SafeError('门户会话无效')
    for name, value in cookies.items():
        if not isinstance(name, str) or not isinstance(value, str) or not value:
            raise SafeError('门户会话无效')
        set_cookie(http, name, value, host)
    return host, referer


def post_portal(http, host, referer, fields):
    body, _ = http.request('POST', host.rstrip('/') + '/function/ajax/epg7getChannelByAjax.jsp',
                           fields, referer=referer)
    response = json.loads(body, object_pairs_hook=unique_pairs)
    if not isinstance(response, dict):
        raise SafeError('门户响应无效')
    return response


def directory_from_portal(http, host, referer):
    batches = []
    for category, kind in CATEGORIES:
        response = post_portal(http, host, referer, dict(action='getChannelList', cateID=category, type=kind))
        if not isinstance(response.get('data'), list):
            raise SafeError('目录响应无效')
        batches.append(dict(category=category, type=kind, response=response))
    result = dict(schema=1, categories=batches)
    directory_index(result)
    return result


def programme_window(now=None):
    moment = time.time() if now is None else now
    now_ms = int(moment * 1000)
    return now_ms - EPG_DAYS_BACK * 86400 * 1000, now_ms + EPG_DAYS_FORWARD * 86400 * 1000


def channel_programmes(http, host, referer, item, start_ms, end_ms):
    code, channel_id = item.get('code'), item.get('ID')
    if not isinstance(code, str) or not code or isinstance(channel_id, bool) or channel_id in (None, ''):
        raise SafeError('节目单频道标识无效')
    response = post_portal(http, host, referer, dict(
        action='getChannelProg', code=str(code), channelID=str(channel_id),
        endTime=str(end_ms), startTime=str(start_ms), offset='0', limit=EPG_PROG_LIMIT))
    data = response.get('data')
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise SafeError('节目单响应无效')
    return data


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


def resolve_names(rows, directory=None, config=None):
    index = directory_index(directory) if directory is not None else {}
    overrides = channel_name_map(config)
    counts = dict(override=0, source=0, matched=0, ambiguous=0, unmatched=0)
    names = []
    for row in rows:
        own = row.get('ChannelName')
        channel_id = str(row.get('ChannelID', ''))
        candidates = index.get(str(row.get('UserChannelID', '')), [])
        if channel_id in overrides:
            name = overrides[channel_id]; counts['override'] += 1
        elif own is not None and own not in ('','null'):
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

SEEK = '${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}'


def text(value):
    if not isinstance(value, str) or any(ord(c) < 32 or c == '"' for c in value):
        raise auth.SafeError('无效文本字段')
    return value


def xml_text(value):
    return (str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&apos;'))


def xmltv_stamp(value):
    if isinstance(value, bool):
        raise auth.SafeError('节目时间无效')
    millis = int(str(value))
    moment = time.gmtime((millis / 1000) + 8 * 3600)
    return time.strftime('%Y%m%d%H%M%S', moment) + ' +0800'


def guide_rows(rows, directory):
    """XMLTV ids stay ChannelID. mixNo only selects the portal programme list."""
    index = {}
    for batch in directory['categories']:
        for item in batch['response']['data']:
            index.setdefault(str(item.get('mixNo')), item)
    selected = []
    for row in rows:
        mix = str(row.get('UserChannelID', ''))
        item = index.get(mix)
        if item is None:
            continue
        code, portal_id = item.get('code'), item.get('ID')
        if not isinstance(code, str) or not code or isinstance(portal_id, bool) or portal_id in (None, ''):
            continue
        selected.append((row, item))
    return selected


def render_xmltv(rows, directory, programmes):
    selected = guide_rows(rows, directory)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv source-info-name="Shanghai Telecom IPTV">']
    seen = set()
    for row, _item in selected:
        channel_id = text(row.get('ChannelID'))
        if not channel_id or channel_id in seen:
            continue
        seen.add(channel_id)
        own = row.get('ChannelName')
        name = text(own) if own not in (None, '', 'null') else channel_id
        lines.append('  <channel id="%s"><display-name lang="zh">%s</display-name></channel>' % (
            xml_text(channel_id), xml_text(name)))
    for row, _item in selected:
        channel_id = text(row.get('ChannelID'))
        for programme in programmes.get(channel_id, []):
            if not isinstance(programme, dict):
                continue
            try:
                start, stop = xmltv_stamp(programme.get('startTime')), xmltv_stamp(programme.get('endTime'))
            except (TypeError, ValueError, OverflowError):
                continue
            title = programme.get('name')
            if not isinstance(title, str):
                continue
            lines.append('  <programme start="%s" stop="%s" channel="%s"><title lang="zh">%s</title></programme>' % (
                start, stop, xml_text(channel_id), xml_text(title)))
    lines.append('</tv>')
    return '\n'.join(lines) + '\n'


def config_paths(directory):
    d = Path(os.path.abspath(directory))
    names = ('config.json', 'catchup.json', 'channels.json', 'resolution.json', 'playlist.m3u', 'epg.xml', 'session.json', '.iptv.lock')
    if Path(__file__).resolve() in [(d / name).resolve() for name in names]:
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
    # Empty playback overrides mean "do not add"; they never fall back to the auth interface.
    for k in ('interface', 'multicast_interface', 'fcc_interface'):
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
    guide = c.get('epg_url') or ''
    if guide:
        text(guide)
        parsed = urlsplit(guide)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or any(ch in guide for ch in ' ",\r\n'):
            raise auth.SafeError('节目单地址无效')
    if 'filter' in c:
        validate_filter(c['filter'])
    if 'channel_names' in c:
        channel_name_map(c)


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
            ('interface', '鉴权接口', ''),
            ('base_url', 'rtp2httpd访问地址', 'http://127.0.0.1:5140'),
            ('live_path', '直播路径', '/rtp/'),
            ('replay_path', '回放路径', '/rtsp/'),
            ('seek_template', '回放时间模板', SEEK),
        ]
        for key, label, default in fields:
            c[key] = ask(label, c.get(key, default))
        current_auth = ' '.join([c.get('auth_origin') or DEFAULT_ORIGIN, *c.get('allowed_origins', [])])
        entered = ask('鉴权地址（空格分隔，第一个为入口，回车保留）', current_auth) or current_auth
        if entered != current_auth or not c.get('auth_origin'):
            apply_auth(c, entered)
        c.pop('directory_metadata', None)  # Retire the legacy key only on explicit save.
        c['filter'] = normalize_filter(c.get('filter'))
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
    # Replay does not inherit the auth interface. Keep any override already in the source query.
    return append_query(url, ['playseek=' + c['seek_template']])


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
    if c.get('multicast_interface'):
        params.append('r2h-ifname=' + quote(c['multicast_interface'], safe=''))
    return append_query(url, params)


def epg_path(paths):
    return paths[0] / 'epg.xml'


def session_path(paths):
    return paths[0] / 'session.json'


def playlist_header(paths, c, explicit=None):
    target = explicit if explicit is not None else (c.get('epg_url') or '')
    if not target and auth.regular(epg_path(paths), missing=True):
        target = 'epg.xml'
    if not target:
        return '#EXTM3U'
    text(target)
    if any(ch in target for ch in ' ",\r\n'):
        raise auth.SafeError('节目单地址无效')
    return '#EXTM3U x-tvg-url="%s"' % target


def playlist_body(rows, c, directory=None, header='#EXTM3U', resolutions=None, apply_filter=True):
    """Build the list and counts. Callers refuse an empty kept set before writing."""
    validate(c)
    if not isinstance(rows, list) or not rows: raise auth.SafeError('原始频道数组为空或无效')
    if not header.startswith('#EXTM3U'): raise auth.SafeError('播放列表头无效')
    lines = [header, '# Raw channelArray order; ChannelID is a placeholder when ChannelName is absent. Apply metadata/templates separately.']
    if any(not isinstance(row, dict) for row in rows): raise auth.SafeError('无效频道记录')
    names, _ = resolve_names(rows, directory, c)
    written, counts = 0, dict(excluded_height=0, excluded_name=0, kept=0)
    for row, name in zip(rows, names):
        if not isinstance(row, dict): raise auth.SafeError('无效频道记录')
        channel_id = text(row.get('ChannelID'))
        if not channel_id: raise auth.SafeError('缺少ChannelID')
        decision = playlist_decision(row, name, c, resolutions) if apply_filter else 'kept'
        counts[decision] += 1
        if decision != 'kept':
            continue
        written += 1
        replay = replay_url(row.get('TimeShiftURL'), c)
        catchup = 'catchup="none"' if replay is None else f'catchup="default" catchup-source="{replay}"'
        lines.extend([f'#EXTINF:-1 tvg-id="{channel_id}" tvg-name="{name}" {catchup},{name}', live_url(row, c)])
    return '\n'.join(lines) + '\n', counts


def render(rows, c, directory=None, header='#EXTM3U', resolutions=None, apply_filter=True):
    body, counts = playlist_body(rows, c, directory, header, resolutions, apply_filter)
    if counts['kept'] == 0:
        raise auth.SafeError('过滤后没有可输出的频道，保留上次播放列表')
    return body, counts


DEFAULT_EXCLUDE_NAMES = ('购物', '理财', '聚鲨')
DEFAULT_MIN_HEIGHT = 720
PROBE_TIMEOUT = 12
PROBE_CONCURRENCY = 2
PROBE_SAVE_EVERY = 10


def resolution_path(paths):
    return paths[0] / 'resolution.json'


def live_fingerprint(url):
    return hashlib.sha256(url.encode('utf-8')).hexdigest()


def source_key(channel_id, url):
    return channel_id + '\n' + live_fingerprint(url)


def load_resolutions(path):
    if auth.regular(path, missing=True) is None:
        return {'schema': 1, 'channels': {}}
    value = auth.read_json(path)
    if not isinstance(value, dict) or value.get('schema') != 1 or not isinstance(value.get('channels'), dict):
        raise auth.SafeError('分辨率记录结构无效')
    return value


def channel_name_map(config):
    """Exact ChannelID display names from config. Empty when the field is absent."""
    if config is None or 'channel_names' not in config:
        return {}
    value = config.get('channel_names')
    if not isinstance(value, dict):
        raise auth.SafeError('channel_names必须是对象')
    if len(value) > 256:
        raise auth.SafeError('channel_names过长')
    mapped = {}
    for key, name in value.items():
        if not isinstance(key, str) or not key or key != key.strip() or any(ord(ch) < 32 or ch in ' \t' for ch in key):
            raise auth.SafeError('channel_names键必须是完整ChannelID')
        text(name)
        if not name or name != name.strip():
            raise auth.SafeError('channel_names名称无效')
        mapped[key] = name
    return mapped


def default_filter(enabled=True):
    return {'enabled': bool(enabled), 'min_height': DEFAULT_MIN_HEIGHT,
            'exclude_names': list(DEFAULT_EXCLUDE_NAMES)}


def validate_filter(value):
    if isinstance(value, bool):
        return
    if not isinstance(value, dict):
        raise auth.SafeError('filter必须是过滤对象')
    if not isinstance(value.get('enabled'), bool):
        raise auth.SafeError('filter.enabled只能是true或false')
    height = value.get('min_height')
    if isinstance(height, bool) or not isinstance(height, int) or height < 0 or height > 10000:
        raise auth.SafeError('filter.min_height必须是0到10000的整数')
    names = value.get('exclude_names')
    if not isinstance(names, list) or not names or any(not isinstance(item, str) or not item.strip() for item in names):
        raise auth.SafeError('filter.exclude_names必须是非空字符串列表')
    if len(names) > 32 or any(len(item) > 32 for item in names):
        raise auth.SafeError('filter.exclude_names过长')


def normalize_filter(value):
    """Turn a missing field or a legacy bool into a complete saved object."""
    if value is None:
        return default_filter(True)
    if isinstance(value, bool):
        return default_filter(value)
    if isinstance(value, dict):
        merged = default_filter(True)
        merged.update(value)
        validate_filter(merged)
        return {'enabled': merged['enabled'], 'min_height': merged['min_height'],
                'exclude_names': list(merged['exclude_names'])}
    raise auth.SafeError('filter必须是过滤对象')


def filter_rules(config):
    rules = normalize_filter(config.get('filter', default_filter(True)))
    return rules['enabled'], rules['min_height'], tuple(rules['exclude_names'])


def measured_height(row, resolutions):
    """Usable height for filtering only. Missing or unusable facts compare as 0; stored facts stay unchanged."""
    if resolutions is None:
        return 0
    key = source_key(text(row.get('ChannelID')), live_url(row, resolutions['config']))
    fact = resolutions['channels'].get(key)
    if not isinstance(fact, dict) or fact.get('status') not in ('identified', 'played'):
        return 0
    width, height = fact.get('width'), fact.get('height')
    if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
        return 0
    if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        return 0
    return height


def playlist_decision(row, name, config, resolutions):
    """Saved filter rules. resolution.json stores measurements, not this choice."""
    enabled, min_height, names = filter_rules(config)
    if not enabled:
        return 'kept'
    if any(word in name for word in names):
        return 'excluded_name'
    if measured_height(row, resolutions) < min_height:
        return 'excluded_height'
    return 'kept'


def redact_probe(text_value, secrets):
    redacted = text_value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, '[redacted]')
    return redacted


def which_probe():
    for name in ('ffprobe', 'ffmpeg'):
        path = shutil.which(name)
        if path:
            return name, path
    return None, None


def _reap_probe(expired, started_group):
    process = getattr(expired, 'process', None) if started_group else None
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return


def probe_fact(url, now, runner=None):
    """Measure one live URL. Facts only; callers decide filtering."""
    checked = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))
    tool, binary = which_probe()
    empty = dict(checked_at=checked, status='unavailable', codec=None, width=None, height=None,
                 field_order=None, playable=None)
    if tool is None:
        return empty
    if runner is None:
        runner = subprocess.run
    if tool == 'ffprobe':
        command = [binary, '-hide_banner', '-loglevel', 'error', '-rw_timeout', '8000000',
                   '-probesize', '5000000', '-analyzeduration', '5000000',
                   '-select_streams', 'v:0', '-show_entries', 'stream=codec_name,width,height,field_order',
                   '-of', 'json', url]
    else:
        command = [binary, '-hide_banner', '-nostdin', '-loglevel', 'info', '-rw_timeout', '8000000',
                   '-probesize', '5000000', '-analyzeduration', '5000000',
                   '-i', url, '-map', '0:v:0', '-frames:v', '1', '-f', 'null', '-']
    try:
        result = runner(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=PROBE_TIMEOUT, check=False, start_new_session=(runner is subprocess.run))
    except subprocess.TimeoutExpired as expired:
        _reap_probe(expired, runner is subprocess.run)
        return dict(empty, status='timeout')
    except OSError:
        return dict(empty, status='error')
    output = ((result.stdout or b'') + b'\n' + (result.stderr or b'')).decode('utf-8', 'replace')
    output = redact_probe(output, (url,))
    failed = dict(empty, status='failed')
    if tool == 'ffprobe':
        try:
            parsed = json.loads(result.stdout.decode('utf-8', 'replace') or '{}')
            stream = (parsed.get('streams') or [None])[0]
        except (json.JSONDecodeError, IndexError, AttributeError, UnicodeError):
            stream = None
        if not isinstance(stream, dict) or result.returncode != 0:
            return failed
        width, height = stream.get('width'), stream.get('height')
        if isinstance(width, bool) or not isinstance(width, int):
            width = None
        if isinstance(height, bool) or not isinstance(height, int):
            height = None
        codec, field = stream.get('codec_name'), stream.get('field_order')
        return dict(empty, status='identified', codec=codec if isinstance(codec, str) else None, width=width,
                    height=height, field_order=field if isinstance(field, str) else None)
    matched = re.search(r'Video:\s*(\S+).*?,\s*(\d{2,5})x(\d{2,5})', output)
    if result.returncode != 0 or matched is None:
        if 'Connection refused' in output or 'Connection timed out' in output or 'Server returned' in output:
            return dict(empty, status='unreachable')
        return failed
    playable = 'frame=' in output and not re.search(r'\bframe=\s*0\b', output)
    return dict(empty, status='played' if playable else 'identified', playable=playable,
                codec=matched.group(1).rstrip(','), width=int(matched.group(2)), height=int(matched.group(3)))


def unique_source_count(rows, config):
    return len({source_key(text(row.get('ChannelID')), live_url(row, config)) for row in rows})


def resolution_targets(rows, config, stored, force=False):
    selected, seen = [], set()
    for index, row in enumerate(rows):
        url = live_url(row, config)
        key = source_key(text(row.get('ChannelID')), url)
        if key in seen:
            continue
        seen.add(key)
        if force or key not in stored:
            selected.append((index, key, url))
    return selected


def _save_facts(path, facts):
    auth.atomic_json(path, {'schema': 1, 'channels': facts})


def _measure(key, url, moment, runner):
    return key, probe_fact(url, moment, runner=runner)


def refresh_resolutions(paths, rows, config, now=None, force=False, runner=None, progress=None):
    """Measure missing or forced sources. Persist finished facts every batch."""
    path = resolution_path(paths)
    stored = {} if force else load_resolutions(path)['channels']
    targets = resolution_targets(rows, config, stored, force=force)
    facts = dict(stored)
    moment = time.time() if now is None else now
    total = len(targets)
    workers = 1 if runner is not None else PROBE_CONCURRENCY
    done = 0
    for start in range(0, total, PROBE_SAVE_EVERY):
        batch = targets[start:start + PROBE_SAVE_EVERY]
        if workers == 1:
            measured = [_measure(key, url, moment, runner) for _index, key, url in batch]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                measured = list(pool.map(lambda item: _measure(item[1], item[2], moment, runner), batch))
        for key, fact in measured:
            facts[key] = fact
            done += 1
            if progress is not None:
                progress(done, total, fact)
            else:
                print('分辨率进度：%d/%d %s' % (done, total, fact.get('status')), flush=True)
        _save_facts(path, facts)
    value = {'schema': 1, 'channels': facts}
    if not targets and auth.regular(path, missing=True) is None:
        _save_facts(path, facts)
    return targets, value


def print_resolution_summary(targets, value, total):
    states = {}
    for _index, key, _url in targets:
        status = value['channels'][key].get('status')
        states[status] = states.get(status, 0) + 1
    measured = ', '.join('%s %d' % item for item in sorted(states.items())) or '无'
    print('分辨率：检查 %d，未变跳过 %d，本次 %s' % (len(targets), max(0, total - len(targets)), measured))


def resolutions_for(paths, config):
    path = resolution_path(paths)
    if auth.regular(path, missing=True) is None:
        return {'config': config, 'channels': {}}
    return {'config': config, 'channels': load_resolutions(path)['channels']}


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


def ensure_filter_default(config_path):
    """Persist a complete filter object. Keep an explicit enabled value and custom rules."""
    c = load(config_path)
    current = c.get('filter')
    if isinstance(current, dict) and set(current) >= {'enabled', 'min_height', 'exclude_names'}:
        validate_filter(current)
        return c
    c['filter'] = normalize_filter(current)
    validate(c)
    auth.atomic_json(config_path, c)
    return c


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
        c = ensure_filter_default(config)
        auth.atomic_json(metadata_path(paths), directory)
        auth.atomic_json(output, rows)
        if context.get('http') and context.get('framecode'):
            try:
                auth.atomic_json(session_path(paths), portal_session_payload(c, context))
            except auth.SafeError:
                pass
        try:
            refresh_after_fetch(paths)
        except Exception:
            print('分辨率检查未完成，已保留已有记录；列表仍会生成', file=sys.stderr)
        print(f'fetch成功：{len(rows)}条完整原始记录；目录已刷新')


def open_portal(paths, c):
    """Reuse the saved portal session. Login only when it cannot list channels."""
    path = session_path(paths)
    if auth.regular(path, missing=True):
        http = auth.HTTP(c)
        http.deadline = time.monotonic() + 180
        try:
            host, referer = restore_portal(c, auth.read_json(path), http)
            directory = directory_from_portal(http, host, referer)
            return http, host, referer, directory, None
        except Exception:
            pass
    ip = auth.local_ipv4(c['interface'])
    context = {}
    auth.login(c, ip, context=context)
    directory = portal_directory(c, context)
    payload = portal_session_payload(c, context)
    host, referer = restore_portal(c, payload, context['http'])
    return context['http'], host, referer, directory, context


def collect_programmes(http, host, referer, rows, directory, now=None):
    start_ms, end_ms = programme_window(now)
    programmes = {}
    seen = set()
    for row, item in guide_rows(rows, directory):
        channel_id = text(row.get('ChannelID'))
        if not channel_id or channel_id in seen:
            continue
        programmes[channel_id] = channel_programmes(http, host, referer, item, start_ms, end_ms)
        seen.add(channel_id)
    if not programmes:
        raise auth.SafeError('没有可匹配的节目单频道')
    return programmes


def refresh_epg(paths, now=None):
    """Refresh programmes without replacing saved catchup addresses."""
    d, config, source, _ = paths
    with auth.lock(d, '.iptv.lock'):
        c = load(config)
        if auth.regular(source, missing=True) is None:
            raise SafeError('缺少catchup.json；请先执行run或带身份参数获取频道')
        before = source.read_bytes(), config.read_bytes()
        rows = auth.read_json(source)
        if not isinstance(rows, list) or not rows:
            raise SafeError('原始频道数组为空或无效')
        try:
            http, host, referer, directory, context = open_portal(paths, c)
            programmes = collect_programmes(http, host, referer, rows, directory, now)
            document = render_xmltv(rows, directory, programmes)
            session = portal_session_payload(c, context) if context is not None else None
        except Exception as exc:
            raise SafeError('节目单获取失败，保留上次节目单') from exc
        if source.read_bytes() != before[0] or config.read_bytes() != before[1]:
            raise SafeError('节目单获取失败，保留上次节目单')
        auth.atomic_json(metadata_path(paths), directory)
        atomic_text(epg_path(paths), document)
        if session is not None:
            auth.atomic_json(session_path(paths), session)
        print('节目单已更新：' + str(epg_path(paths).resolve()) + '；回看地址未改')
        return context is not None


def publish_playlist(paths, destination):
    guide = epg_path(paths)
    if destination is not None and destination.parent != paths[0] and auth.regular(guide, missing=True):
        copy = destination.parent / 'epg.xml'
        if ensure_output_dir(destination):
            try:
                protected_export(copy, paths)
                atomic_text(copy, guide.read_text())
            except OSError:
                print('节目单副本写入失败。原节目单在：' + str(guide.resolve()), file=sys.stderr)
    convert(paths, destination=destination)


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
    guide = epg_path(paths)
    session = session_path(paths)
    protected = [config, source, metadata, guide, session, resolution_path(paths), d / '.iptv.lock', Path(__file__), output]
    if (export.resolve() in [p.resolve() for p in protected] or
            export.name in ('config.json', 'catchup.json', 'channels.json', 'resolution.json', 'session.json', '.iptv.lock') or
            (export.name == 'epg.xml' and export.parent == d) or
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
        result = playlist_body(rows, c, directory, playlist_header(paths, c), resolutions=resolutions_for(paths, c))
        text_value, decisions = result
        if decisions['kept'] == 0:
            raise SafeError('过滤后没有可输出的频道，保留上次播放列表')
        if output.resolve() == Path(__file__).resolve():
            raise SafeError('主输出不能覆盖运行脚本')
        atomic_text(output, text_value)
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
                    atomic_text(destination, text_value)
                    shown = destination.resolve()
                except SafeError:
                    raise
                except OSError:
                    print('导出写入失败。原播放列表在：' + str(output.resolve()), file=sys.stderr)
                    raise SafeError('导出写入失败。原播放列表在：' + str(output.resolve())) from None
        if not export_skipped:
            print('本次生成的播放列表在：' + str(shown))
        _, counts = resolve_names(rows, directory, c)
        print(f'convert成功：{decisions["kept"]}条M3U记录；名称 ' + json.dumps(counts, ensure_ascii=False))
        enabled, min_height, names = filter_rules(c)
        if not enabled:
            print('过滤：已关闭')
        else:
            print('过滤：高度不足 %d，名称排除 %d' % (
                decisions['excluded_height'], decisions['excluded_name']))


def refresh_after_fetch(paths, now=None, runner=None):
    """Probe only new channel ids, changed live URLs, and sources with no saved fact."""
    c = load(paths[1])
    rows = auth.read_json(paths[2])
    targets, value = refresh_resolutions(paths, rows, c, now=now, runner=runner)
    print_resolution_summary(targets, value, unique_source_count(rows, c))
    return targets


def manual_resolution(paths, now=None, runner=None):
    c = load(paths[1])
    if auth.regular(paths[2], missing=True) is None:
        raise SafeError('缺少catchup.json；请先执行run或带身份参数获取频道')
    rows = auth.read_json(paths[2])
    if not isinstance(rows, list) or not rows:
        raise SafeError('原始频道数组为空或无效')
    targets, value = refresh_resolutions(paths, rows, c, now=now, force=True, runner=runner)
    print_resolution_summary(targets, value, unique_source_count(rows, c))
    return targets


COMMANDS = ('configure', 'fetch', 'convert', 'run', 'epg', 'resolution')
FIELDS = {'userid', 'sn', 'mac', 'interface', 'multicast_interface', 'fcc_interface',
          'auth_origin', 'base_url', 'live_path', 'replay_path', 'seek_template', 'epg_url'}
RETIRED_CLI = {'allowed_origins', 'baseURL'}
ALIASES = {'userinfo': 'userid', 'SN': 'sn', 'MAC': 'mac', 'rtp2http': 'base_url',
           'auth': 'auth_origin', 'live': 'live_path', 'replay': 'replay_path', 'seek': 'seek_template'}


def parse_pairs(argv):
    values = {}
    for arg in argv:
        if '=' not in arg: raise SafeError('参数必须使用key=value，例如 config=tel')
        key, value = arg.split('=', 1)
        key = ALIASES.get(key, key)
        if key == 'directory_metadata':
            raise SafeError('directory_metadata参数已移除，无需设置；fetch/run及全参数获取会同时刷新完整频道目录')
        if key in RETIRED_CLI:
            raise SafeError('未知或重复参数键')
        if key not in FIELDS | {'config', 'output', 'filter'} or key in values:
            raise SafeError('未知或重复参数键')
        values[key] = value
    return values


def parse_filter(value):
    if value == 'true':
        return True
    if value == 'false':
        return False
    raise SafeError('filter只能是true或false')


def config_directory(location):
    if not location: raise SafeError('config不能为空')
    directory = Path(location)
    if not directory.is_absolute(): directory = Path(__file__).resolve().parent / directory
    return directory


def save_filter(paths, enabled):
    """Toggle filter.enabled only. Keeps saved height and name rules. No login or probe."""
    with lock(paths[0]):
        if not regular(paths[1], missing=True):
            raise SafeError('缺少配置；请先完成首次配置')
        c = load(paths[1])
        rules = normalize_filter(c.get('filter'))
        rules['enabled'] = enabled
        c['filter'] = rules
        validate(c)
        atomic_json(paths[1], c)
        print('过滤已' + ('开启' if enabled else '关闭'))


def save_fields(paths, values):
    with lock(paths[0]):
        c = load(paths[1]) if regular(paths[1], missing=True) else {}
        defaults = dict(provider='shanghai-telecom', multicast_interface='', fcc_interface='',
                        auth_origin=DEFAULT_ORIGIN, base_url='http://127.0.0.1:5140',
                        live_path='/rtp/', replay_path='/rtsp/', seek_template=SEEK,
                        allowed_origins=[], filter=default_filter(True))
        for key, value in defaults.items(): c.setdefault(key, value)
        if 'auth_origin' in values:
            values = dict(values)
            apply_auth(values, values.pop('auth_origin'))
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
    publish_playlist(paths, destination)
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
  python3 iptv.py config=tel userinfo=YOUR_USER_ID SN=YOUR_BOX_SN MAC=YOUR_BOX_MAC interface=iptv85 rtp2http=http://127.0.0.1:5140
  python3 iptv.py run config=tel
  python3 iptv.py epg config=tel
  python3 iptv.py resolution config=tel
  python3 iptv.py config=tel
  python3 iptv.py config=tel output=exports

config 相对路径以脚本目录为基准。带身份或播放字段时，合并已有配置、保存并获取后生成。run 重新获取并生成；频道或直播地址新增、变化，或没有分辨率记录时才探测直播。epg 只更新节目单，先用已保存的门户会话，失效才重新登录；不改回看地址，也不探测。只给 config（可再加 output）时离线生成，不探测。resolution 按当前频道重新测量全部分辨率，不生成列表。
已有 tel/epg.xml 时，playlist.m3u 头部写 x-tvg-url="epg.xml"。可选 epg_url=http://访问地址/epg.xml 改为播放器能打开的地址；output 导出到其他目录时，同时复制一份 epg.xml。以 .m3u 结尾的 output 是文件，其他 output 是目录，副本名为配置目录名.m3u。filter 写入 config.json，是对象：enabled、min_height、exclude_names。缺省为 enabled true、min_height 720、exclude_names 含“购物”“理财”“聚鲨”。旧的 true/false 在保存或 run/fetch 补字段时变成该对象，并保留原来的开关；已有自定义高度和关键词不改。比较高度时，没有可用记录、状态不是 identified 或 played、宽或高缺失、不是正整数，都按高度 0；resolution.json 仍只保存实测，不会改成 0。filter=false 只把 enabled 写成 false 并重新生成，不改规则、不登录、不探测。可选 channel_names 是对象，键为完整 ChannelID，值为显示名；只在生成列表时按完整键精确替换，优先于原始名、门户名和数字兜底，并在过滤前生效。不改 ChannelID、tvg-id、catchup.json 或 channels.json。离线生成、epg、resolution 不补写该字段。当前仅上海电信。'''


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
        if 'output' in values and command not in ('convert', 'run', 'epg'):
            raise SafeError('output仅用于离线生成或run')
        location = values.pop('config', None)
        output_value = values.pop('output', None)
        filter_value = values.pop('filter', None)
        if command == 'convert' and not argv:
            raise SafeError('请提供 config=tel；首次示例：' + usage().splitlines()[-3].strip())
        if location is None:
            if command != 'convert' or not values.get('interface'):
                raise SafeError('请提供 config=目录；仅首次全参数可省略config并用interface作目录名')
            location = values['interface']
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', location) or location in ('.', '..'):
                raise SafeError('接口名称无效')
        paths = config_paths(config_directory(location))
        if filter_value is not None:
            if command not in ('convert', 'run'):
                raise SafeError('filter仅用于离线生成或与配置一起保存')
            if configuring and command != 'convert':
                raise SafeError('filter仅用于离线生成或与配置一起保存')
            enabled = parse_filter(filter_value)
            if configuring and regular(paths[1], missing=True):
                values['filter'] = normalize_filter(load(paths[1]).get('filter'))
                values['filter']['enabled'] = enabled
            elif configuring:
                values['filter'] = default_filter(enabled)
            else:
                save_filter(paths, enabled)
        if configuring:
            save_fields(paths, values)
            fetch(paths)
        if command == 'configure':
            configure(paths)
        elif command == 'epg':
            refresh_epg(paths)
            destination = export_path(output_value, paths[0]) if output_value is not None else None
            publish_playlist(paths, destination)
        elif command == 'resolution':
            if output_value is not None:
                raise SafeError('output不用于分辨率重查')
            manual_resolution(paths)
        else:
            if command in ('fetch', 'run'): fetch(paths)
            if command in ('convert', 'run'):
                destination = export_path(output_value, paths[0]) if output_value is not None else None
                publish_playlist(paths, destination)
        return 0
    except SafeError as exc:
        print('IPTV操作失败：' + str(exc), file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        print('IPTV操作失败：请检查配置、权限、原始数据、接口与网络（详情已脱敏）', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
