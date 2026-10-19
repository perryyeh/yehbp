#!/usr/bin/env python3
"""Shanghai Telecom authentication only; Python stdlib + OpenSSL CLI.

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
KNOWN_ORIGINS = {'http://124.75.29.176:7001'}
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


def login(config, ip, http=None):
    http = http or HTTP(config)
    params = dict(UserID=config['userid'], Action='Login', SN=config['sn'], Type='iptv4k', Mode='MENU.SMG-4K', FCCSupport='1')
    url = config['auth_origin'].rstrip('/') + '/iptv3a/4kLogAuth.do?' + urlparse.urlencode(params)
    text, base = http.request('GET', trusted(url, config))
    attrs, fields = form(text)
    action = trusted(urlparse.urljoin(base, attrs.get('action') or ''), config)
    text, base = http.request('POST', action, fields, referer=url)
    attrs, fields = form(text, 'initform')
    fields['authenticator'] = authenticator(js_var(text, 'encrytoken'), config, ip)
    action = trusted(urlparse.urljoin(base, attrs.get('action') or '/iptv3a/ottauth'), config)
    text, _ = http.request('POST', action, fields, referer=base)
    # These markers are the reference client's success criteria after OTT auth.
    js_var(text, 'usertoken')
    if not re.search(r'EPGDomain=([^"\']+)', text) and not re.search(r'\bvar\s+EPGDomain\s*=\s*["\'][^"\']+', text):
        raise SafeError('认证成功标记缺失')
    return channels(text)


def local_ipv4(interface):
    result = subprocess.run(['ip','-j','-4','addr','show','dev',interface,'scope','global'],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=True)
    ips = [str(ipaddress.IPv4Address(x['local'])) for item in json.loads(result.stdout)
           for x in item.get('addr_info',[]) if x.get('family') == 'inet']
    if len(ips) != 1: raise SafeError('所选 IPTV 接口必须有唯一全局 IPv4 地址')
    return ips[0]


def validate(config):
    if config.get('provider', 'shanghai-telecom') != 'shanghai-telecom':
        raise SafeError('当前仅支持上海电信协议')
    for key in ('userid','sn','mac','interface','auth_origin','output'):
        if not isinstance(config.get(key), str) or not config[key] or any(ord(ch)<32 for ch in config[key]):
            raise SafeError('配置字段缺失或无效')
    if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}',config['mac']):
        raise SafeError('盒子 MAC 格式必须为六组十六进制')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}',config['interface']):
        raise SafeError('接口名称无效')
    for value in [config['auth_origin'], *config.get('allowed_origins',[])]:
        p = urlparse.urlsplit(value)
        origin(value)
        if p.path not in ('','/') or p.query: raise SafeError('入口与允许地址必须是 origin（协议、主机、端口）')
    output = Path(config['output'])
    if not output.is_absolute() or output.suffix.lower() != '.json' or output.name == 'identity.json' or output.name.endswith('.auth.json'):
        raise SafeError('输出必须是独立 JSON 绝对路径')
    safe_parents(output.parent)
    regular(output, missing=True)


def config_path(directory):
    directory = Path(directory)
    name = directory.name
    if name.startswith('.iptv-auth-rtp2httpd'):
        name = name[len('.iptv-auth-'):]
        if not re.fullmatch(r'rtp2httpd(?:_[A-Za-z0-9][A-Za-z0-9_-]*)?', name):
            raise SafeError('实例配置目录名无效')
        return directory / (name + '.auth.json')
    return directory / 'identity.json'


def load_config(directory):
    path = config_path(directory)
    info = regular(path)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise SafeError('身份配置必须为当前用户所有且权限 0600')
    config = read_json(path)
    validate(config)
    return config


def instance_interface(config):
    path = config.get('instance_config')
    if path is None:
        return config['interface']
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise SafeError('实例配置路径无效')
    regular(path)
    with open(path, encoding='utf-8') as stream:
        values = re.findall(r'^\s*upstream-interface-multicast\s*=\s*([^\s#;]+)\s*$', stream.read(), re.M)
    if len(values) != 1 or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', values[0]):
        raise SafeError('实例 IPTV 接口缺失或重复')
    return values[0]


def refresh(directory):
    with lock(directory):
        config = load_config(directory)
        config['interface'] = instance_interface(config)
        ip = local_ipv4(config['interface'])
        rows = login(config, ip)
        atomic_json(Path(config['output']), rows)
        print(f'刷新成功：{len(rows)} 条原始频道记录，权限 0600')


def configure(directory, interface, output, instance_config=None):
    with lock(directory):
        path = config_path(directory)
        config = load_config(directory) if regular(path, missing=True) else {}
        print('当前鉴权协议：上海电信；其它运营商尚不支持')
        config['provider'] = 'shanghai-telecom'
        for key, label in [('userid','UserID'),('sn','盒子 SN'),('mac','盒子 MAC')]:
            value = getpass.getpass(f'{label}（隐藏输入，回车保留）: ').strip()
            if value: config[key] = value
        config['interface'] = interface
        if instance_config is not None:
            config['instance_config'] = str(Path(instance_config).absolute())
            instance_interface(config)
        config['output'] = input('catchup JSON 绝对路径（回车保留/使用实例目录）: ').strip() or config.get('output', output)
        config['auth_origin'] = input('认证 origin（回车保留/默认）: ').strip() or config.get('auth_origin',DEFAULT_ORIGIN)
        allowed = input('额外允许 origin，空格分隔（回车保留，输入 - 清空）: ').strip()
        if allowed: config['allowed_origins'] = [] if allowed == '-' else allowed.split()
        config.setdefault('allowed_origins',[])
        validate(config)
        atomic_json(path,config)
        print('身份配置已保存（0600）；未启用定时任务')


def status(directory):
    with lock(directory):
        path = config_path(directory)
        config = load_config(directory) if regular(path, missing=True) else {}
        for key in ('userid','sn','mac','interface','auth_origin','output'):
            print(f'{key}: ' + ('已设置' if config.get(key) else '未设置'))


UNIT_DIR = Path('/etc/systemd/system')
UNIT_MARKER = '# Managed by YehBP IPTV auth\n'


def unit_name(directory):
    return 'yehbp-iptv-auth-' + hashlib.sha256(str(Path(directory).absolute()).encode()).hexdigest()[:20]


def systemctl(*args):
    return subprocess.run(['systemctl', *args], check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=20).stdout.decode().strip()


def unit_quote(value):
    if any(ord(ch) < 32 for ch in value): raise SafeError('服务路径包含控制字符')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def timer(directory, enable):
    directory = Path(directory).absolute()
    # A running refresh owns auth.lock. Control must remain available to stop it.
    with lock(directory, 'timer.lock'):
        name = unit_name(directory)
        service = UNIT_DIR/(name+'.service')
        task = UNIT_DIR/(name+'.timer')
        for path in (service, task):
            if regular(path, missing=True):
                if not path.read_text().startswith(UNIT_MARKER):
                    raise SafeError('同名任务不属于 YehBP，拒绝覆盖或删除')
        if enable:
            with lock(directory):
                instance_interface(load_config(directory))
            helper = str(Path(__file__).absolute())
            contents = [
                UNIT_MARKER + '[Unit]\nDescription=YehBP IPTV authentication\nWants=network-online.target\nAfter=network-online.target\n[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=100\nExecStart=/usr/bin/python3 ' + unit_quote(helper) + ' refresh --directory ' + unit_quote(str(directory)) + '\n',
                UNIT_MARKER + '[Unit]\nDescription=YehBP IPTV daily refresh\n[Timer]\nOnCalendar=*-*-* 05:00:00\nPersistent=true\nUnit='+name+'.service\n[Install]\nWantedBy=timers.target\n']
            safe_parents(UNIT_DIR)
            for path, content in zip((service, task), contents):
                fd, temporary = tempfile.mkstemp(prefix='.iptv-unit-',dir=UNIT_DIR)
                try:
                    with os.fdopen(fd,'w') as stream:
                        os.fchmod(stream.fileno(),0o644)
                        stream.write(content); stream.flush(); os.fsync(stream.fileno())
                    os.replace(temporary,path)
                finally:
                    if os.path.exists(temporary): os.unlink(temporary)
            systemctl('daemon-reload')
            systemctl('enable','--now',name+'.timer')
        else:
            # Only stop/delete units we just verified as ours. Data survives.
            if task.exists(): systemctl('disable','--now',name+'.timer')
            if service.exists(): systemctl('stop',name+'.service')
            for path in (task,service):
                if path.exists(): path.unlink()
            systemctl('daemon-reload')
    timer_status(directory)


def timer_status(directory):
    import datetime
    print('本机时区：' + datetime.datetime.now().astimezone().strftime('%Z %z'))
    name = unit_name(directory)
    if not (UNIT_DIR/(name+'.timer')).exists():
        print('每日刷新：未启用')
        return
    # Only fixed systemd property output, never ExecStart or identity.
    print('每日刷新 next-run：' + systemctl('show',name+'.timer','--property=NextElapseUSecRealtime','--value'))


def main():
    parser = argparse.ArgumentParser(description='IPTV 三步鉴权与原始 channelArray 保存')
    parser.add_argument('command',choices=('configure','refresh','status','validate','enable-daily','disable-daily','timer-status'))
    parser.add_argument('--directory',required=True)
    parser.add_argument('--interface')
    parser.add_argument('--output')
    parser.add_argument('--instance-config')
    args = parser.parse_args()
    try:
        if args.command == 'refresh':
            def expired(*_): raise SafeError('认证总超时')
            signal.signal(signal.SIGALRM,expired)
            signal.alarm(90)
            try: refresh(Path(args.directory))
            finally: signal.alarm(0)
        elif args.command == 'configure':
            if not args.interface or not args.output: raise SafeError('配置需要实例接口与默认输出路径')
            configure(Path(args.directory),args.interface,args.output,args.instance_config)
        elif args.command in ('enable-daily','disable-daily'):
            timer(Path(args.directory), args.command == 'enable-daily')
        elif args.command == 'timer-status':
            timer_status(Path(args.directory))
        elif args.command == 'validate':
            with lock(Path(args.directory)): load_config(Path(args.directory))
        else: status(Path(args.directory))
        return 0
    except (Exception, KeyboardInterrupt):
        # Exceptions may carry full URLs, identifiers, response bodies or tokens.
        # Never interpolate an exception, even when it appears to be a local error.
        print('IPTV 操作失败：请检查配置、权限、锁、接口、可信入口与网络（详情已脱敏）',file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
