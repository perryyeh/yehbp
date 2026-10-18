"""Exercise real installer functions with local fixtures and command boundaries mocked."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'install.sh').read_text()
BASHES = list(dict.fromkeys(p for p in ['/bin/bash', shutil.which('bash'), '/opt/homebrew/bin/bash'] if p and Path(p).exists()))


def function(name):
    # Stop at the next shell function, not at a column-zero brace in a heredoc.
    start = SOURCE.index(name + '() {')
    body_start = SOURCE.index('\n', start) + 1
    following = re.search(r'^\w+\(\) \{', SOURCE[body_start:], re.M)
    end = body_start + following.start() if following else len(SOURCE)
    block = SOURCE[start:end]
    return block[:block.rfind('\n}') + 3] + '\n'


def helpers(*names):
    return ''.join(function(n) for n in names)


def run(code, data='', bash='/bin/bash', cwd=None, **env):
    return subprocess.run([bash, '-c', code], input=data, text=True,
                          capture_output=True, errors='replace', timeout=10, cwd=cwd,
                          env=dict(os.environ, **env))


class AddressTests(unittest.TestCase):
    def test_success_then_every_failure_invalidates_all_outputs(self):
        code = helpers('calculate_ip_mac', 'ip_to_mac') + '''
mode=ok
docker() { [ "$*" = 'network inspect chosen' ] || return 98; [ "$mode" != inspect ] || return 1; if [ "$mode" = no4 ]; then echo '[{"IPAM":{"Config":[]}}]'; else echo '[{"EnableIPv6":true,"IPAM":{"Config":[{"Subnet":"192.0.2.0/24","Gateway":"192.0.2.1"},{"Subnet":"2001:db8::/64","Gateway":"2001:db8::1"}]}}]'; fi; }
SELECTED_MACVLAN=chosen
network_info=sentinel
calculate_ip_mac 10 || exit 10
[ "$calculated_ip" = 192.0.2.10 ] && [ -n "$calculated_mac" ] || exit 11
mode="$MODE"
[ "$mode" != mac ] || ip_to_mac() { return 1; }
arg=12; [ "$mode" != invalid ] || arg=invalid
calculate_ip_mac "$arg" && exit 12
[ -z "$calculated_ip$calculated_ip6$calculated_mac$calculated_gateway$calculated_gateway6" ] || exit 13
[ "$network_info" = sentinel ] || exit 14
'''
        for mode in ['inspect', 'no4', 'mac', 'invalid']:
            with self.subTest(mode=mode):
                r = run(code, MODE=mode)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_required_callers_stop_before_storage_or_staging(self):
        for name in ['install_librespeed', 'install_adguardhome', 'install_mosdns',
                     'install_mihomo', 'install_ddnsgo', 'install_lucky']:
            for fail_at in ([1, 2] if name == 'install_adguardhome' else [1]):
                with self.subTest(name=name, fail_at=fail_at):
                    code = function(name) + '''
select_macvlan_or_exit() { SELECTED_MACVLAN=chosen; }
prompt_ipv4_last_octet() { echo 10; }
select_mihomo_upstream() { MIHOMO_ENDPOINT4=192.0.2.2:53; }
docker() { return 98; }
n=0
calculate_ip_mac() { n=$((n+1)); [ "$n" -ne "$FAIL_AT" ] || return 1; calculated_ip=192.0.2.10; }
select_dockerapps_dir() { echo UNEXPECTED_STORAGE; return 99; }
repo_stage_update() { echo UNEXPECTED_STAGE; return 99; }
''' + name + '\nrc=$?; [ "$rc" -ne 0 ] && [ "$n" -eq "$FAIL_AT" ]'
                    r = run(code, '2\n' if name in ['install_mihomo', 'install_ddnsgo', 'install_lucky'] else '1\n', FAIL_AT=str(fail_at))
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertNotIn('UNEXPECTED', r.stdout + r.stderr)

    def test_optional_upstream_derivation_ignores_failed_stale_outputs(self):
        code = helpers('select_mihomo_upstream') + '''
detect_mihomo_macvlan_candidates() { MIHOMO_CANDIDATE_COUNT=0; }
read_mihomo_manual_endpoint() { echo 192.0.2.2; }
normalize_mihomo_endpoint_port() { echo "$1"; }
normalize_mihomo_ipv6_endpoint() { echo "$1"; }
calculate_ip_mac() { calculated_ip=192.0.2.2; calculated_ip6=stale; return 1; }
select_mihomo_upstream || exit 1
[ "$MIHOMO_ENDPOINT6" = '2001:db8::9' ] || exit 2
'''
        r = run(code, '2001:db8::9\n')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('stale', r.stdout)


class ParentTests(unittest.TestCase):
    def test_reference_helper_has_three_distinct_results(self):
        code = function('macvlan_parent_in_use') + '''
docker() {
 case "$*" in
 'network ls --filter driver=macvlan --format {{.Name}}') [ "$MODE" != ls ] || return 1; printf 'one\\ntwo\\n';;
 'network inspect one -f {{.Options.parent}}') [ "$MODE" != inspect ] || return 1; echo eth0.8;;
 'network inspect two -f {{.Options.parent}}') echo eth1.9;;
 *) return 98;; esac
}
macvlan_parent_in_use "$PARENT"
'''
        for mode, parent, expected in [('ok', 'eth0.8', 0), ('ok', 'eth2.7', 1), ('ls', 'eth2.7', 2), ('inspect', 'eth2.7', 2)]:
            for bash in BASHES:
                r = run(code, bash=bash, MODE=mode, PARENT=parent)
                self.assertEqual(r.returncode, expected, r.stdout + r.stderr)

    def test_empty_inventory_and_unknown_parent_output(self):
        code = function('macvlan_parent_in_use') + '''
 docker() {
   case "$1 $2" in
     'network ls') [ "$MODE" = empty ] || printf '%s\\n' one;;
     'network inspect') [ "$MODE" != missing ] || printf '%s\\n' '<no value>';;
     *) return 98;;
   esac
 }
 macvlan_parent_in_use eth0.8
 '''
        for bash in BASHES:
            for mode, expected in [('empty', 1), ('blank', 2), ('missing', 2)]:
                with self.subTest(bash=bash, mode=mode):
                    r = run(code, bash=bash, MODE=mode)
                    self.assertEqual(r.returncode, expected, r.stdout + r.stderr)

    def test_deletion_uses_remaining_real_parents_and_keeps_unknown(self):
        code = helpers('clean_macvlan_network', 'macvlan_parent_in_use') + '''
list_macvlan_networks() { MACVLAN_NETWORKS=(macvlan_eth0_8 macvlan_eth1_9); }
show_macvlan_network_summary() { :; }
bash_supports_associative_arrays() { [ "$BRANCH" = 4 ]; }
docker() {
 case "$1 $2" in
 'network inspect')
   if [ "$3" = -f ]; then
     [ "$MODE" != cancel ] || [ "${@: -1}" != macvlan_eth0_8 ] || echo busy
   else
     [ "$MODE" != inspect ] || return 1
     case "$3" in macvlan_eth0_8) echo eth1.9;; peer) echo eth1.9;; *) echo eth1.9;; esac
   fi;;
 'network rm')
   [ "$MODE" != failed ] || [ "$3" != macvlan_eth0_8 ] || return 1
   printf '%s\\n' "$3" >> "$BOX/removed";;
 'network ls')
   [ "$MODE" != ls ] || return 1
   for net in macvlan_eth0_8 macvlan_eth1_9; do
     command grep -qx "$net" "$BOX/removed" || echo "$net"
   done
   case "$MODE" in peer|inspect) echo peer;; esac;;
 *) return 98;; esac
}
ip() { printf '%s\\n' "$*" >> "$BOX/ip"; }
sudo() { [ "$1" = ip ] || return 98; shift; ip "$@"; }
clean_macvlan_network
'''
        for bash in BASHES:
            for branch in ['3', '4']:
                for mode in ['ok', 'peer', 'failed', 'cancel', 'ls', 'inspect', 'decline']:
                    with self.subTest(bash=bash, branch=branch, mode=mode), tempfile.TemporaryDirectory() as box:
                        Path(box, 'removed').touch()
                        data = 'a\n' + ('n\n' if mode == 'cancel' else '') + ('n\nn\n' if mode == 'decline' else 'y\ny\n')
                        r = run(code, data, bash=bash, BOX=box, BRANCH=branch, MODE=mode)
                        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                        operations = Path(box, 'ip').read_text() if Path(box, 'ip').exists() else ''
                        self.assertIn('link delete eth0.8', operations) if mode not in ['ls', 'inspect', 'cancel', 'failed', 'decline'] else None
                        if mode in ['peer', 'failed', 'cancel', 'ls', 'inspect', 'decline']:
                            self.assertNotIn('link delete eth1.9', operations)
                        else:
                            self.assertIn('link delete eth1.9', operations)


COMPOSE = '''services:
  app:
    container_name: app
    hostname: app
    image: example/app
    environment:
      note: "container_name: app"
    networks:
      lan:
        mac_address: 02:42:c0:00:02:01
networks:
  lan:
    external: true
'''


class TextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name, 'compose.yaml')
        self.file.write_text(COMPOSE)

    def mac_code(self):
        names = ['docker_server_api_at_least', 'convert_compose_network_mac_for_legacy_docker', 'convert_compose_network_mac_for_legacy_compose']
        if 'convert_compose_single_network_mac() {' in SOURCE:
            names.append('convert_compose_single_network_mac')
        return helpers(*names) + '\ndocker() { [ "$*" = "version --format {{.Server.APIVersion}}" ] || return 98; echo "${API:-1.43}"; }\n'

    def test_mac_both_entries_count_idempotence_and_output(self):
        results = []
        for entry in ['docker', 'compose']:
            for count in [0, 1, 2]:
                with self.subTest(entry=entry, count=count):
                    text = COMPOSE.replace('        mac_address: 02:42:c0:00:02:01\n', '        mac_address: 02:42:c0:00:02:01\n' * count)
                    self.file.write_text(text)
                    call = f'convert_compose_network_mac_for_legacy_{entry} "$FILE"'
                    r = run(self.mac_code() + call, FILE=str(self.file))
                    self.assertEqual(r.returncode, int(count > 1), r.stdout + r.stderr)
                    after = self.file.read_text()
                    if count == 1:
                        self.assertIn('    mac_address: 02:42:c0:00:02:01\n    networks:', after)
                        self.assertNotIn('        mac_address:', after)
                        results.append(after)
                        r = run(self.mac_code() + call, FILE=str(self.file))
                        self.assertEqual(r.returncode, 0, r.stderr)
                        self.assertEqual(self.file.read_text(), after)
                    else:
                        self.assertEqual(after, text)
        self.assertEqual(results[0], results[1])

    def test_mac_api_trigger_and_schema_trigger(self):
        r = run(self.mac_code() + 'convert_compose_network_mac_for_legacy_docker "$FILE"', FILE=str(self.file), API='1.44')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.file.read_text(), COMPOSE)
        error = Path(self.temp.name, 'error')
        error.write_text('unrelated')
        code = self.mac_code() + helpers('apply_compose_schema_compatibility_fallback') + 'apply_compose_schema_compatibility_fallback "$ERROR" "$FILE"'
        r = run(code, FILE=str(self.file), ERROR=str(error))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.file.read_text(), COMPOSE)
        error.write_text('Additional property mac_address is not allowed')
        r = run(code, FILE=str(self.file), ERROR=str(error))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self.file.read_text(), COMPOSE)

    def test_mac_io_failures_preserve_source_and_clean_temporary_files(self):
        for entry in ['docker', 'compose']:
            for failure in ['write', 'mv']:
                with self.subTest(entry=entry, failure=failure):
                    self.file.write_text(COMPOSE)
                    mock = 'mv() { return 1; }\n' if failure == 'mv' else 'awk() { case "$*" in *"-v mac="*) return 1;; *) command awk "$@";; esac; }\n'
                    r = run(self.mac_code() + mock + f'convert_compose_network_mac_for_legacy_{entry} "$FILE"', FILE=str(self.file))
                    self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertEqual(self.file.read_text(), COMPOSE)
                    self.assertEqual(list(self.file.parent.glob('*.legacy*')), [])

    def test_names_are_scoped_and_explicit(self):
        code = helpers('rename_compose_app_fields') + 'rename_compose_app_fields "$FILE" "$SERVICE" "$CONTAINER" "$HOST" "$TARGET"'
        for service, container, host, target in [('app', 'app', 'app', 'app'), ('app', 'app', 'app', 'custom'), ('ddns-go', 'ddnsgo', 'ddnsgo', 'custom'), ('ddns-go', 'ddnsgo', 'ddnsgo', 'ddnsgo'), ('mihomo', 'mihomo', 'mihomo', 'mihomoin')]:
            with self.subTest(service=service, target=target):
                original = COMPOSE.replace('  app:', f'  {service}:').replace('    container_name: app\n', f'    container_name: {container}\n').replace('    hostname: app\n', f'    hostname: {host}\n')
                self.file.write_text(original)
                r = run(code, FILE=str(self.file), SERVICE=service, CONTAINER=container, HOST=host, TARGET=target)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                expected = original.replace(f'  {service}:\n', f'  {target}:\n').replace(f'    container_name: {container}\n', f'    container_name: {target}\n').replace(f'    hostname: {host}\n', f'    hostname: {target}\n')
                self.assertEqual(self.file.read_text(), expected)

    def test_all_installers_propagate_name_failure_with_original_template_arguments(self):
        apps = [('install_librespeed', 'librespeed'), ('install_adguardhome', 'adguardhome'),
                ('install_mosdns', 'mosdns'), ('install_mihomo', 'mihomo'),
                ('install_ddnsgo', 'ddnsgo'), ('install_lucky', 'lucky')]
        for name, original in apps:
            with self.subTest(name=name):
                self.file.write_text(COMPOSE)
                for template in ['compose.host.yaml', 'config.host.yaml']:
                    Path(self.temp.name, template).write_text(COMPOSE)
                body = function(name)
                end = body.index('\n    fi', body.index('        rename_compose_app_fields'))
                prefix = body[:end] + '\n    fi\n    echo UNEXPECTED_CONTINUE\n}\n'
                code = prefix + r'''
select_macvlan_or_exit() { SELECTED_MACVLAN=chosen; }
prompt_ipv4_last_octet() { echo 10; }
calculate_ip_mac() { calculated_ip=192.0.2.10; return 0; }
select_mihomo_upstream() { MIHOMO_ENDPOINT4=192.0.2.2:53; }
select_dockerapps_dir() { SELECTED_DOCKERAPPS_DIR="$BOX"; }
validate_compose_app_name() { return 0; }
repo_stage_update() { WORK_DIR="$BOX"; TARGET_DIR="$BOX/old"; }
install_mihomo_external_ui() { return 0; }
docker() { return 1; }
rename_compose_app_fields() { printf 'ARGS=%s|%s|%s|%s|%s\n' "$@"; return 1; }
''' + name
                # Host choice is 1; AdGuard chooses public upstreams (2).
                data = ('2\ncustom\n' if name == 'install_adguardhome' else
                        '1\ncustom\n' if name in ['install_mihomo', 'install_ddnsgo', 'install_lucky'] else 'custom\n')
                r = run(code, data, BOX=self.temp.name, cwd=self.temp.name)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                service = 'ddns-go' if original == 'ddnsgo' else original
                self.assertIn(f'ARGS=compose.yaml|{service}|{original}|{original}|custom', r.stdout)
                self.assertNotIn('UNEXPECTED_CONTINUE', r.stdout + r.stderr)

    def test_name_does_not_change_other_services_or_top_level_sections(self):
        extra = '  other:\n    container_name: app\n    hostname: app\n'
        original = COMPOSE.replace('networks:\n  lan:', extra + 'networks:\n  app:\n    hostname: app\n  lan:')
        self.file.write_text(original)
        r = run(helpers('rename_compose_app_fields') + 'rename_compose_app_fields "$FILE" app app app custom', FILE=str(self.file))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = self.file.read_text()
        self.assertIn(extra, after)
        self.assertIn('networks:\n  app:\n    hostname: app\n', after)
        self.assertIn('  custom:\n    container_name: custom\n    hostname: custom\n', after)

    def test_quoted_names_spacing_and_comments_remain_scoped(self):
        for quote in ['', "'", '"']:
            with self.subTest(quote=quote):
                original = (f'services:  # apps\n  {quote}app{quote}:   # primary\n'
                            f'    container_name:   {quote}app{quote}  # container\n'
                            f'    hostname: {quote}app{quote}  \n'
                            '    image: example/app\n    volumes:\n      - app:/app\n'
                            '  other:\n    container_name: app\n')
                self.file.write_text(original)
                r = run(helpers('rename_compose_app_fields') +
                        'rename_compose_app_fields "$FILE" app app app custom', FILE=str(self.file))
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                expected = original.replace(f'{quote}app{quote}:   # primary', f'{quote}custom{quote}:   # primary').replace(
                    f'   {quote}app{quote}  # container', f'   {quote}custom{quote}  # container').replace(
                    f'hostname: {quote}app{quote}  ', f'hostname: {quote}custom{quote}  ')
                self.assertEqual(self.file.read_text(), expected)

    def test_name_write_or_move_failure_preserves_source(self):
        for mock in ['awk() { return 1; }', 'mv() { return 1; }']:
            r = run(helpers('rename_compose_app_fields') + mock + '\nrename_compose_app_fields "$FILE" app app app custom', FILE=str(self.file))
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(self.file.read_text(), COMPOSE)
            self.assertEqual(list(self.file.parent.glob('*.names.*')), [])


if __name__ == '__main__':
    unittest.main()
