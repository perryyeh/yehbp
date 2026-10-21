#!/usr/bin/env python3
"""DIYP playlist and EPG bridge for an iptv.py config directory.

Reads that directory's playlist.m3u and serves DIYP text at /<name>.m3u.
The .m3u suffix is only the interface name; the body stays DIYP text.

  /<config>.m3u                 grouped DIYP text playlist
  /c/<n>/<group>/<name>         302 to the original live URL
  /PLTV/<n>/<group>/<name>      same live redirect
  /TVOD/<n>/<group>/<name>      302 to catchup with the client playseek

An empty group-title is served as the path segment "-". Lookup uses the
channel index plus the display name; the group segment is only a label and
may be empty or "-". It is never used to choose a different upstream URL.
  /<config>.epg?ch=<name>&date=YYYY-MM-DD

Live and catchup query strings are copied as stored. A client playseek only
replaces an existing playseek value, or is appended when that key is absent.
Upstream targets come only from the M3U, never from the request.

EPG is XMLTV from the M3U x-tvg-url list, earlier URLs winning per channel.
A failed source keeps the previous good copy of that URL. Missing programmes
stay empty; nothing is invented.
"""

import argparse
import datetime
import hashlib
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

USER_AGENT = "yehbp-diyp-bridge"
PLAYSEEK_VALUE = re.compile(r"^\d{14}-\d{14}$")
DATE_COMPACT = re.compile(r"^\d{8}$")
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
CONFIG_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5142
PLAYLIST_CHECK_SECONDS = 300
DEFAULT_EPG_REFRESH = 3600
HOST_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,253}(?::[0-9]{1,5})?$")


def norm_key(value):
    return re.sub(r"\s+", "", value or "").casefold()


def parse_xmltv_time(value):
    text = (value or "").strip()
    matched = re.match(r"^(\d{14})(?:\s*([+-])(\d{2})(\d{2}))?$", text)
    if not matched:
        return None
    moment = datetime.datetime.strptime(matched.group(1), "%Y%m%d%H%M%S")
    if matched.group(2):
        sign = 1 if matched.group(2) == "+" else -1
        offset = datetime.timedelta(
            hours=int(matched.group(3)), minutes=int(matched.group(4)))
        moment = moment.replace(tzinfo=datetime.timezone(sign * offset))
    else:
        moment = moment.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    return moment


def local_day(moment, tz):
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(tz)


def header_sources(header, source=""):
    matched = re.search(r'\bx-tvg-url="([^"]*)"', header)
    if not matched:
        return []
    base = ""
    parsed_source = urllib.parse.urlsplit(source) if source else None
    if parsed_source and parsed_source.scheme not in ("http", "https") and "://" not in source:
        base = Path(source).expanduser().resolve().as_posix()
    sources = []
    for item in matched.group(1).split(","):
        item = item.strip()
        if not item:
            continue
        if base and urllib.parse.urlsplit(item).scheme not in ("http", "https"):
            sources.append(str((Path(base).parent / item).resolve()))
        elif base and "://" in base:
            sources.append(urllib.parse.urljoin(base, item))
        else:
            sources.append(item)
    return sources


def split_extinf(line):
    if not line.startswith("#EXTINF"):
        raise ValueError("not an EXTINF line")
    try:
        meta, name = line.split(",", 1)
    except ValueError as exc:
        raise ValueError("EXTINF display separator is missing") from exc
    attrs = {}
    for key, value in ATTR_RE.findall(meta):
        if key in attrs:
            raise ValueError("duplicate EXTINF attribute: " + key)
        attrs[key] = value
    return attrs, name


def parse_m3u(text, source=""):
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    header = next((line for line in lines if line.strip()), "")
    if not header.startswith("#EXTM3U"):
        raise ValueError("M3U header is missing")
    sources = header_sources(header, source)
    channels = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("#EXTINF"):
            index += 1
            continue
        url = ""
        cursor = index + 1
        while cursor < len(lines):
            candidate = lines[cursor].strip()
            if candidate and not candidate.startswith("#"):
                url = lines[cursor]
                break
            if candidate.startswith("#EXTINF"):
                break
            cursor += 1
        if not url:
            raise ValueError("EXTINF without a URL at line %d" % (index + 1))
        attrs, name = split_extinf(line)
        parsed = urllib.parse.urlsplit(url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("live URL is not http(s) at line %d" % (index + 1))
        catchup = attrs.get("catchup-source", "")
        if catchup:
            target = urllib.parse.urlsplit(catchup)
            if target.scheme not in ("http", "https") or not target.netloc:
                raise ValueError("catchup URL is not http(s) at line %d" % (index + 1))
        channels.append({
            "index": len(channels) + 1,
            "group": attrs.get("group-title", ""),
            "name": name,
            "tvg_id": attrs.get("tvg-id", ""),
            "tvg_name": attrs.get("tvg-name", ""),
            "live": url.strip(),
            "catchup": catchup,
        })
        index = cursor + 1
    if not channels:
        raise ValueError("M3U contains no channels")
    return {"sources": sources, "channels": channels}


def load_m3u(source, timeout=20):
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme in ("http", "https"):
        request = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    elif parsed.scheme in ("", "file"):
        path = Path(urllib.request.url2pathname(parsed.path) if parsed.scheme == "file" else source)
        payload = path.read_bytes()
    else:
        raise ValueError("M3U source must be a file path or http(s) URL")
    return parse_m3u(payload.decode("utf-8"), source)


def _query_parts(query):
    if not query:
        return []
    return query.split("&")


def _part_key(part):
    return urllib.parse.unquote_plus(part.split("=", 1)[0]) if part else ""


def apply_playseek(catchup, playseek):
    if not PLAYSEEK_VALUE.fullmatch(playseek or ""):
        raise ValueError("playseek must be 14 digits, a hyphen, and 14 digits")
    parsed = urllib.parse.urlsplit(catchup)
    parts = _query_parts(parsed.query)
    replacement = "playseek=" + playseek
    found = False
    rewritten = []
    for part in parts:
        if _part_key(part) == "playseek":
            rewritten.append(replacement)
            found = True
        else:
            rewritten.append(part)
    if not found:
        rewritten.append(replacement)
    return urllib.parse.urlunsplit(parsed._replace(query="&".join(rewritten)))


def parse_epg_date(value, today):
    text = (value or "").strip()
    if not text:
        return today
    if DATE_COMPACT.fullmatch(text):
        return datetime.datetime.strptime(text, "%Y%m%d").date()
    return datetime.date.fromisoformat(text)


class EpgIndex:
    def __init__(self):
        self.by_key = {}
        self.channel_count = 0
        self.programme_count = 0
        self.errors = []

    def add_xml(self, payload, tz):
        count = 0
        names = {}
        for _event, elem in ET.iterparse(payload, events=("end",)):
            if elem.tag == "channel":
                channel_id = elem.get("id") or ""
                displays = [
                    (node.text or "").strip()
                    for node in elem.findall("display-name")
                    if (node.text or "").strip()
                ]
                if channel_id:
                    names[channel_id] = displays
                elem.clear()
            elif elem.tag == "programme":
                start = parse_xmltv_time(elem.get("start"))
                stop = parse_xmltv_time(elem.get("stop"))
                title = (elem.findtext("title") or "").strip()
                desc = (elem.findtext("desc") or "").strip()
                channel = elem.get("channel") or ""
                if start and stop and title and channel and stop > start:
                    item = (start, stop, title, desc)
                    origin = names.get(channel, [])
                    primary = norm_key(origin[0]) if origin else norm_key(channel)
                    if primary:
                        bucket = self.by_key.setdefault(primary, [])
                        if not any(existing[0] == start and existing[1] == stop for existing in bucket):
                            bucket.append(item)
                    count += 1
                elem.clear()
        self.channel_count += len(names)
        self.programme_count += count
        for key, items in self.by_key.items():
            items.sort(key=lambda item: (item[0], item[1], item[2]))
        return count


def fetch_xml(url, timeout):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        path = Path(urllib.request.url2pathname(parsed.path) if parsed.scheme == "file" else url)
        return path.read_bytes(), None
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
        stamp = response.headers.get("Last-Modified")
    return payload, stamp


def build_epg(sources, cache, tz, timeout=30):
    index = EpgIndex()
    errors = []
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    for url in sources:
        try:
            payload, _stamp = fetch_xml(url, timeout)
            path = cache / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".xml")
            temporary = path.with_suffix(".xml.partial")
            temporary.write_bytes(payload)
            try:
                with temporary.open("rb") as handle:
                    added = index.add_xml(handle, tz)
            except (OSError, ET.ParseError, ValueError):
                temporary.unlink(missing_ok=True)
                raise
            os.replace(temporary, path)
            if added == 0 and b"<programme" not in payload[:4096] and b"<programme" not in payload:
                errors.append(url + ": XMLTV contained no programmes")
        except (OSError, urllib.error.URLError, ET.ParseError, ValueError) as exc:
            cached = cache / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".xml")
            if cached.is_file():
                try:
                    with cached.open("rb") as handle:
                        index.add_xml(handle, tz)
                    errors.append(url + ": refresh failed, kept previous copy (" + type(exc).__name__ + ")")
                    continue
                except (OSError, ET.ParseError, ValueError) as cached_exc:
                    errors.append(url + ": refresh and cache failed (" + type(cached_exc).__name__ + ")")
                    continue
            errors.append(url + ": " + type(exc).__name__)
    index.errors = errors
    return index


def programmes_for(index, keys, day, tz):
    found = []
    seen_slots = set()
    for key in keys:
        for start, stop, title, desc in index.by_key.get(norm_key(key), ()):
            slot = (start, stop)
            if slot in seen_slots or local_day(start, tz).date() != day:
                continue
            seen_slots.add(slot)
            found.append((start, stop, title, desc))
    found.sort(key=lambda item: (item[0], item[1], item[2]))
    return [
        {
            "title": title,
            "start": local_day(start, tz).strftime("%H:%M"),
            "end": local_day(stop, tz).strftime("%H:%M"),
            "desc": desc,
        }
        for start, stop, title, desc in found
    ]


def config_directory(location):
    if not location or not CONFIG_NAME.fullmatch(location):
        raise ValueError("config must be a single directory name, for example tel")
    directory = Path(__file__).resolve().parent / location
    playlist = directory / "playlist.m3u"
    if playlist.resolve() == Path(__file__).resolve():
        raise ValueError("config playlist cannot replace this script")
    return directory, playlist


def request_public_base(host_header, explicit_base):
    if explicit_base:
        return explicit_base.rstrip("/")
    host = (host_header or "").strip()
    if not host or len(host) > 255 or not HOST_VALUE.fullmatch(host):
        raise ValueError("invalid host")
    hostname, separator, port = host.rpartition(":")
    if separator and (not port.isdigit() or int(port) > 65535):
        raise ValueError("invalid host")
    if separator and hostname.endswith("."):
        raise ValueError("invalid host")
    return "http://" + host


class Bridge:
    def __init__(self, playlist, public_base, cache, tz, refresh_seconds, playlist_name="", epg_name=""):
        self.playlist_source = playlist
        self.playlist_name = playlist_name
        self.epg_name = epg_name
        self.public_base = public_base.rstrip("/") if public_base else ""
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.tz = tz
        self.refresh_seconds = refresh_seconds
        self.lock = threading.Lock()
        self.channels = []
        self.routes = {}
        self.sources = []
        self.epg = EpgIndex()
        self.m3u_mtime = None
        self.last_epg = 0
        self.epg_sources = None

    def reload_m3u(self, force=False):
        parsed = urllib.parse.urlsplit(self.playlist_source)
        mtime = None
        if parsed.scheme not in ("http", "https"):
            path = Path(self.playlist_source)
            mtime = path.stat().st_mtime_ns
            if not force and mtime == self.m3u_mtime:
                return False
        loaded = load_m3u(self.playlist_source)
        routes = {}
        for channel in loaded["channels"]:
            routes[(channel["index"], channel["name"])] = channel
        with self.lock:
            self.channels = loaded["channels"]
            self.routes = routes
            self.sources = loaded["sources"]
            self.m3u_mtime = mtime
        return True

    def reload_epg(self, force=False):
        now = time.monotonic()
        with self.lock:
            sources = list(self.sources)
            sources_changed = self.epg_sources is not None and sources != self.epg_sources
        if not force and not sources_changed and now - self.last_epg < self.refresh_seconds:
            return False
        fresh = build_epg(sources, self.cache, self.tz)
        with self.lock:
            self.epg = fresh
            self.last_epg = now
            self.epg_sources = sources
        return True

    def refresh_playlist(self):
        self.reload_m3u()

    def refresh_epg(self):
        self.reload_epg()

    def refresh_loop(self):
        playlist_due = time.monotonic() + PLAYLIST_CHECK_SECONDS
        epg_due = time.monotonic() + self.refresh_seconds
        while True:
            now = time.monotonic()
            wait = min(playlist_due, epg_due) - now
            if wait > 0:
                time.sleep(wait)
            now = time.monotonic()
            if now >= playlist_due:
                playlist_due = now + PLAYLIST_CHECK_SECONDS
                try:
                    self.refresh_playlist()
                except (OSError, urllib.error.URLError, ValueError) as exc:
                    print("playlist refresh failed: %s" % type(exc).__name__, file=sys.stderr, flush=True)
            if now >= epg_due:
                epg_due = now + self.refresh_seconds
                try:
                    self.refresh_epg()
                except (OSError, urllib.error.URLError, ValueError) as exc:
                    print("epg refresh failed: %s" % type(exc).__name__, file=sys.stderr, flush=True)

    def origin(self, host_header=None):
        return request_public_base(host_header, self.public_base)

    def playlist_text(self, host_header=None):
        origin = self.origin(host_header)
        with self.lock:
            channels = list(self.channels)
        groups = []
        buckets = {}
        for channel in channels:
            if channel["group"] not in buckets:
                groups.append(channel["group"])
                buckets[channel["group"]] = []
            buckets[channel["group"]].append(channel)
        rows = []
        for group in groups:
            rows.append("%s,#genre#" % (group if "," not in group else group.replace(",", "，")))
            for channel in buckets[group]:
                segment = channel["group"] if channel["group"] else "-"
                path = "/c/%d/%s/%s" % (
                    channel["index"],
                    urllib.parse.quote(segment, safe=""),
                    urllib.parse.quote(channel["name"], safe=""),
                )
                rows.append("%s,%s%s" % (channel["name"], origin, path))
        return "\n".join(rows) + "\n"

    def lookup(self, index, group, name):
        del group  # path label only; index and display name select the channel
        with self.lock:
            return self.routes.get((index, name))

    def epg_document(self, name, raw_date, host_header=None):
        today = datetime.datetime.now(self.tz).date()
        try:
            day = parse_epg_date(raw_date, today)
        except ValueError:
            day = today
        with self.lock:
            channels = [
                channel for channel in self.channels
                if channel["name"] == name or channel["tvg_name"] == name or channel["tvg_id"] == name
            ]
            index = self.epg
            errors = list(self.epg.errors)
        keys = []
        for channel in channels:
            for key in (channel["tvg_id"], channel["tvg_name"], channel["name"]):
                if key and key not in keys:
                    keys.append(key)
        if not keys and name:
            keys.append(name)
        data = programmes_for(index, keys, day, self.tz) if keys else []
        return {
            "date": day.isoformat(),
            "channel_name": name,
            "url": self.origin(host_header) + "/" + self.epg_name,
            "epg_data": data,
            "errors": errors,
        }


class BridgeServer(http.server.ThreadingHTTPServer):
    def __init__(self, address, handler, bridge):
        self.bridge = bridge
        super().__init__(address, handler)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        path = urllib.parse.urlsplit(self.path).path
        status = args[1] if len(args) > 1 else ""
        print("%s %s %s" % (self.command, path, status), flush=True)

    def send_bytes(self, payload, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_text(self, text, content_type, status=200):
        self.send_bytes(text.encode("utf-8"), content_type, status)

    def redirect(self, target):
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        bridge = self.server.bridge  # type: ignore[attr-defined]
        parsed = urllib.parse.urlsplit(self.path)
        try:
            path = urllib.parse.unquote(parsed.path, errors="strict")
        except UnicodeDecodeError:
            self.send_text("not found\n", "text/plain; charset=utf-8", 404)
            return
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        playlist_path = "/" + bridge.playlist_name if bridge.playlist_name else ""
        epg_path = "/" + bridge.epg_name if bridge.epg_name else ""
        if playlist_path and path == playlist_path:
            try:
                text = bridge.playlist_text(self.headers.get("Host"))
            except ValueError:
                self.send_text("invalid host\n", "text/plain; charset=utf-8", 400)
                return
            self.send_text(text, "text/plain; charset=utf-8")
            return
        if epg_path and path == epg_path:
            name = (query.get("ch") or query.get("name") or [""])[0]
            raw_date = (query.get("date") or [""])[0]
            if len(name) > 200 or len(raw_date) > 32:
                self.send_text("invalid request\n", "text/plain; charset=utf-8", 400)
                return
            try:
                document = bridge.epg_document(name, raw_date, self.headers.get("Host"))
            except ValueError:
                self.send_text("invalid host\n", "text/plain; charset=utf-8", 400)
                return
            body = json.dumps(document, ensure_ascii=False)
            self.send_text(body, "application/json; charset=utf-8")
            return
        if path == "/health":
            with bridge.lock:
                summary = {
                    "channels": len(bridge.channels),
                    "catchup": sum(1 for channel in bridge.channels if channel["catchup"]),
                    "epg_channels": bridge.epg.channel_count,
                    "programmes": bridge.epg.programme_count,
                    "epg_errors": len(bridge.epg.errors),
                }
            self.send_text(json.dumps(summary), "application/json; charset=utf-8")
            return
        raw_segments = parsed.path.split("/")
        if (
            len(raw_segments) < 5
            or raw_segments[0]
            or raw_segments[1] not in ("c", "PLTV", "TVOD")
            or not raw_segments[2].isdigit()
            or any(not item for item in raw_segments[4:])
        ):
            self.send_text("not found\n", "text/plain; charset=utf-8", 404)
            return
        kind = raw_segments[1]
        try:
            index = int(raw_segments[2])
            name = urllib.parse.unquote("/".join(raw_segments[4:]), errors="strict")
        except (ValueError, UnicodeDecodeError):
            self.send_text("not found\n", "text/plain; charset=utf-8", 404)
            return
        channel = bridge.lookup(index, raw_segments[3], name)
        if channel is None or kind not in ("c", "PLTV", "TVOD"):
            self.send_text("not found\n", "text/plain; charset=utf-8", 404)
            return
        playseek = (query.get("playseek") or [""])[0]
        if kind == "TVOD":
            if not channel["catchup"]:
                self.send_text("catchup unavailable\n", "text/plain; charset=utf-8", 404)
                return
            if not playseek or len(query) != 1:
                self.send_text("playseek required\n", "text/plain; charset=utf-8", 400)
                return
            try:
                target = apply_playseek(channel["catchup"], playseek)
            except ValueError:
                self.send_text("invalid playseek\n", "text/plain; charset=utf-8", 400)
                return
        else:
            if parsed.query:
                self.send_text("invalid request\n", "text/plain; charset=utf-8", 400)
                return
            target = channel["live"]
        self.redirect(target)


def serve(playlist, host, port, public_base, cache, tz_name, refresh_seconds, playlist_name="", epg_name=""):
    if tz_name != "Asia/Shanghai" and tz_name != "UTC+8":
        raise ValueError("only Asia/Shanghai or UTC+8 is supported")
    if refresh_seconds < 1:
        raise ValueError("refresh must be at least 1 second")
    tz = datetime.timezone(datetime.timedelta(hours=8), name="Asia/Shanghai")
    bridge = Bridge(playlist, public_base, cache, tz, refresh_seconds, playlist_name, epg_name)
    bridge.reload_m3u(force=True)
    try:
        bridge.reload_epg(force=True)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        print("initial EPG failed: %s" % type(exc).__name__, file=sys.stderr, flush=True)
    threading.Thread(target=bridge.refresh_loop, daemon=True).start()
    server = BridgeServer((host, port), Handler, bridge)
    print(
        "listening %s:%d channels=%d catchup=%d programmes=%d epg_errors=%d"
        % (
            host,
            server.server_address[1],
            len(bridge.channels),
            sum(1 for channel in bridge.channels if channel["catchup"]),
            bridge.epg.programme_count,
            len(bridge.epg.errors),
        ),
        flush=True,
    )
    return server


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Serve DIYP text and EPG from an iptv.py config")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--public-base", default="",
        help="optional external origin, including a path prefix; otherwise use the request Host")
    parser.add_argument("--cache", default="")
    parser.add_argument("--tz", default="Asia/Shanghai")
    parser.add_argument(
        "--refresh", type=int, default=DEFAULT_EPG_REFRESH,
        help="seconds between EPG source checks; playlist mtime is checked every 300 seconds")
    known, rest = parser.parse_known_args(argv)
    config = None
    for item in rest:
        if item.startswith("-"):
            parser.error("unrecognized arguments: " + item)
        if not item.startswith("config=") or config is not None:
            parser.error("use config=tel")
        config = item.split("=", 1)[1]
    if not config:
        parser.error("config=tel is required")
    known.config = config
    return known


def main(argv=None):
    args = parse_args(argv)
    cache = args.cache or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "yehbp-diyp-" + args.config)
    try:
        _directory, playlist = config_directory(args.config)
        server = serve(
            str(playlist), args.host, args.port, args.public_base,
            cache, args.tz, args.refresh, args.config + ".m3u", args.config + ".epg")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print("start failed: %s" % exc.__class__.__name__, file=sys.stderr)
        return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
