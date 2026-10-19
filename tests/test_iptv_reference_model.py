"""Directory model acceptance tests; synthetic data only."""
import contextlib
import io
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from iptv_reference_loader import iptv as c
from test_iptv_reference_cli import config, row
from test_iptv_reference_directory import directory


class DirectoryModelTests(unittest.TestCase):
    def prepare(self, root):
        paths = c.config_paths(root/'instance')
        c.private_dir(paths[0])
        c.atomic_json(paths[1], config())
        c.atomic_json(paths[2], [row(UserChannelID='1'), row(UserChannelID='1')])
        c.atomic_json(c.metadata_path(paths), directory([dict(mixNo='1', name=' 高清 HD 4K ')]))
        return paths

    def test_two_arguments_offline_export_bytes_and_original_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); paths=self.prepare(root)
            before={p:p.read_bytes() for p in (paths[1],paths[2],c.metadata_path(paths))}
            with patch.object(c.socket,'socket',side_effect=AssertionError('network forbidden')), patch.object(c,'login',side_effect=AssertionError('login forbidden')):
                self.assertEqual(c.main(['config='+str(paths[0]),'output='+str(root/'export')]),0)
            self.assertEqual(paths[3].read_bytes(),(root/'export/instance.m3u').read_bytes())
            self.assertEqual(paths[3].read_text().count('#EXTINF:'),2)
            self.assertIn(' 高清 HD 4K ',paths[3].read_text())
            self.assertEqual(before,{p:p.read_bytes() for p in before})
            self.assertEqual((root/'export').stat().st_mode&0o777,0o700)
            self.assertEqual({p.name for p in paths[0].iterdir()},{'config.json','catchup.json','channels.json','playlist.m3u','.iptv.lock'})

    def test_key_value_first_fetch_and_script_relative_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); script=root/'iptv.py';script.write_bytes(Path(c.__file__).read_bytes())
            args=['userinfo=fixture','SN=box','MAC=00:11:22:33:44:55','interface=eth0.85','rtp2http=http://127.0.0.1:5140']
            with patch.object(c,'__file__',str(script)), patch.object(c,'local_ipv4',return_value='192.0.2.1'), patch.object(c,'login',return_value=[row()]) as login, patch.object(c,'portal_directory',return_value=directory([dict(mixNo='1',name='fixture')])):
                self.assertEqual(c.main(args),0)
                self.assertEqual(login.call_count,1)
                paths=c.config_paths(root/'eth0.85');self.assertTrue(paths[3].exists())
                with patch.object(c.socket,'socket',side_effect=AssertionError('offline')):
                    self.assertEqual(c.main(['config=eth0.85']),0)
                self.assertEqual(login.call_count,1)
            elsewhere=root/'cwd';elsewhere.mkdir()
            result=subprocess.run([sys.executable,str(script),'config=eth0.85'],cwd=elsewhere,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(list(elsewhere.iterdir()),[])
            with patch.object(c,'__file__',str(script)), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(c.main(['userinfo=fixture']),1)
                self.assertEqual(c.main(['config=incomplete','interface=eth0.85']),1)
                self.assertFalse((root/'incomplete/config.json').exists())

    def test_lock_and_script_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); paths=self.prepare(root)
            before={p:p.read_bytes() for p in paths[0].iterdir()}
            with c.lock(paths[0]), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(c.main(['config='+str(paths[0])]),1)
            self.assertEqual(before,{p:p.read_bytes() for p in before})
            for name in ('config.json','catchup.json','channels.json','playlist.m3u','.iptv.lock'):
                with patch.object(c,'__file__',str(paths[0]/name)), self.assertRaises(c.SafeError):
                    c.config_paths(paths[0])

    def test_output_path_boundaries_and_other_instance_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();paths=self.prepare(root);export=root/'export';export.mkdir()
            self.assertEqual(c.export_path('file.m3u',paths[0]),Path(os.path.abspath('file.m3u')))
            self.assertEqual(c.export_path('FILE.M3U',paths[0]),Path(os.path.abspath('FILE.M3U')))
            self.assertEqual(c.export_path('exports',paths[0]),Path(os.path.abspath('exports'))/'instance.m3u')
            self.assertEqual(c.export_path(str(export),paths[0]),export/'instance.m3u')
            self.assertEqual(c.export_path(str(root/'new')+'/',paths[0]),root/'new/instance.m3u')
            self.assertEqual(c.export_path(str(root/'new/file.m3u'),paths[0]),root/'new/file.m3u')
            self.assertEqual(c.export_path(str(root/'lists.backup'),paths[0]),root/'lists.backup/instance.m3u')
            for output in (str(export),str(root/'new')+'/',str(root/'chosen.m3u')):
                self.assertEqual(c.main(['config='+str(paths[0]),'output='+output]),0)
                self.assertEqual(paths[3].read_bytes(),c.export_path(output,paths[0]).read_bytes())
            primary=paths[3].read_bytes()
            beta=root/'beta';beta.mkdir();c.atomic_json(beta/'config.json',config());c.atomic_json(beta/'catchup.json',[row()])
            before={p:p.read_bytes() for p in beta.iterdir()}
            err=io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(c.main(['config='+str(paths[0]),'output='+str(beta/'catchup.json')]),1)
            self.assertEqual(before,{p:p.read_bytes() for p in beta.iterdir()});self.assertIn('不能覆盖已有文件',err.getvalue());self.assertEqual(paths[3].read_bytes(),primary)

    def test_same_directory_once_custom_name_and_basename_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths=self.prepare(Path(tmp).resolve())
            with patch.object(c,'atomic_text',wraps=c.atomic_text) as write:
                c.convert(paths, destination=paths[0]/'playlist.m3u'); self.assertEqual(write.call_count,1)
            c.convert(paths, destination=paths[0]/'custom.m3u')
            self.assertEqual(paths[3].read_bytes(),(paths[0]/'custom.m3u').read_bytes())
            old=paths[1].read_bytes()
            with self.assertRaises(c.SafeError): c.convert(paths, destination=paths[0]/'config.json')
            self.assertEqual(old,paths[1].read_bytes())
            for name in ('../bad','a/b','a\\b','.iptv.lock','..','bad\nname'):
                with self.assertRaises(c.SafeError): c.output_basename(name)

    def test_existing_parent_and_service_directory_modes_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); root.chmod(0o755)
            service=root/'service'; service.mkdir(mode=0o755)
            export=root/'export'; export.mkdir(mode=0o751)
            modes={p:p.stat().st_mode for p in (root,service,export,Path(c.__file__).parent)}
            paths=c.config_paths(service); c.atomic_json(paths[1],config());c.atomic_json(paths[2],[row()])
            c.convert(paths, destination=export/'playlist.m3u')
            self.assertEqual(modes,{p:p.stat().st_mode for p in modes})
            for p in service.iterdir(): self.assertEqual(p.stat().st_mode&0o777,0o600)
            self.assertEqual((export/'playlist.m3u').stat().st_mode&0o777,0o600)

    def test_missing_input_no_network_and_legacy_files_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); paths=self.prepare(root); paths[2].unlink()
            legacy=paths[0]/'catchup_tel.json';legacy.write_text('old')
            out=io.StringIO()
            with contextlib.redirect_stderr(out), patch.object(c.socket,'socket',side_effect=AssertionError('network')), patch.object(c,'login',side_effect=AssertionError('login')):
                self.assertEqual(c.main(['config='+str(paths[0])]),1)
            self.assertIn('先执行fetch',out.getvalue());self.assertFalse(paths[3].exists());self.assertEqual(legacy.read_text(),'old')
            err=io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(c.main(['config='+str(paths[0]),'--instance','tel']),1)
            self.assertIn('config=',err.getvalue())

    def test_export_failure_reports_primary_and_preserves_old_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();paths=self.prepare(root); export=root/'export';export.mkdir()
            old=export/'playlist.m3u';old.write_text('old')
            replace=c.os.replace
            def fail_export(source,target):
                if Path(target)==old: raise OSError('secret details')
                return replace(source,target)
            err=io.StringIO()
            with patch.object(c.os,'replace',side_effect=fail_export), contextlib.redirect_stderr(err):
                self.assertEqual(c.main(['config='+str(paths[0]),'output='+str(export/'playlist.m3u')]),1)
            self.assertIn(str(paths[3]),err.getvalue())
            self.assertNotIn('secret',err.getvalue());self.assertEqual(old.read_text(),'old');self.assertTrue(paths[3].exists())
            self.assertFalse(list(export.glob('.iptv-*')))

    def test_interactive_first_and_reentry_hide_identity_keep_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp).resolve()/'instance'; args=['configure','config='+str(d)]
            with patch.object(c.getpass,'getpass',side_effect=['fixture','box','00:11:22:33:44:55','eth0.85']+['']*5) as hidden, patch.object(c.socket,'socket',side_effect=AssertionError('network')):
                self.assertEqual(c.main(args),0)
            self.assertEqual(hidden.call_count,9)
            paths=c.config_paths(d); cfg=c.load(paths[1]);cfg['extra']={'null':None,'duplicates':[1,1]};c.atomic_json(paths[1],cfg)
            with patch.object(c.getpass,'getpass',return_value=''):
                self.assertEqual(c.main(args),0)
            self.assertEqual(c.load(paths[1]),cfg)
            err=io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(c.main(args+['--stdin']),1)
            self.assertIn('config=',err.getvalue());self.assertNotIn('fixture',err.getvalue())

    def test_single_runtime_file_real_cli_independent_of_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();paths=self.prepare(root);script=root/'iptv.py';script.write_bytes(Path(c.__file__).read_bytes())
            elsewhere=root/'elsewhere';elsewhere.mkdir()
            result=subprocess.run([sys.executable,str(script),'config='+str(paths[0]),'output='+str(root/'export/chosen.m3u')],cwd=elsewhere,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(paths[3].read_bytes(),(root/'export/chosen.m3u').read_bytes());self.assertEqual(list(elsewhere.iterdir()),[])
            verifier=Path(__file__).with_name('verify_iptv_artifacts.py')
            verified=subprocess.run([sys.executable,str(verifier),str(paths[0])],capture_output=True,text=True)
            self.assertEqual(verified.returncode,0,verified.stderr)
            self.assertIn('"records": 2',verified.stdout)
