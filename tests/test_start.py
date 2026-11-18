"""Setup retry and launch isolation using disposable files; no installs or sign-in."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('start', Path(__file__).resolve().parents[1] / 'tools/start.py')
start = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start)


class StartTests(unittest.TestCase):
    def setUp(self):
        previous_reporter = start.REPORTER
        start.REPORTER = None
        self.addCleanup(setattr, start, 'REPORTER', previous_reporter)
        temporary = tempfile.TemporaryDirectory(prefix='bedrock start ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for directory in ('client', 'tools', 'config'):
            (self.root / directory).mkdir()
        source = Path(__file__).resolve().parents[1]
        (self.root / 'client/proprietary_consent.py').write_bytes(
            (source / 'client/proprietary_consent.py').read_bytes())
        (self.root / 'EULA.md').write_text((source / 'EULA.md').read_text().replace(
            'PROPRIETARY_COMPONENTS_ACCEPTED = false',
            'PROPRIETARY_COMPONENTS_ACCEPTED = true'))
        self.root_patch = patch.object(start, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        for name in ('wine', 'graphics', 'helper'):
            (self.root / (name + '.src')).write_text(name)
        self.calls = []

    def build(self, command, label, log):
        name = command[0]
        self.calls.append(name)
        (self.root / (name + '.out')).write_text(name + '-compiled')

    def step(self, steps, name):
        steps.step(name, name, [[name]], [name + '.src'], [name + '.out'])

    def test_cache_save_preserves_stale_temp_links_and_their_targets(self):
        steps = start.Steps()
        steps.path.parent.mkdir(parents=True)
        recovery = self.root / 'recovery.json'
        recovery.write_bytes(b'preserve recovery data')
        stale = steps.path.with_suffix('.tmp')
        for link_type in ('symlink', 'hardlink'):
            with self.subTest(link_type=link_type):
                if link_type == 'symlink':
                    stale.symlink_to(recovery)
                else:
                    os.link(recovery, stale)
                steps.state = {'wine': {'signature': link_type}}
                steps.save()
                self.assertEqual(recovery.read_bytes(), b'preserve recovery data')
                self.assertEqual(stale.read_bytes(), b'preserve recovery data')
                self.assertFalse(steps.path.is_symlink())
                self.assertEqual(json.loads(steps.path.read_text()), steps.state)
                self.assertEqual(steps.path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(list(steps.path.parent.glob('.start-state-*.tmp')), [])
                stale.unlink()

    def test_cache_save_replaces_destination_link_without_modifying_target(self):
        steps = start.Steps()
        steps.path.parent.mkdir(parents=True)
        recovery = self.root / 'recovery.json'
        recovery.write_text('{"previous": true}')
        steps.path.symlink_to(recovery)
        steps.state = {'wine': {'signature': 'new'}}
        steps.save()
        self.assertEqual(recovery.read_text(), '{"previous": true}')
        self.assertFalse(steps.path.is_symlink())
        self.assertEqual(json.loads(steps.path.read_text()), steps.state)

    def test_failed_cache_save_preserves_previous_state_and_cleans_owned_temp(self):
        steps = start.Steps()
        steps.state = {'wine': {'signature': 'previous'}}
        steps.save()
        previous = steps.path.read_bytes()
        unrelated = steps.path.with_suffix('.tmp')
        unrelated.write_bytes(b'preserve unrelated temporary file')
        for operation in ('fsync', 'replace'):
            with self.subTest(operation=operation):
                steps.state = {'wine': {'signature': 'new'}}
                with patch.object(start.os, operation, side_effect=OSError('fixture failure')):
                    with self.assertRaisesRegex(OSError, 'fixture failure'):
                        steps.save()
                self.assertEqual(steps.path.read_bytes(), previous)
                self.assertEqual(unrelated.read_bytes(), b'preserve unrelated temporary file')
                self.assertEqual(list(steps.path.parent.glob('.start-state-*.tmp')), [])
        steps.state = {'invalid': object()}
        with self.assertRaises(TypeError):
            steps.save()
        self.assertEqual(steps.path.read_bytes(), previous)
        self.assertEqual(list(steps.path.parent.glob('.start-state-*.tmp')), [])
        steps.state = {'wine': {'signature': 'retry'}}
        steps.save()
        self.assertEqual(json.loads(steps.path.read_text()), steps.state)

    def test_setup_rejects_unsupported_paths_before_installing_tools(self):
        source = Path(__file__).resolve().parents[1]
        with patch.object(start, 'ROOT', source):
            builder = start.module('build-native-dependencies')
        for root, prefix in ((self.root, ''), (source, str(self.root / 'prefix'))):
            with self.subTest(root=root, prefix=prefix), \
                    patch.object(start, 'ROOT', root), \
                    patch.object(start, 'module', return_value=builder), \
                    patch.object(start, 'run') as run, \
                    patch.dict(os.environ, {'STANDALONE_DEPENDENCY_PREFIX': prefix}):
                with self.assertRaisesRegex(ValueError, 'Unsupported'):
                    start.setup(2, 'CA')
                run.assert_not_called()
        self.assertFalse((self.root / 'runtime').exists())
        self.assertFalse((self.root / 'build').exists())

    def test_retry_skips_completed_steps_and_runs_failed_step_again(self):
        def fail_helper(command, label, log):
            if command[0] == 'helper':
                raise RuntimeError('interrupted')
            self.build(command, label, log)

        with patch.object(start, 'run', side_effect=fail_helper):
            steps = start.Steps()
            self.step(steps, 'wine')
            self.step(steps, 'graphics')
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.step(steps, 'helper')
        state = json.loads(steps.path.read_text())
        self.assertEqual(set(state), {'wine', 'graphics'})
        self.calls.clear()
        with patch.object(start, 'run', side_effect=self.build):
            retry = start.Steps()
            for name in ('wine', 'graphics', 'helper'):
                self.step(retry, name)
        self.assertEqual(self.calls, ['helper'])

    def test_changed_input_rebuilds_it_and_downstream_only(self):
        with patch.object(start, 'run', side_effect=self.build):
            steps = start.Steps()
            for name in ('wine', 'graphics', 'helper'):
                self.step(steps, name)
            (self.root / 'graphics.src').write_text('new input')
            self.calls.clear()
            retry = start.Steps()
            for name in ('wine', 'graphics', 'helper'):
                self.step(retry, name)
        self.assertEqual(self.calls, ['graphics', 'helper'])

    def test_damaged_output_invalidates_downstream_even_if_inputs_unchanged(self):
        with patch.object(start, 'run', side_effect=self.build):
            steps = start.Steps()
            for name in ('wine', 'graphics', 'helper'):
                self.step(steps, name)
            (self.root / 'wine.out').write_text('partial replacement')
            self.calls.clear()
            retry = start.Steps()
            for name in ('wine', 'graphics', 'helper'):
                self.step(retry, name)
        self.assertEqual(self.calls, ['wine', 'graphics', 'helper'])

    def test_missing_output_cannot_be_marked_complete(self):
        with patch.object(start, 'run'):
            steps = start.Steps()
            with self.assertRaises(FileNotFoundError):
                self.step(steps, 'wine')
        self.assertNotIn('wine', json.loads(steps.path.read_text()))

    def test_check_never_creates_files_or_runs_setup(self):
        for ready in (False, True):
            with patch.object(start, 'installed', return_value=ready), \
                 patch.object(start, 'run', side_effect=AssertionError('process started')), \
                 patch.object(start, 'setup', side_effect=AssertionError('setup started')):
                self.assertEqual(start.main(['--market', 'US', '--check']), 0 if ready else 1)
        self.assertFalse((self.root / 'runtime').exists())

    def test_second_start_cannot_enter_while_first_is_active(self):
        with start.lock('start.lock'):
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                with start.lock('start.lock'):
                    self.fail('second caller acquired lock')
        with start.lock('start.lock'):
            pass

    def test_fresh_checkout_check_does_not_even_write_python_cache(self):
        source = Path(__file__).resolve().parents[1]
        (self.root / 'client/standalone.py').write_bytes((source / 'client/standalone.py').read_bytes())
        before = set(self.root.rglob('*'))
        self.assertEqual(start.main(['--check']), 1)
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_source_check_uses_checkout_even_with_inherited_app_environment(self):
        source = Path(__file__).resolve().parents[1]
        (self.root / 'client/standalone.py').write_bytes((source / 'client/standalone.py').read_bytes())
        app_home = self.root / 'app-data'
        app_bundle = self.root / 'app-resources'
        with patch.dict(os.environ, {'BEDROCK_HOME': str(app_home),
                                     'BEDROCK_BUNDLE': str(app_bundle)}):
            self.assertEqual(start.main(['--check']), 1)
            launcher = start.module('standalone')
            self.assertEqual(launcher.ROOT, self.root)
            self.assertEqual(launcher.PREFIX, self.root / 'bottles/Bedrock-Standalone')
            self.assertNotIn('BEDROCK_BUNDLE', os.environ)
        self.assertFalse(app_home.exists())
        self.assertFalse(app_bundle.exists())
        self.assertFalse((self.root / 'runtime').exists())

    def test_termination_and_terminal_close_stop_owned_logged_job(self):
        source = Path(__file__).resolve().parents[1] / 'tools/start.py'
        worker = self.root / 'worker.py'
        worker.write_text('''import importlib.util
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location('start', sys.argv[1])
start = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start)
start.ROOT = Path(__file__).parent
def fixture():
    start.run([sys.executable, '-c',
               'import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)',
               sys.argv[2]], 'Owned fixture job', start.ROOT / 'job.log')
    return 0
start.main = fixture
sys.exit(start.cli())
''')
        for requested in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=requested):
                receipt = self.root / f'child-{requested}.pid'
                process = subprocess.Popen([sys.executable, str(worker), str(source), str(receipt)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, start_new_session=True)
                child_pid = None
                try:
                    deadline = time.monotonic() + 5
                    while not receipt.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(receipt.exists(), 'fixture child did not become ready')
                    child_pid = int(receipt.read_text())
                    process.send_signal(requested)
                    _output, error = process.communicate(timeout=8)
                    self.assertEqual(process.returncode, 130, error)
                    self.assertIn('Stopped. Open Start.command again to retry.', error)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)
                    # If an assertion fails, still remove only this fixture's
                    # known child group; never leave a sleeping QA job behind.
                    if child_pid is None and receipt.exists():
                        child_pid = int(receipt.read_text())
                    if child_pid is not None:
                        try:
                            os.killpg(child_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_complete_installation_launches_without_installing_tools(self):
        class Launcher:
            @staticmethod
            def game_running():
                return False

            @staticmethod
            def wait_for_idle_prefix():
                return None

        with patch.object(start, 'installed', return_value=True), \
             patch.object(start, 'module', return_value=Launcher), \
             patch.object(start.sys, 'platform', 'darwin'), \
             patch.object(start.platform, 'machine', return_value='arm64'), \
             patch.object(start.platform, 'mac_ver', return_value=('15.3', '', '')), \
             patch.object(start, 'setup', side_effect=AssertionError('unexpected setup')), \
             patch.object(start, 'run') as run, patch.dict(start.os.environ), \
             patch.object(start.os, 'umask'):
            self.assertEqual(start.main(['--market', 'US']), 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-4:], ['play', '--', '--market', 'US'])

    def test_main_initializes_setup_reporter_and_prints_log_location_once(self):
        class Launcher:
            @staticmethod
            def game_running():
                return False

            @staticmethod
            def wait_for_idle_prefix():
                return None

        with patch.object(start, 'installed', side_effect=[False, True]), \
                patch.object(start, 'module', return_value=Launcher), \
                patch.object(start, 'setup') as setup, patch.object(start, 'run'), \
                patch.object(start, 'lock', return_value=contextlib.nullcontext()), \
                patch.object(start.sys, 'platform', 'darwin'), \
                patch.object(start.platform, 'machine', return_value='arm64'), \
                patch.object(start.platform, 'mac_ver', return_value=('15.3', '', '')), \
                patch.object(start.os, 'umask'), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(start.main(['--setup-only']), 0)
        setup.assert_called_once()
        text = output.getvalue()
        self.assertIn('First setup builds Wine and downloads required components and your game. '
                      'You can resume if interrupted.', text)
        self.assertIn('Logs: ' + str(self.root / 'logs/standalone/setup'), text)
        self.assertEqual(text.count('Logs:'), 1)
        self.assertNotIn('Traceback', text)

    def test_cli_reports_handled_setup_failure_without_traceback(self):
        class Launcher:
            @staticmethod
            def game_running():
                return False

            @staticmethod
            def wait_for_idle_prefix():
                return None

        with patch.object(start, 'installed', return_value=False), \
                patch.object(start, 'module', return_value=Launcher), \
                patch.object(start, 'setup', side_effect=RuntimeError('fixture setup failure')), \
                patch.object(start, 'lock', return_value=contextlib.nullcontext()), \
                patch.object(start.sys, 'platform', 'darwin'), \
                patch.object(start.platform, 'machine', return_value='arm64'), \
                patch.object(start.platform, 'mac_ver', return_value=('15.3', '', '')), \
                patch.object(start.os, 'umask'), \
                patch.object(start.sys, 'argv', ['Start.command']), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(start.cli(), 1)
        self.assertIn('Start: fixture setup failure', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())

    def test_cli_keeps_handled_tty_failure_inside_eleven_line_panel(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        reporter = start.SetupReporter(output, environment={'TERM': 'xterm-256color'})
        reporter.begin('Building the Windows compatibility runtime')
        with patch.object(start, 'REPORTER', reporter), \
                patch.object(start, 'main', side_effect=RuntimeError('fixture setup failure')), \
                contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(start.cli(), 1)

        frame = start.ANSI_ESCAPE.sub('', output.getvalue().rsplit('\033[11A', 1)[-1])
        rows = [line for line in frame.splitlines() if line.strip()]
        self.assertEqual(len(rows), 11)
        self.assertIn('Start: fixture setup failure', output.getvalue())
        self.assertEqual(error.getvalue(), '')

    def test_cli_does_not_repeat_a_child_failure_already_reported(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        reporter = start.SetupReporter(output, environment={'TERM': 'xterm-256color'})
        reporter.begin('Building Microsoft sign-in')
        reporter.event('Building Microsoft sign-in failed; see the setup log.',
                       failed=True, cause='Setup: close game/helper normally.', log=self.root / 'helper.log')
        with patch.object(start, 'REPORTER', reporter), \
                patch.object(start, 'main', side_effect=RuntimeError('already reported')), \
                contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(start.cli(), 1)

        frame = start.ANSI_ESCAPE.sub('', output.getvalue().rsplit('\033[11A', 1)[-1])
        rows = [line for line in frame.splitlines() if line.strip()]
        self.assertEqual(len(rows), 11)
        self.assertEqual(frame.count('FAILED'), 1)
        self.assertNotIn('Start: already reported', output.getvalue())
        self.assertEqual(error.getvalue(), '')

    def test_failure_summary_redacts_secret_bearing_log_lines(self):
        log = self.root / 'logs/failure.log'
        log.parent.mkdir(parents=True)
        log.write_text('Setup: Could not continue; access_token=private-value\n')

        self.assertIsNone(start.safe_failure_summary(log))

    def dependency_inputs(self):
        for name in ('tools/build-native-dependencies.py', 'config/native-dependencies.lock.json'):
            (self.root / name).write_text(name)

    def test_source_dependencies_build_once_then_verify_outputs_before_reusing(self):
        self.dependency_inputs()
        verified = []

        def verify(prefix):
            verified.append(prefix)
            self.assertEqual((prefix / 'dependency-manifest.json').read_text(), 'verified fixture')

        def build(command, label, log):
            self.calls.append(command)
            destination = Path(command[command.index('--destination') + 1])
            self.assertFalse((self.root / 'runtime').exists())
            (destination / 'prefix').mkdir()
            (destination / 'prefix/dependency-manifest.json').write_text('verified fixture')

        with patch.object(start, 'module', return_value=SimpleNamespace(verify=verify)), \
                patch.object(start, 'run', side_effect=build), \
                patch.dict(os.environ, {'STANDALONE_DEPENDENCY_PREFIX': ''}):
            prefix = start.native_dependencies('/source/python', 12)
            self.assertEqual(start.native_dependencies('/source/python', 12), prefix)
        self.assertEqual(len(self.calls), 1)
        cache_option = self.calls[0].index('--cache-dir')
        self.assertEqual(Path(self.calls[0][cache_option + 1]).name, 'download-cache')
        self.assertEqual(self.calls[0][self.calls[0].index('--jobs') + 1], '8')
        self.assertIn('--live-output', self.calls[0])
        self.assertEqual(verified, [prefix, prefix])

    def test_interrupted_dependency_build_is_preserved_and_retry_uses_fresh_attempt(self):
        self.dependency_inputs()
        attempts = []

        def build(command, label, log):
            attempt = Path(command[command.index('--destination') + 1])
            attempts.append(attempt)
            (attempt / 'prefix').mkdir()
            if len(attempts) == 1:
                (attempt / 'prefix/partial-library').write_bytes(b'keep failed output')
                raise RuntimeError('interrupted build')
            (attempt / 'prefix/dependency-manifest.json').write_text('{}')

        verifier = unittest.mock.Mock()
        with patch.object(start, 'module', return_value=SimpleNamespace(verify=verifier)), \
                patch.object(start, 'run', side_effect=build), \
                patch.dict(os.environ, {'STANDALONE_DEPENDENCY_PREFIX': ''}):
            with self.assertRaisesRegex(RuntimeError, 'interrupted build'):
                start.native_dependencies('/source/python', 2)
            verifier.assert_not_called()
            prefix = start.native_dependencies('/source/python', 2)
        self.assertNotEqual(attempts[0], attempts[1])
        self.assertEqual((attempts[0] / 'prefix/partial-library').read_bytes(), b'keep failed output')
        self.assertEqual(prefix, attempts[1] / 'prefix')
        verifier.assert_called_once_with(prefix)

    def test_explicit_dependency_prefix_is_verified_without_build_or_runtime_changes(self):
        prefix = self.root / 'selected dependency prefix'
        verifier = unittest.mock.Mock()
        with patch.object(start, 'module', return_value=SimpleNamespace(verify=verifier)), \
                patch.object(start, 'run') as run, \
                patch.dict(os.environ, {'STANDALONE_DEPENDENCY_PREFIX': str(prefix)}):
            self.assertEqual(start.native_dependencies('/source/python', 2), prefix)
            verifier.assert_called_once_with(prefix)
            run.assert_not_called()
            verifier.side_effect = ValueError('tampered dependency')
            with self.assertRaisesRegex(ValueError, 'tampered dependency'):
                start.native_dependencies('/source/python', 2)
            run.assert_not_called()
        self.assertFalse((self.root / 'runtime').exists())
        self.assertFalse((self.root / 'build').exists())

    def test_invalid_completed_dependency_prefix_is_preserved_without_rebuilding(self):
        self.dependency_inputs()

        def build(command, label, log):
            destination = Path(command[command.index('--destination') + 1])
            (destination / 'prefix').mkdir()
            (destination / 'prefix/dependency-manifest.json').write_text('{}')

        verifier = unittest.mock.Mock()
        with patch.object(start, 'module', return_value=SimpleNamespace(verify=verifier)), \
                patch.object(start, 'run', side_effect=build) as run, \
                patch.dict(os.environ, {'STANDALONE_DEPENDENCY_PREFIX': ''}):
            prefix = start.native_dependencies('/source/python', 2)
            verifier.side_effect = ValueError('changed library')
            with self.assertRaisesRegex(ValueError, 'changed library'):
                start.native_dependencies('/source/python', 2)
            self.assertEqual(run.call_count, 1)
            self.assertTrue((prefix / 'dependency-manifest.json').exists())

    def test_setup_passes_verified_source_prefix_to_wine_and_tracks_receipt(self):
        prefix = self.root / 'verified dependencies/prefix'
        recorded = []

        class Steps:
            def step(self, *args):
                recorded.append(args)

        bootstrap = SimpleNamespace(PROJECTS={'xodus': {'patches': ('account.patch',)}},
                                    require_build_path=unittest.mock.Mock(),
                                    HELPER_INPUTS={
                                        'client/standalone-helper.rs': 'helper',
                                        'client/standalone-consent.rs': 'consent',
                                        'client/standalone-xvd.rs': 'xvd',
                                    },
                                    SOURCE_INPUTS=('native/file-pickers/pickers.c', 'native/file-pickers/build.sh'),
                                    bootstrap=unittest.mock.Mock())
        with patch.object(start, 'run') as run, patch.object(start, 'Steps', Steps), \
                patch.object(start, 'module', return_value=bootstrap), \
                patch.object(start, 'native_dependencies', return_value=prefix) as dependencies, \
                patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}):
            start.setup(2, 'US')
            self.assertTrue(os.environ['PATH'].startswith('/opt/homebrew/opt/python@3.13/libexec/bin:'))
        dependencies.assert_called_once_with('/opt/homebrew/opt/python@3.13/bin/python3.13', 2)
        self.assertEqual(recorded[0][0], 'wine')
        wine_command = recorded[0][2][0]
        self.assertEqual(wine_command[:2], ['/usr/bin/env', 'STANDALONE_DEPENDENCY_PREFIX=' + str(prefix)])
        self.assertIn(str(prefix / 'dependency-manifest.json'), recorded[0][3])
        self.assertIn('tools/verify-wine-source.py', recorded[0][3])
        self.assertIn('native/file-pickers/pickers.c', recorded[0][3])
        self.assertIn('runtime/standalone/wine/lib/wine/x86_64-windows/bedrock-pickers.dll', recorded[0][4])
        self.assertIn('runtime/standalone/wine/file-pickers-build.json', recorded[0][4])
        helper_inputs = next(step[3] for step in recorded if step[0] == 'helper')
        self.assertIn('client/standalone-xvd.rs', helper_inputs)
        self.assertIn('client/standalone-consent.rs', helper_inputs)
        self.assertIn('patches/account.patch', helper_inputs)
        self.assertEqual(run.call_args_list[0].args[0][-1], str(self.root / 'tools/install-build-tools.sh'))

    def test_logged_stage_streams_useful_output_and_keeps_complete_log(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, (
            b'[ 42%] Building fixture target\n'
            b'Compiling fixture crate\n'
            b'Authorization: Bearer private-token\n'
            b'Downloading https://example.invalid/signed?token=private\n'
            b'ordinary noisy output\n'))
        os.close(write_fd)

        class Process:
            pid = os.getpid()
            stdout = os.fdopen(read_fd, 'rb', buffering=0)

            @staticmethod
            def poll():
                return 0

            @staticmethod
            def wait(timeout=None):
                return 0

        def spawn(_command, *, cwd, stdout, stderr, start_new_session, bufsize):
            return Process()

        log = self.root / 'logs/build.log'
        with patch.object(start.subprocess, 'Popen', side_effect=spawn), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            start.run(['build'], 'Building the Windows compatibility runtime', log)
        text = output.getvalue()
        self.assertIn('Applies WineGDK patches and compiles the x86-64 Wine runtime.', text)
        self.assertIn('Wine runtime: 42% — Building fixture target', text)
        self.assertNotIn('private-token', text)
        self.assertNotIn('example.invalid', text)
        self.assertNotIn('ordinary noisy output', text)
        self.assertNotIn('tail -f', text)
        self.assertEqual(log.read_bytes(), (
            b'[ 42%] Building fixture target\n'
            b'Compiling fixture crate\n'
            b'Authorization: Bearer private-token\n'
            b'Downloading https://example.invalid/signed?token=private\n'
            b'ordinary noisy output\n'))

    def test_terminal_reporter_renders_distinct_tasks_in_fixed_width_rows(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        reporter = start.SetupReporter(output, environment={'TERM': 'xterm-256color'})
        reporter.intro('/private/setup/logs')
        self.assertEqual(output.getvalue(), '')
        reporter.begin('Building native source dependencies')
        reporter.output_line(b'Downloading source archive: gmp')
        reporter.output_line(b'################################ 42.6%')
        reporter.output_line(b'Building dependency: gmp')
        reporter.output_line(b'config.status: linking mpn/x86_64')
        reporter.progress(12)
        rendered = output.getvalue()
        self.assertIn('\033[2J\033[H', rendered)
        frame = rendered.rsplit('\033[11A', 1)[-1]
        frame = start.ANSI_ESCAPE.sub('', frame)
        rows = [line for line in frame.splitlines() if line.strip()]
        self.assertEqual(len(rows), 11)
        self.assertEqual(len({reporter.display_width(row) for row in rows}), 1)
        self.assertTrue(all(reporter.display_width(row) <= reporter.terminal_width for row in rows))
        self.assertEqual(rows[0].split('   | ', 1)[0].strip(), reporter.CREEPER_START[0])
        self.assertIn('Building GMP', rows[0].split('   | ', 1)[1])
        self.assertIn('config.status: linking mpn/x86_64', rows[1].split('   | ', 1)[1])
        self.assertEqual(tuple(row.split('   | ', 1)[0].strip() for row in rows),
                         reporter.CREEPER_START)
        self.assertIn('█', rendered)
        self.assertEqual(rendered.count('\033[2J'), 1)
        self.assertEqual(reporter.stage_label, 'Building GMP')
        self.assertNotIn('################################', frame)
        self.assertNotIn('Building native source dependencies\n', frame)
        self.assertIn('\033[1;32m', rendered)
        reporter.finish('')

    def test_completed_build_stage_replaces_unknown_progress_status(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        with patch.object(start.subprocess, 'run') as command, \
                contextlib.redirect_stdout(output):
            start.run(['/bin/true'], 'Preparing build tools')

        command.assert_called_once()
        frame = start.ANSI_ESCAPE.sub('', output.getvalue().rsplit('\033[11A', 1)[-1])
        self.assertIn('Preparing build tools | 00:00 | Complete', frame)
        self.assertNotIn('--%', frame)
        self.assertNotIn('Working', frame)
        rows = [line for line in frame.splitlines() if line.strip()]
        self.assertEqual(
            tuple(row.split('   | ', 1)[0].strip() for row in rows),
            ('',) + start.SetupReporter.CREEPER_UNICODE + ('', ''))

    def test_stage_changes_reuse_eleven_line_panel_without_heading(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        reporter = start.SetupReporter(output, environment={'TERM': 'xterm-256color'})
        reporter.begin('Building native source dependencies')
        reporter.begin('Building the Windows compatibility runtime')

        self.assertEqual(reporter.panel_rows, 11)
        self.assertEqual(output.getvalue().count('\033[2J'), 1)
        self.assertNotIn('\nBuilding the Windows compatibility runtime\n',
                         output.getvalue())
        frame = start.ANSI_ESCAPE.sub('', output.getvalue().rsplit('\033[11A', 1)[-1])
        rows = [line for line in frame.splitlines() if line.strip()]
        self.assertEqual(len(rows), 11)
        self.assertIn('Wine runtime', rows[0])
        self.assertIn('Applies WineGDK patches', rows[1])

    def test_compiler_fragments_never_become_task_labels(self):
        examples = (
            'lex',
            'whether',
            'that',
            'CCPIC... | checking',
            'if | checking',
            'for | checking',
            'checking for error.h... no',
            'Build | config.status: linking x86_64/ec',
            'config.status: linking mpn/x86_64',
            '! /Applications/Xcode.app/Contents/Developer/usr/bin/clang',
            '! ar cr libfoo.a foo.o',
            '! libtool -static -o libfoo.a',
            '! checking for error.h... no',
            '! -Wno-error=deprecated-declarations',
            '! -pthread',
        )
        reporter = start.SetupReporter(io.StringIO(), environment={'TERM': 'dumb'})
        reporter.begin('Building native source dependencies')
        for line in examples:
            previous_detail = reporter.detail
            reporter.output_line(line.encode())
            self.assertEqual(reporter.stage_label, 'Native dependencies')
            if line.startswith('! ') or line.startswith('checking '):
                self.assertEqual(reporter.detail, previous_detail)
            if reporter.tasks:
                self.assertEqual(list(reporter.tasks), ['stage'])
                self.assertEqual(reporter.tasks['stage']['name'], 'Native dependencies')
                self.assertNotEqual(reporter.tasks['stage']['name'],
                                    line.split('|', 1)[0].strip())
            self.assertLessEqual(len(reporter.detail), reporter.MAX_DETAIL_LENGTH)
        for line in examples:
            if line.startswith('! '):
                self.assertNotIn(line, reporter.stream.getvalue())
        self.assertNotIn('! ', reporter.stream.getvalue())

    def test_progress_context_keeps_three_recent_safe_lines_and_full_stage_name(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        reporter = start.SetupReporter(output, environment={'TERM': 'xterm-256color'})
        reporter.begin('Building native source dependencies')
        reporter.output_line(b'Building dependency: libunistring')
        reporter.output_line(b'CC tls13/finished.lo')
        reporter.output_line(b'GEN error.h')
        reporter.output_line(b'CC libgnu_gpl_la-error.lo')
        reporter.output_line(b'config.status: linking x86_64/ec')
        reporter.output_line(b'ordinary noisy output')
        reporter.progress(181)

        self.assertEqual(reporter.stage_label, 'Building libunistring')
        self.assertEqual(reporter.recent_output, [
            'GEN error.h',
            'CC libgnu_gpl_la-error.lo',
            'config.status: linking x86_64/ec',
        ])
        frame = start.ANSI_ESCAPE.sub('', output.getvalue().rsplit('\033[11A', 1)[-1])
        rows = [line for line in frame.splitlines() if line.strip()]
        self.assertEqual(len(rows), 11)
        self.assertIn('Building libunistring', rows[0])
        self.assertIn('CC libgnu_gpl_la-error.lo', rows[2])
        self.assertIn('config.status: linking x86_64/ec', rows[3])
        self.assertTrue(all(reporter.display_width(line) <= reporter.terminal_width for line in rows))
        self.assertNotIn('ordinary noisy output', frame)

    def test_progress_context_clips_details_to_terminal_width(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        with patch.object(start.shutil, 'get_terminal_size', return_value=os.terminal_size((30, 24))):
            reporter = start.SetupReporter(output, environment={'TERM': 'xterm-256color'})
        reporter.begin('Building native source dependencies')
        reporter.output_line(b'Building dependency: libunistring')
        reporter.output_line(('CC ' + 'x' * 300).encode())
        reporter.progress(60)
        frame = start.ANSI_ESCAPE.sub('', output.getvalue().rsplit('\033[11A', 1)[-1])
        rows = [line for line in frame.splitlines() if line.strip()]
        self.assertEqual(len(rows), 11)
        self.assertTrue(all(reporter.display_width(line) <= 30 for line in rows))
        self.assertIn('Build', rows[0])
        self.assertNotIn('ordinary noisy output', frame)

    def test_narrow_terminal_falls_back_to_compact_creeper(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        with patch.object(start.shutil, 'get_terminal_size', return_value=os.terminal_size((20, 24))):
            reporter = start.SetupReporter(output, environment={'TERM': 'xterm-256color'})
        reporter.begin('Building native source dependencies')

        frame = start.ANSI_ESCAPE.sub('', output.getvalue().rsplit('\033[11A', 1)[-1])
        rows = [line for line in frame.splitlines() if line.strip()]
        self.assertFalse(reporter.unicode_creeper)
        self.assertEqual(len(rows), 11)
        self.assertTrue(all(reporter.display_width(line) == 20 for line in rows))
        self.assertIn('########', rows[0])

    def test_creeper_indicator_uses_known_download_progress(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        reporter = start.SetupReporter(output, environment={'TERM': 'xterm-256color'})
        reporter.begin('Building native source dependencies')
        reporter.output_line(b'Downloading source archive: freetype')
        reporter.output_line(b'################################ 42.6%')
        reporter.progress(10)

        frame = start.ANSI_ESCAPE.sub('', output.getvalue().rsplit('\033[11A', 1)[-1])
        rows = [line for line in frame.splitlines() if line.strip()]
        self.assertEqual(len(rows), 11)
        self.assertIn('Downloading FreeType', rows[0].split('   | ', 1)[1])
        art = tuple(row.split('   | ', 1)[0].strip() for row in rows)
        self.assertNotEqual(art, reporter.CREEPER_START)
        self.assertIn('43%', rows[0].split('   | ', 1)[1])

    def test_creeper_animation_has_exact_start_and_padded_end_frames(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        reporter = start.SetupReporter(output, environment={'TERM': 'xterm-256color'})
        reporter.percent = 0
        self.assertEqual(reporter.creeper_frame(), reporter.CREEPER_START)

        reporter.percent = 100
        expected = (('',) + reporter.CREEPER_UNICODE + ('', ''))
        self.assertEqual(reporter.creeper_frame(), expected)
        self.assertEqual(len(reporter.creeper_frame()), reporter.CREEPER_ROWS)

        reporter.percent = 0
        reporter.render_progress(0)
        start_frame = [line for line in output.getvalue().splitlines() if '   | ' in line]
        start_column = reporter.display_width(start_frame[0][:start_frame[0].index('   | ')])
        reporter.percent = 100
        reporter.render_progress(1)
        end_frame = [
            line for line in start.ANSI_ESCAPE.sub('', output.getvalue()).splitlines()
            if '   | ' in line
        ][-reporter.CREEPER_ROWS:]
        self.assertEqual(len(end_frame), reporter.CREEPER_ROWS)
        self.assertEqual(end_frame[1].split('   | ', 1)[0].strip(), reporter.CREEPER_UNICODE[0])
        end_column = reporter.display_width(
            end_frame[0][:end_frame[0].index('   | ')])
        self.assertEqual(end_column, start_column)

    def test_indeterminate_creeper_animation_never_claims_a_percentage(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        reporter = start.SetupReporter(output, environment={'TERM': 'xterm-256color'})
        reporter.begin('Building the Windows compatibility runtime')
        first_frame = output.getvalue().split('   | ', 1)[1].splitlines()[0]
        reporter.progress(1)
        second_frame = output.getvalue().rsplit('\033[11A', 1)[-1]
        second = second_frame.split('   | ', 1)[1].splitlines()[0]

        self.assertIn('Working', first_frame)
        self.assertIn('Working', second)
        self.assertNotIn('%', first_frame)
        self.assertNotIn('%', second)
        self.assertNotEqual(first_frame, second)

    def test_creeper_intermediate_transformation_is_monotonic(self):
        reporter = start.SetupReporter(io.StringIO(), environment={'TERM': 'dumb'})
        reporter.unicode_creeper = True
        reporter.art_slot_width = reporter.CREEPER_UNICODE_WIDTH
        frames = []
        for percent in (0, 25, 50, 75, 99):
            reporter.percent = percent
            frames.append(reporter.creeper_frame())

        def transformed(frame, row, column):
            return frame[row][column] in 'gk'

        for lower, upper in zip(frames, frames[1:]):
            for row in range(reporter.CREEPER_ROWS):
                for column in range(reporter.CREEPER_UNICODE_WIDTH):
                    if column >= len(lower[row]):
                        continue
                    if transformed(lower, row, column):
                        self.assertTrue(transformed(upper, row, column))

    def test_creeper_no_color_fallback_keeps_eleven_rows(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        reporter = start.SetupReporter(
            output, environment={'TERM': 'xterm-256color', 'NO_COLOR': '1'})
        reporter.begin('Building native source dependencies')
        reporter.percent = 100
        reporter.render_progress(10)

        frame = start.ANSI_ESCAPE.sub('', output.getvalue().rsplit('\033[11A', 1)[-1])
        rows = [line for line in frame.splitlines() if line.strip()]
        self.assertFalse(reporter.unicode_creeper)
        self.assertEqual(len(rows), reporter.CREEPER_ROWS)
        self.assertEqual(tuple(row.split('   | ', 1)[0].strip() for row in rows),
                         ('',) + reporter.CREEPER_ASCII + ('', ''))
        self.assertNotIn('\033[', frame)

    def test_autoconf_probe_exclamation_is_detail_not_a_failure_event(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        reporter = start.SetupReporter(output, environment={'TERM': 'xterm-256color'})
        reporter.begin('Building native source dependencies')
        probe = ('! checking if we should use a .note.GNU-stack section... '
                 '/Applications/Xcode.app/Contents/Developer/usr/bin/ld')
        reporter.output_line(probe.encode())

        self.assertEqual(reporter.detail, 'Working')
        self.assertEqual(reporter.stage_label, 'Native dependencies')
        self.assertNotIn(reporter.paint('!', '1;31'), output.getvalue())
        self.assertNotIn(probe, output.getvalue())

    def test_nonzero_process_exit_is_reported_as_failure(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'Setup: Close the standalone game and its account helper before rebuilding that helper.\n')
        os.close(write_fd)

        class Process:
            pid = os.getpid()
            stdout = os.fdopen(read_fd, 'rb', buffering=0)

            @staticmethod
            def poll():
                return 1

            @staticmethod
            def wait(timeout=None):
                return 1

        def spawn(_command, *, cwd, stdout, stderr, start_new_session, bufsize):
            return Process()

        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        log = self.root / 'logs/failed-build.log'
        with patch.object(start.subprocess, 'Popen', side_effect=spawn), \
                contextlib.redirect_stdout(output), \
                self.assertRaisesRegex(RuntimeError, 'failed; see the setup log'):
            start.run(['build'], 'Building the Windows compatibility runtime', log)

        frame = start.ANSI_ESCAPE.sub('', output.getvalue().rsplit('\033[11A', 1)[-1])
        self.assertIn('Wine runtime | 00:00 | FAILED', frame)
        self.assertIn('Cause: Setup: Close the standalone game',
                      frame)
        self.assertIn('Action: quit the game/helper normally, then retry.', frame)
        self.assertIn('Full log: logs/failed-build.log', frame)
        self.assertEqual(frame.count('FAILED'), 1)
        self.assertNotIn('--%', frame)

    def test_completed_archive_event_clears_live_download_row(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        reporter = start.SetupReporter(output, environment={'TERM': 'xterm-256color'})
        reporter.begin('Building native source dependencies')
        reporter.output_line(b'Downloading source archive: freetype')
        reporter.output_line(b'############################## 97%')
        reporter.output_line(b'Reusing verified source archive: freetype')
        self.assertNotIn('download:freetype', reporter.tasks)
        self.assertIsNone(reporter.current_download)
        self.assertEqual(reporter.stage_label, 'Native dependencies')
        self.assertEqual(reporter.tasks['stage']['status'], 'Preparing source builds')
        reporter.progress(199)
        self.assertNotIn('97%', output.getvalue().split('\r')[-1])
        self.assertIn('Reused verified archive: freetype', output.getvalue())
        self.assertNotIn('✓ Reusing verified source archive', output.getvalue())
        self.assertEqual(reporter.panel_rows, 11)

    def test_non_tty_download_updates_are_throttled_to_coarse_milestones(self):
        output = io.StringIO()
        reporter = start.SetupReporter(output, environment={'TERM': 'dumb'})
        reporter.begin('Building native source dependencies')
        reporter.output_line(b'Downloading source archive: freetype')
        for percent in (1, 5, 24, 26, 49, 51, 74, 76, 99, 100):
            reporter.output_line(b'#' * 40 + f' {percent}%'.encode())
        text = output.getvalue()
        milestones = [line for line in text.splitlines()
                      if 'Downloading FreeType:' in line and '%' in line]
        self.assertEqual(len(milestones), 5)
        self.assertEqual([int(line.split('%', 1)[0].rsplit(' ', 1)[-1]) for line in milestones],
                         [0, 26, 51, 76, 100])
        self.assertNotIn('#' * 20, text)

    def test_no_color_and_opt_in_fun_facts(self):
        class TTYCapture(io.StringIO):
            def isatty(self):
                return True

        output = TTYCapture()
        reporter = start.SetupReporter(output, fun_facts=True,
                                       environment={'TERM': 'xterm-256color', 'NO_COLOR': '1'})
        reporter.begin('Building the Windows compatibility runtime')
        reporter.output_line(b'Compiling crate')
        reporter.progress(12)
        reporter.finish('Complete')
        self.assertNotRegex(output.getvalue(), r'\x1b\[[0-9;]*m')
        self.assertIn('Fun fact:', output.getvalue())
        self.assertIn('########', output.getvalue())
        self.assertNotIn('🟩', output.getvalue())

    def test_ascii_only_tty_uses_readable_creeper_fallback(self):
        class ASCIITTY(io.StringIO):
            encoding = 'ascii'

            def isatty(self):
                return True

        output = ASCIITTY()
        reporter = start.SetupReporter(output, environment={'TERM': 'xterm-256color'})
        reporter.begin('Building native source dependencies')

        self.assertFalse(reporter.unicode_creeper)
        self.assertIn('########', output.getvalue())
        self.assertNotIn('🟩', output.getvalue())
        self.assertEqual(reporter.panel_rows, 11)

    def test_source_update_refreshes_generated_helper_before_existing_tree_verification(self):
        source = Path(__file__).resolve().parents[1]
        with patch.object(start, 'ROOT', source):
            bootstrap = start.module('bootstrap-sources')
        tree = self.root / 'sources/xodus'
        tree.mkdir(parents=True)
        for original, generated in bootstrap.HELPER_INPUTS.items():
            author = self.root / original
            author.parent.mkdir(parents=True, exist_ok=True)
            author.write_bytes(b'updated authoritative source')
            target = tree / generated
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'previous generated copy')

        def verify(repository, name, project):
            self.assertEqual(repository, tree)
            for original, generated in bootstrap.HELPER_INPUTS.items():
                self.assertEqual((repository / generated).read_bytes(),
                                 (self.root / original).read_bytes())

        bootstrap.PROJECTS = {'xodus': {'patches': ()}}
        bootstrap.SOURCE_INPUTS = ()
        with patch.object(start, 'module', return_value=bootstrap), \
                patch.object(bootstrap, 'require_build_path', create=True), \
                patch.object(bootstrap, 'verify_project', side_effect=verify) as check, \
                patch.object(start, 'native_dependencies', return_value=self.root / 'dependencies'), \
                patch.object(start, 'Steps') as steps, patch.object(start, 'run'):
            start.setup(2, 'CA')
        check.assert_called_once()
        self.assertTrue(any(call.args[0] == 'helper' for call in steps.return_value.step.call_args_list))

    def test_source_release_includes_start_and_console_authentication(self):
        source = Path(__file__).resolve().parents[1]
        with patch.object(start, 'ROOT', source):
            exporter = start.module('export-source')
        for name in ('Start.command', 'tools/start.py', 'tools/install-build-tools.sh',
                     'tests/test_start.py', 'patches/standalone-wine-xuser-z-device-auth.patch'):
            self.assertIn(name, exporter.ALLOWED_FILES)

    def test_fresh_homebrew_rustup_is_found_in_its_keg_only_path(self):
        source = Path(__file__).resolve().parents[1]
        brew_root = self.root / 'homebrew'
        (brew_root / 'bin').mkdir(parents=True)
        brew = brew_root / 'bin/brew'
        brew.write_text('''#!/bin/bash
if [ "$1" = list ]; then exit 1; fi
printf '%s\\n' "$@" > "$(dirname "$0")/../installed-formulae"
mkdir -p "$(dirname "$0")/../opt/rustup/bin"
printf '#!/bin/sh\\nexit 0\\n' > "$(dirname "$0")/../opt/rustup/bin/rustup"
chmod +x "$(dirname "$0")/../opt/rustup/bin/rustup"
''')
        brew.chmod(0o755)
        script = self.root / 'tools.sh'
        script.write_text((source / 'tools/install-build-tools.sh').read_text()
                          .replace('/opt/homebrew', str(brew_root))
                          .replace('/usr/bin/arch', '/usr/bin/true'))
        result = subprocess.run(['/bin/bash', str(script)],
            env=dict(os.environ, PATH='/usr/bin:/bin', CARGO_HOME=str(self.root / 'cargo')),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((brew_root / 'opt/rustup/bin/rustup').is_file())
        formulae = (brew_root / 'installed-formulae').read_text().splitlines()
        self.assertIn('python@3.13', formulae)
        self.assertIn('pkgconf', formulae)
        self.assertNotIn('gnutls', formulae)
        self.assertNotIn('freetype', formulae)


if __name__ == '__main__':
    unittest.main()
