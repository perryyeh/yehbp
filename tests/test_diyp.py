"""DIYP bridge parsing, redirects, and EPG selection. Synthetic fixtures only."""

import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "yehbp_diyp", ROOT / "assets/iptv/diyp.py")
assert SPEC is not None and SPEC.loader is not None
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)

TZ = timezone(timedelta(hours=8))
LIVE = "http://127.0.0.1:5140/rtp/233.18.204.52:5140?fcc=124.75.26.151:15970"
CATCHUP = (
    "http://127.0.0.1:5140/rtsp/example.test:554/live/1"
    "?AuthInfo=A%2Fb+C==&fcc=124.75.26.151%3A15970"
    "&playseek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}"
)
M3U = """#EXTM3U x-tvg-url="http://127.0.0.1/first.xml,http://127.0.0.1/second.xml"
#EXTINF:-1 tvg-id="CCTV1" tvg-name="CCTV1" group-title="央视" catchup="default" catchup-source="%s" catchup-days="7",CCTV1综合
%s
#EXTINF:-1 tvg-id="CCTV4K" tvg-name="CCTV4K" group-title="4K" catchup="default" catchup-source="%s",CCTV4K
http://127.0.0.1:5140/rtp/233.18.204.1:5140?fcc=1
#EXTINF:-1 tvg-id="CCTV4K" tvg-name="CCTV4K" group-title="4K" catchup="default" catchup-source="%s",CCTV4K
http://127.0.0.1:5140/rtp/233.18.204.2:5140?fcc=2&x=a%%3Db
#EXTINF:-1 tvg-id="本地" tvg-name="本地" group-title="上海台",只有直播
http://127.0.0.1:5140/rtp/233.18.204.3:5140
""" % (CATCHUP, LIVE, CATCHUP, CATCHUP)

FIRST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="1"><display-name lang="zh">CCTV1</display-name></channel>
  <programme channel="1" start="20261021004500 +0800" stop="20261021013000 +0800">
    <title>早间</title><desc>甲</desc>
  </programme>
  <programme channel="1" start="20261021233000 +0800" stop="20261022003000 +0800">
    <title>跨天</title>
  </programme>
  <programme channel="1" start="20261022013000 +0800" stop="20261022020000 +0800">
    <title>次日</title>
  </programme>
</tv>
"""
SECOND_XML = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="CCTV1"><display-name>CCTV1</display-name></channel>
  <channel id="本地"><display-name>本地</display-name></channel>
  <programme channel="CCTV1" start="20261021004500 +0800" stop="20261021013000 +0800">
    <title>不应覆盖</title>
  </programme>
  <programme channel="CCTV1" start="20261021020000 +0800" stop="20261021030000 +0800">
    <title>补充</title>
  </programme>
  <programme channel="本地" start="20261021100000 +0800" stop="20261021110000 +0800">
    <title>本地节目</title>
  </programme>
</tv>
"""


class ParseTests(unittest.TestCase):
    def test_keeps_order_duplicates_and_query_bytes(self):
        loaded = bridge.parse_m3u(M3U)
        self.assertEqual(loaded["sources"], [
            "http://127.0.0.1/first.xml", "http://127.0.0.1/second.xml"])
        self.assertEqual([item["name"] for item in loaded["channels"]],
                         ["CCTV1综合", "CCTV4K", "CCTV4K", "只有直播"])
        self.assertEqual(loaded["channels"][0]["live"], LIVE)
        self.assertEqual(loaded["channels"][0]["catchup"], CATCHUP)
        self.assertIn("x=a%3Db", loaded["channels"][2]["live"])

    def test_playseek_replaces_only_that_key(self):
        updated = bridge.apply_playseek(CATCHUP, "20261021004500-20261021013000")
        self.assertIn("AuthInfo=A%2Fb+C==", updated)
        self.assertIn("fcc=124.75.26.151%3A15970", updated)
        self.assertIn("playseek=20261021004500-20261021013000", updated)
        self.assertNotIn("${(b)", updated)
        self.assertEqual(updated.count("playseek="), 1)

    def test_playseek_appends_when_absent(self):
        original = "http://127.0.0.1/rtsp/a?AuthInfo=A%2Fb+C=="
        updated = bridge.apply_playseek(original, "20261021004500-20261021013000")
        self.assertEqual(
            updated,
            original + "&playseek=20261021004500-20261021013000")

    def test_rejects_malformed_playseek_and_m3u(self):
        with self.assertRaises(ValueError):
            bridge.apply_playseek(CATCHUP, "2026-10-21")
        with self.assertRaises(ValueError):
            bridge.parse_m3u("#EXTM3U\n#EXTINF:-1,名称\nrtp://239.0.0.1:1\n")
        with self.assertRaises(ValueError):
            bridge.parse_m3u('#EXTM3U\n#EXTINF:-1 tvg-id="a" tvg-id="b",名称\nhttp://127.0.0.1/a\n')


class EpgTests(unittest.TestCase):
    def test_first_source_wins_and_midnight_stays_on_start_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)

            def fake_fetch(url, timeout):
                return ((FIRST_XML if url.endswith("first.xml") else SECOND_XML).encode(), None)

            with patch.object(bridge, "fetch_xml", side_effect=fake_fetch):
                index = bridge.build_epg(
                    ["http://127.0.0.1/first.xml", "http://127.0.0.1/second.xml"], cache, TZ)
            day = datetime(2026, 10, 21).date()
            rows = bridge.programmes_for(index, ["CCTV1"], day, TZ)
            self.assertEqual([row["title"] for row in rows], ["早间", "补充", "跨天"])
            self.assertEqual(rows[0]["start"], "00:45")
            self.assertEqual(rows[2]["end"], "00:30")
            next_day = bridge.programmes_for(index, ["CCTV1"], day + timedelta(days=1), TZ)
            self.assertEqual([row["title"] for row in next_day], ["次日"])
            local = bridge.programmes_for(index, ["本地"], day, TZ)
            self.assertEqual(local[0]["title"], "本地节目")

    def test_relative_epg_resolves_beside_playlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            playlist = root / "playlist.m3u"
            playlist.write_text(
                '#EXTM3U x-tvg-url="epg.xml"\n#EXTINF:-1 tvg-id="42" tvg-name="42",频道\nhttp://127.0.0.1/a\n',
                encoding="utf-8")
            (root / "epg.xml").write_text(SECOND_XML, encoding="utf-8")
            loaded = bridge.load_m3u(str(playlist))
            self.assertEqual(loaded["sources"], [str((root / "epg.xml").resolve())])
            cache = root / "cache"
            index = bridge.build_epg(loaded["sources"], cache, TZ)
            rows = bridge.programmes_for(index, ["本地"], datetime(2026, 10, 21).date(), TZ)
            self.assertEqual(rows[0]["title"], "本地节目")

    def test_failed_source_keeps_previous_copy_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            payloads = {"http://127.0.0.1/first.xml": FIRST_XML.encode()}

            def fake_fetch(url, timeout):
                if url not in payloads:
                    raise OSError("down")
                return payloads[url], None

            with patch.object(bridge, "fetch_xml", side_effect=fake_fetch):
                first = bridge.build_epg(["http://127.0.0.1/first.xml"], cache, TZ)
                self.assertEqual(first.programme_count, 3)
                payloads.clear()
                second = bridge.build_epg(["http://127.0.0.1/first.xml"], cache, TZ)
            self.assertEqual(second.programme_count, 3)
            self.assertEqual(len(second.errors), 1)
            self.assertIn("kept previous copy", second.errors[0])
            self.assertNotIn("不应覆盖", json.dumps(second.by_key, default=str))


class ServerTests(unittest.TestCase):
    def fake_fetch(self, url, timeout):
        body = FIRST_XML if url.endswith("first.xml") else SECOND_XML
        return body.encode(), None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.playlist = Path(self.tmp.name) / "channels.m3u"
        self.playlist.write_text(M3U, encoding="utf-8")
        self.cache = Path(self.tmp.name) / "cache"
        self.bridge = bridge.Bridge(
            str(self.playlist), "http://127.0.0.1:9", self.cache, TZ, 3600, "tel.m3u", "tel.epg")
        self.bridge.reload_m3u(force=True)

        with patch.object(bridge, "fetch_xml", side_effect=self.fake_fetch):
            self.bridge.reload_epg(force=True)
        self.httpd = bridge.BridgeServer(("127.0.0.1", 0), bridge.Handler, self.bridge)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def open(self, path, host=None):
        return self.open_at(self.port, path, host)

    def open_at(self, port, path, host=None):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        no_redirect = urllib.request.build_opener(NoRedirect)
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
        if host is not None:
            request.add_header("Host", host)
        response = None
        try:
            response = no_redirect.open(request, timeout=5)
            return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            result = exc.code, dict(exc.headers), payload
            exc.close()
            return result
        finally:
            if response is not None:
                response.close()

    def test_playlist_routes_and_epg(self):
        status, _headers, body = self.open("/tel.m3u")
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertEqual(text.splitlines()[0], "央视,#genre#")
        self.assertEqual(text.splitlines()[0].count(","), 1)
        self.assertIn("CCTV1综合,http://127.0.0.1:9/PLTV/1/", text)
        self.assertEqual(text.count("CCTV4K,http://127.0.0.1:9/PLTV/"), 2)
        self.assertIn("/PLTV/2/", text)
        self.assertIn("/PLTV/3/", text)
        self.assertIn("只有直播,http://127.0.0.1:9/c/4/", text)
        self.assertNotIn("/PLTV/4/", text)

        status, headers, payload = self.open("/PLTV/1/%E5%A4%AE%E8%A7%86/CCTV1%E7%BB%BC%E5%90%88")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], LIVE)
        self.assertNotIn("AuthInfo", payload.decode())
        status, headers, payload = self.open("/c/1/%E5%A4%AE%E8%A7%86/CCTV1%E7%BB%BC%E5%90%88")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], LIVE)

        issued = next(line for line in text.splitlines() if line.startswith("CCTV1综合,"))
        play = issued.split(",", 1)[1].replace("/PLTV/", "/TVOD/", 1)
        play += "?playseek=20261021004500-20261021013000"
        status, headers, _payload = self.open(play.split(":9", 1)[1])
        self.assertEqual(status, 302)
        location = headers["Location"]
        self.assertIn("AuthInfo=A%2Fb+C==", location)
        self.assertIn("fcc=124.75.26.151%3A15970", location)
        self.assertIn("playseek=20261021004500-20261021013000", location)
        self.assertNotIn("${(b)", location)

        status, _headers, payload = self.open("/tel.epg?ch=CCTV1%E7%BB%BC%E5%90%88&date=2026-10-21")
        document = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual([row["title"] for row in document["epg_data"]], ["早间", "补充", "跨天"])
        self.assertEqual(document["epg_data"][0]["desc"], "甲")
        self.assertEqual(document["url"], "http://127.0.0.1:9/tel.epg")
        status, _headers, _payload = self.open("/epg?ch=CCTV1%E7%BB%BC%E5%90%88&date=2026-10-21")
        self.assertEqual(status, 404)

    def test_host_header_builds_links_unless_public_base_overrides(self):
        dynamic = bridge.Bridge(str(self.playlist), "", self.cache, TZ, 3600, "tel.m3u", "tel.epg")
        dynamic.reload_m3u(force=True)
        server = bridge.BridgeServer(("127.0.0.1", 0), bridge.Handler, dynamic)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            status, _headers, payload = self.open_at(port, "/tel.m3u", "player.example:5142")
            self.assertEqual(status, 200)
            self.assertIn("CCTV1综合,http://player.example:5142/PLTV/1/", payload.decode())
            status, _headers, payload = self.open_at(
                port, "/tel.epg?ch=CCTV1%E7%BB%BC%E5%90%88&date=2026-10-21", "player.example:5142")
            document = json.loads(payload)
            self.assertEqual(status, 200)
            self.assertEqual(document["url"], "http://player.example:5142/tel.epg")
            status, _headers, payload = self.open_at(port, "/tel.m3u", "bad host")
            self.assertEqual(status, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        for bad in ("", "http://evil.test", "player.example:99999", "a b", "player.example/extra"):
            with self.assertRaises(ValueError):
                dynamic.origin(bad)

        prefixed = bridge.Bridge(
            str(self.playlist), "http://player.example:80/lucky/iptv/", self.cache, TZ, 3600, "tel.m3u", "tel.epg")
        prefixed.reload_m3u(force=True)
        with patch.object(bridge, "fetch_xml", side_effect=self.fake_fetch):
            prefixed.reload_epg(force=True)
        text = prefixed.playlist_text("ignored.example:1")
        self.assertIn("CCTV1综合,http://player.example:80/lucky/iptv/PLTV/1/", text)
        document = prefixed.epg_document("CCTV1综合", "20261021", "ignored.example:1")
        self.assertEqual(document["url"], "http://player.example:80/lucky/iptv/tel.epg")
        self.assertEqual([row["title"] for row in document["epg_data"]], ["早间", "补充", "跨天"])

    def test_cli_defaults_are_optional(self):
        args = bridge.parse_args(["config=tel"])
        self.assertEqual(args.config, "tel")
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 5142)
        self.assertEqual(args.public_base, "")
        self.assertEqual(args.refresh, 3600)
        with self.assertRaises(SystemExit):
            bridge.parse_args([])
        with self.assertRaises(SystemExit):
            bridge.parse_args(["--playlist", str(self.playlist)])
        with self.assertRaises(ValueError):
            bridge.config_directory("../tel")
        with self.assertRaises(ValueError):
            bridge.config_directory("/tmp/tel")

    def test_rejects_unknown_routes_and_injected_targets(self):
        for path in (
            "/c/1/%E5%A4%AE%E8%A7%86/CCTV1%E7%BB%BC%E5%90%88?url=http://evil.test/a",
            "/c/1/%E5%A4%AE%E8%A7%86/CCTV1%E7%BB%BC%E5%90%88?playseek=20261021004500-20261021013000",
            "/c/99/nope/nope",
            "/TVOD/4/%E4%B8%8A%E6%B5%B7%E5%8F%B0/%E5%8F%AA%E6%9C%89%E7%9B%B4%E6%92%AD?playseek=20261021004500-20261021013000",
            "/TVOD/1/%E5%A4%AE%E8%A7%86/CCTV1%E7%BB%BC%E5%90%88?playseek=bad",
            "/tel.epg?ch=" + ("x" * 201),
        ):
            status, _headers, _payload = self.open(path)
            self.assertIn(status, (400, 404), path)
        status, headers, _payload = self.open("/c/4/%E4%B8%8A%E6%B5%B7%E5%8F%B0/%E5%8F%AA%E6%9C%89%E7%9B%B4%E6%92%AD")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "http://127.0.0.1:5140/rtp/233.18.204.3:5140")
        status, headers, _payload = self.open("/PLTV/4/%E4%B8%8A%E6%B5%B7%E5%8F%B0/%E5%8F%AA%E6%9C%89%E7%9B%B4%E6%92%AD")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "http://127.0.0.1:5140/rtp/233.18.204.3:5140")

    def test_empty_group_uses_dash_and_index_not_the_label(self):
        empty = (
            '#EXTM3U x-tvg-url="http://127.0.0.1/first.xml"\n'
            '#EXTINF:-1 tvg-id="n1" tvg-name="无组甲",无组甲\n'
            + LIVE + "\n"
            '#EXTINF:-1 tvg-id="n2" tvg-name="无组甲" catchup="default" catchup-source="'
            + CATCHUP + '",无组甲\n'
            'http://127.0.0.1:5140/rtp/233.18.204.9:5140?fcc=9&AuthInfo=A%2Fb+C==\n'
            '#EXTINF:-1 tvg-id="slash" tvg-name="斜/杠",斜/杠\n'
            'http://127.0.0.1:5140/rtp/233.18.204.8:5140\n'
        )
        playlist = Path(self.tmp.name) / "empty.m3u"
        playlist.write_text(empty, encoding="utf-8")
        empty_bridge = bridge.Bridge(
            str(playlist), "http://127.0.0.1:9", self.cache, TZ, 3600, "tel.m3u", "tel.epg")
        empty_bridge.reload_m3u(force=True)
        text = empty_bridge.playlist_text()
        lines = text.splitlines()
        self.assertEqual(lines[0], ",#genre#")
        self.assertEqual(lines[1], "无组甲,http://127.0.0.1:9/c/1/-/%E6%97%A0%E7%BB%84%E7%94%B2")
        self.assertEqual(lines[2], "无组甲,http://127.0.0.1:9/PLTV/2/-/%E6%97%A0%E7%BB%84%E7%94%B2")
        self.assertEqual(lines[3], "斜/杠,http://127.0.0.1:9/c/3/-/%E6%96%9C%2F%E6%9D%A0")
        self.assertNotIn("//", lines[1].split(",", 1)[1].split("://", 1)[1])

        previous = self.httpd.bridge
        self.httpd.bridge = empty_bridge
        try:
            status, headers, _payload = self.open("/c/1/-/%E6%97%A0%E7%BB%84%E7%94%B2")
            self.assertEqual(status, 302)
            self.assertEqual(headers["Location"], LIVE)
            status, headers, _payload = self.open("/c/1//%E6%97%A0%E7%BB%84%E7%94%B2")
            self.assertEqual(status, 302)
            self.assertEqual(headers["Location"], LIVE)
            issued = next(line for line in lines if line.startswith("无组甲,http://127.0.0.1:9/PLTV/"))
            play = issued.split(",", 1)[1].replace("/PLTV/", "/TVOD/", 1)
            play += "?playseek=20261021004500-20261021013000"
            status, headers, _payload = self.open(play.split(":9", 1)[1])
            self.assertEqual(status, 302)
            self.assertIn("AuthInfo=A%2Fb+C==", headers["Location"])
            self.assertIn("fcc=124.75.26.151%3A15970", headers["Location"])
            self.assertIn("playseek=20261021004500-20261021013000", headers["Location"])
            self.assertNotIn("${(b)", headers["Location"])
            status, headers, _payload = self.open("/c/2/-/%E6%97%A0%E7%BB%84%E7%94%B2")
            self.assertEqual(status, 302)
            self.assertIn("fcc=9", headers["Location"])
            self.assertIn("AuthInfo=A%2Fb+C==", headers["Location"])
            status, _headers, _payload = self.open("/c/1/wrong-group/%E6%97%A0%E7%BB%84%E4%B9%99")
            self.assertEqual(status, 404)
            status, _headers, _payload = self.open("/c/9/-/%E6%97%A0%E7%BB%84%E7%94%B2")
            self.assertEqual(status, 404)
            status, _headers, _payload = self.open(
                "/c/3/-/%E6%96%9C%2F%E6%9D%A0?url=http://evil.test/a")
            self.assertEqual(status, 400)
            status, headers, _payload = self.open("/c/3/-/%E6%96%9C%2F%E6%9D%A0")
            self.assertEqual(status, 302)
            self.assertEqual(headers["Location"], "http://127.0.0.1:5140/rtp/233.18.204.8:5140")
        finally:
            self.httpd.bridge = previous


class ConfigSourceTests(unittest.TestCase):
    def test_config_uses_playlist_and_reloads_when_it_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "diyp.py"
            script.write_text("print('placeholder')\n", encoding="utf-8")
            config_dir = root / "tel"
            config_dir.mkdir()
            playlist = config_dir / "playlist.m3u"
            playlist.write_text(M3U, encoding="utf-8")
            (config_dir / "config.json").write_text("{}\n", encoding="utf-8")
            with patch.object(bridge, "__file__", str(script)):
                directory, resolved = bridge.config_directory("tel")
            self.assertEqual(directory.resolve(), config_dir.resolve())
            self.assertEqual(resolved.resolve(), playlist.resolve())
            cache = root / "cache"
            service = bridge.Bridge(str(resolved), "", cache, TZ, 300, "tel.m3u", "tel.epg")
            service.reload_m3u(force=True)
            self.assertEqual(service.playlist_text("player.example:5142").splitlines()[1],
                             "CCTV1综合,http://player.example:5142/PLTV/1/%E5%A4%AE%E8%A7%86/CCTV1%E7%BB%BC%E5%90%88")
            changed = M3U.replace("CCTV1综合", "更新频道", 1)
            playlist.write_text(changed, encoding="utf-8")
            playlist.touch()
            self.assertNotEqual(playlist.stat().st_mtime_ns, service.m3u_mtime)
            self.assertTrue(service.reload_m3u())
            self.assertIn("更新频道,", service.playlist_text("player.example:5142"))
            self.assertFalse(service.reload_m3u())

    def test_serve_uses_config_playlist_and_named_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "diyp.py"
            script.write_bytes(Path(bridge.__file__).read_bytes())
            config_dir = root / "tel"
            config_dir.mkdir()
            playlist = config_dir / "playlist.m3u"
            playlist.write_text(M3U, encoding="utf-8")
            secret = config_dir / "config.json"
            secret.write_text("{\"userid\":\"do-not-read\"}\n", encoding="utf-8")
            before = secret.read_bytes()
            with patch.object(bridge, "__file__", str(script)):
                directory, resolved = bridge.config_directory("tel")
                with patch.object(bridge, "fetch_xml", side_effect=OSError("down")), \
                        patch.object(bridge.threading, "Thread"):
                    httpd = bridge.serve(
                        str(resolved), "127.0.0.1", 0, "", str(root / "cache"),
                        "Asia/Shanghai", 300, directory.name + ".m3u", directory.name + ".epg")
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                request = urllib.request.Request(
                    "http://127.0.0.1:%d/tel.m3u" % httpd.server_address[1])
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = response.read().decode()
                self.assertTrue(body.startswith("央视,#genre#\n"))
                self.assertIn("CCTV1综合,http://127.0.0.1:", body)
                self.assertNotIn("#EXTM3U", body)
                missing = urllib.request.Request(
                    "http://127.0.0.1:%d/channels.txt" % httpd.server_address[1])
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(missing, timeout=5)
                self.assertEqual(caught.exception.code, 404)
                caught.exception.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=3)
            self.assertEqual(secret.read_bytes(), before)

    def test_epg_refresh_uses_memory_window_and_disk_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            calls = []

            def fake_fetch(url, timeout):
                calls.append(url)
                if len(calls) == 1:
                    return FIRST_XML.encode(), None
                raise OSError("down")

            service = bridge.Bridge("unused.m3u", "", cache, TZ, 3600, "tel.m3u", "tel.epg")
            service.sources = ["http://127.0.0.1/first.xml"]
            with patch.object(bridge, "fetch_xml", side_effect=fake_fetch):
                with patch.object(bridge.time, "monotonic", return_value=1000):
                    self.assertTrue(service.reload_epg(force=True))
                with patch.object(bridge.time, "monotonic", return_value=4599):
                    self.assertFalse(service.reload_epg())
                self.assertEqual(calls, ["http://127.0.0.1/first.xml"])
                with patch.object(bridge.time, "monotonic", return_value=4600):
                    self.assertTrue(service.reload_epg())
            self.assertEqual(service.epg.programme_count, 3)
            self.assertEqual(len(service.epg.errors), 1)
            cached = list(cache.glob("*.xml"))
            self.assertEqual(len(cached), 1)
            self.assertIn("早间".encode(), cached[0].read_bytes())

    def test_playlist_change_does_not_refetch_unchanged_epg_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            playlist = root / "playlist.m3u"
            playlist.write_text(M3U, encoding="utf-8")
            cache = root / "cache"
            calls = []

            def fake_fetch(url, timeout):
                calls.append(url)
                body = FIRST_XML if url.endswith("first.xml") else SECOND_XML
                return body.encode(), None

            service = bridge.Bridge(str(playlist), "", cache, TZ, 3600, "tel.m3u", "tel.epg")
            service.reload_m3u(force=True)
            with patch.object(bridge, "fetch_xml", side_effect=fake_fetch):
                service.reload_epg(force=True)
            self.assertEqual(calls, [
                "http://127.0.0.1/first.xml", "http://127.0.0.1/second.xml"])
            playlist.write_text(M3U.replace("CCTV1综合", "更新频道", 1), encoding="utf-8")
            playlist.touch()
            clock = {"now": 0.0}
            playlist_checks = {"count": 0}
            original_playlist = service.refresh_playlist

            def counting_playlist():
                original_playlist()
                playlist_checks["count"] += 1
                if playlist_checks["count"] >= 1:
                    raise RuntimeError("stop")

            def fake_monotonic():
                return clock["now"]

            def advance_sleep(seconds):
                self.assertLessEqual(seconds, 300)
                clock["now"] += seconds

            service.refresh_playlist = counting_playlist
            with patch.object(bridge.time, "monotonic", side_effect=fake_monotonic), \
                    patch.object(bridge.time, "sleep", side_effect=advance_sleep), \
                    patch.object(bridge, "fetch_xml", side_effect=fake_fetch):
                with self.assertRaises(RuntimeError):
                    service.refresh_loop()
            self.assertEqual(playlist_checks["count"], 1)
            self.assertEqual(clock["now"], 300)
            self.assertEqual(calls, [
                "http://127.0.0.1/first.xml", "http://127.0.0.1/second.xml"])
            self.assertIn("更新频道,", service.playlist_text("player.example:5142"))

            service.sources = ["http://127.0.0.1/second.xml"]
            with patch.object(bridge.time, "monotonic", return_value=clock["now"]), \
                    patch.object(bridge, "fetch_xml", side_effect=fake_fetch):
                self.assertTrue(service.reload_epg())
            self.assertEqual(calls, [
                "http://127.0.0.1/first.xml",
                "http://127.0.0.1/second.xml",
                "http://127.0.0.1/second.xml"])


if __name__ == "__main__":
    unittest.main()
