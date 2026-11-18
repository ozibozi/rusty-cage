#!/usr/bin/env python3
"""Set up owned Windows Bedrock and launch it; --check is strictly read-only."""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
STAGE_NAMES = ('wine', 'graphics', 'helper', 'probes', 'bridge')
STAGE_DETAILS = {
    'Preparing build tools': 'Checks required Apple, Homebrew and Rust build tools.',
    'Checking source-build Python': 'Verifies the interpreter used by current source-build scripts.',
    'Building native source dependencies': 'Downloads pinned GnuTLS/FreeType; builds the x86-64 dependency prefix.',
    'Building the Windows compatibility runtime': 'Applies WineGDK patches and compiles the x86-64 Wine runtime.',
    'Installing graphics support': 'Verifies and stages pinned DXMT graphics support.',
    'Building Microsoft sign-in': 'Builds the Rust helper with ownership and integrity checks.',
    'Building graphics checks': 'Compiles Windows graphics and runtime validation probes.',
    'Building and testing the graphics memory fixes': 'Builds, tests and stages the DXMT Unix bridge.',
    'Installing Microsoft game support': 'Obtains the Microsoft component required by the current Wine setup.',
    'Downloading Minecraft': 'Checks PC ownership; downloads and verifies the Windows package.',
    'Preparing Minecraft': 'Prepares the Wine prefix without replacing existing worlds.',
    'Starting Minecraft': 'Launches the vanilla x86-64 Bedrock client through Wine.',
}
FUN_FACTS = (
    'This setup verifies pinned source archives by SHA-256 before extraction.',
    'The Rust account helper checks Microsoft ownership before game acquisition.',
    'The current game path keeps Minecraft vanilla; the launcher does not add visual mods.',
)
REPORTER = None
ANSI_ESCAPE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
SENSITIVE_OUTPUT = re.compile(
    r'(?i)\b(?:authorization|access[_ -]?token|refresh[_ -]?token|token|client[_ -]?secret|'
    r'secret|credential|password|cookie|bearer)\b|https?://\S+')
COMPLETED_STATUS = re.compile(
    r'(?i)^\s*(?:finished|verified|reusing|reused|discarded|complete(?:d)?|installed)\b')
ACTIVE_STATUS = re.compile(
    r'(?i)^\s*(?:compiling|checking|downloading|installing|building|generating|linking|'
    r'resuming|extracting|configuring|retrying)\b')
CONFIGURE_PROBE = re.compile(r'(?i)^\s*checking\b')
COMPILER_OUTPUT = re.compile(r'(?i)^(?:CC|CXX|GEN|LD|LINK)\s+\S')
CURL_PROGRESS = re.compile(r'^\s*#+.*\b\d{1,3}(?:\.\d+)?%')
CMAKE_PROGRESS = re.compile(r'\[\s*\d{1,3}%\]')
CURL_PERCENT = re.compile(r'\b(\d{1,3}(?:\.\d+)?)%')
DOWNLOAD_NAME = re.compile(r'(?i)(?:source archive|graphics asset):\s*([A-Za-z0-9._+-]+)')
RESUME_NAME = re.compile(r'(?i)Resuming partial source download:\s*([A-Za-z0-9._+-]+)')
NATIVE_COMPONENT_LABELS = {
    'gmp': 'GMP',
    'nettle': 'Nettle',
    'libtasn1': 'libtasn1',
    'libunistring': 'libunistring',
    'gnutls': 'GnuTLS',
    'freetype': 'FreeType',
}
DEPENDENCY_NAME = re.compile(r'(?i)^Building dependency:\s*([A-Za-z0-9._+-]+)$')
ARCHIVE_EVENT = re.compile(
    r'(?i)(?:Verified and cached|Reusing verified) source archive:\s*([A-Za-z0-9._+-]+)')
CMAKE_PERCENT = re.compile(r'\[\s*(\d{1,3})%\]\s*(.*)')


class SetupReporter:
    """Render truthful, optionally colorful stage progress for an interactive terminal."""

    MAX_ROWS = 4
    MIN_PANEL_WIDTH = 16
    CREEPER_ROWS = 11
    CREEPER_UNICODE_WIDTH = 17
    CREEPER_ASCII_WIDTH = 8
    CONTEXT_LINES = 3
    MAX_DETAIL_LENGTH = 240
    CREEPER_START = (
        '█████████████████',
        '█░░░░░░░░░░░░░░░█',
        '█░░░░░░░░░░░░░░░█',
        '█░░████░░░████░░█',
        '█░░████░░░████░░█',
        '█░░░░░███░░░░░░█',
        '█░░░░███████░░░░█',
        '█░░░░███████░░░░█',
        '█░░░░██░░░██░░░░█',
        '█░░░░░░░░░░░░░░░█',
        '█████████████████',
    )
    CREEPER_UNICODE = (
        '🟩🟩🟩🟩🟩🟩🟩🟩',
        '🟩⬛⬛🟩🟩⬛⬛🟩',
        '🟩⬛⬛🟩🟩⬛⬛🟩',
        '🟩🟩🟩⬛⬛🟩🟩🟩',
        '🟩🟩⬛⬛⬛⬛🟩🟩',
        '🟩🟩⬛⬛⬛⬛🟩🟩',
        '🟩🟩⬛🟩🟩⬛🟩🟩',
        '🟩🟩🟩🟩🟩🟩🟩🟩',
    )
    CREEPER_ASCII = (
        '########',
        '#  ##  #',
        '#  ##  #',
        '###  ###',
        '##    ##',
        '##    ##',
        '## ## ##',
        '########',
    )
    CREEPER_START_ASCII = tuple(
        line.replace('█', '#').replace('░', '.')
        for line in CREEPER_START
    )

    def __init__(self, stream=None, *, fun_facts=False, environment=None):
        self.stream = sys.stdout if stream is None else stream
        environment = os.environ if environment is None else environment
        try:
            is_tty = self.stream.isatty()
        except (AttributeError, OSError):
            is_tty = False
        self.tty = bool(is_tty and environment.get('TERM') != 'dumb')
        self.color = bool(self.tty and not environment.get('NO_COLOR')
                          and environment.get('CLICOLOR') != '0')
        encoding = getattr(self.stream, 'encoding', None) or 'utf-8'
        try:
            for line in (*self.CREEPER_START, *self.CREEPER_UNICODE):
                line.encode(encoding)
            self.unicode_creeper = self.color
        except (LookupError, UnicodeEncodeError):
            self.unicode_creeper = False
        self.terminal_width = max(
            self.MIN_PANEL_WIDTH, min(140, shutil.get_terminal_size((100, 24)).columns))
        unicode_prefix_width = (
            2 + self.CREEPER_UNICODE_WIDTH + self.display_width('   | '))
        if self.unicode_creeper and self.terminal_width < unicode_prefix_width:
            self.unicode_creeper = False
        self.art_slot_width = (
            self.CREEPER_UNICODE_WIDTH if self.unicode_creeper else self.CREEPER_ASCII_WIDTH)
        self.fun_facts = fun_facts
        self.fact_index = 0
        self.last_fact_elapsed = 0
        self.started = time.monotonic()
        self.spinner_index = 0
        self.tasks = {}
        self.current_download = None
        self.stage_label = 'Setup'
        self.detail = 'Working'
        self.percent = None
        self.recent_output = []
        self.panel_rows = 0
        self.screen_initialized = False
        self.non_tty_buckets = {}
        self.last_non_tty_update = 0
        self.stage_started = time.monotonic()
        self.phase_result = None
        self.failure_reported = False

    def paint(self, text, color):
        if not self.color:
            return text
        return f'\033[{color}m{text}\033[0m'

    @staticmethod
    def display_width(text):
        text = ANSI_ESCAPE.sub('', text)
        return sum(2 if unicodedata.east_asian_width(character) in ('W', 'F') else 1
                   for character in text)

    @staticmethod
    def clip_display(text, width):
        clipped = []
        used = 0
        for character in text:
            character_width = (
                2 if unicodedata.east_asian_width(character) in ('W', 'F') else 1)
            if used + character_width > width:
                break
            clipped.append(character)
            used += character_width
        return ''.join(clipped)

    def creeper_frame(self):
        if self.percent is None:
            return self.CREEPER_START if self.unicode_creeper else self.creeper_frame_for_ascii_start()

        progress = min(100.0, max(0.0, float(self.percent)))
        if progress == 0:
            return (self.CREEPER_START if self.unicode_creeper else
                    self.creeper_frame_for_ascii_start())
        if progress == 100:
            art = self.CREEPER_UNICODE if self.unicode_creeper else self.CREEPER_ASCII
            padding = (self.CREEPER_ROWS - len(art)) // 2
            return (('',) * padding + art
                    + ('',) * (self.CREEPER_ROWS - padding - len(art)))

        width = self.art_slot_width
        if self.unicode_creeper:
            source = self.CREEPER_START
        else:
            source = self.creeper_frame_for_ascii_start()
        source = tuple(line.ljust(width) for line in source)
        target_count = int(progress * self.CREEPER_ROWS * width / 100)
        frame = []
        for row, line in enumerate(source):
            cells = []
            target_row = min(
                len(self.CREEPER_UNICODE) - 1,
                max(0, round((row - 1) * (len(self.CREEPER_UNICODE) - 1)
                             / (self.CREEPER_ROWS - 3))))
            for column, original in enumerate(line):
                if row * width + column < target_count:
                    target_column = min(
                        len(self.CREEPER_UNICODE[0]) - 1,
                        round(column * (self.CREEPER_ASCII_WIDTH - 1) / (width - 1)))
                    target = self.CREEPER_UNICODE[target_row][target_column]
                    cells.append('g' if target == '🟩' else 'k')
                else:
                    cells.append(original)
            frame.append(''.join(cells))
        return tuple(frame)

    def creeper_frame_for_ascii_start(self):
        return tuple(
            ''.join(line[round(column * (len(line) - 1)
                               / (self.CREEPER_ASCII_WIDTH - 1))]
                    for column in range(self.CREEPER_ASCII_WIDTH))
            for line in self.CREEPER_START_ASCII
        )

    def format_creeper_row(self, drawing):
        cells = []
        for character in drawing:
            if character in ('g', '█'):
                glyph, color = ('█' if self.unicode_creeper else '#'), '1;32'
            elif character in ('k', '░'):
                glyph, color = ('░' if self.unicode_creeper else ' '), '90'
            else:
                glyph, color = character, None
            cells.append(self.paint(glyph, color) if self.unicode_creeper and color else glyph)
        drawing = ''.join(cells)
        return drawing if self.unicode_creeper else self.paint(drawing, '1;32')

    def begin(self, label):
        if self.tty and not self.screen_initialized:
            self.stream.write('\033[2J\033[H')
            self.screen_initialized = True
        self.tasks.clear()
        self.current_download = None
        self.stage_label = {
            'Building native source dependencies': 'Native dependencies',
            'Building the Windows compatibility runtime': 'Wine runtime',
            'Building Microsoft sign-in': 'Sign-in helper',
            'Installing graphics support': 'Graphics support',
            'Building graphics checks': 'Graphics checks',
            'Building and testing the graphics memory fixes': 'Graphics bridge',
            'Installing Microsoft game support': 'Microsoft component',
            'Downloading Minecraft': 'Minecraft download',
            'Preparing Minecraft': 'Minecraft setup',
            'Starting Minecraft': 'Minecraft launch',
        }.get(label, label)
        self.detail = 'Working'
        self.percent = None
        self.phase_result = None
        self.failure_reported = False
        details = STAGE_DETAILS.get(label)
        self.recent_output[:] = [details] if details else []
        self.non_tty_buckets.clear()
        self.last_non_tty_update = 0
        self.stage_started = time.monotonic()
        if self.tty:
            self.render_progress(0)
        else:
            self.stream.write('\n' + label + '\n')
            if details:
                self.stream.write('  ' + details + '\n')
            self.stream.flush()
        if self.fun_facts and self.fact_index == 0:
            self.fact()

    def fact(self):
        text = 'Fun fact: ' + FUN_FACTS[self.fact_index % len(FUN_FACTS)]
        self.fact_index += 1
        if self.tty:
            self.recent_output.append(text)
            self.recent_output = self.recent_output[-self.CONTEXT_LINES:]
            self.render_progress(int(time.monotonic() - self.stage_started))
        else:
            self.stream.write('  ' + text + '\n')
            self.stream.flush()

    def clear_panel(self):
        if not self.tty or not self.panel_rows:
            return
        self.stream.write(f'\033[{self.panel_rows}A')
        for _ in range(self.panel_rows):
            self.stream.write('\r\033[K\n')
        self.panel_rows = 0

    def update_task(self, key, name, status, percent=None):
        self.tasks.pop(key, None)
        self.tasks[key] = {'name': name, 'status': status, 'percent': percent}
        while len(self.tasks) > self.MAX_ROWS:
            self.tasks.pop(next(iter(self.tasks)))
        if self.tty:
            self.render_progress(int(time.monotonic() - self.stage_started))
        else:
            self.non_tty_update(key, percent)

    def update_stage(self, status, percent=None, *, include_context=True):
        self.detail = status[:self.MAX_DETAIL_LENGTH]
        if (include_context and status
                and (not self.recent_output or self.recent_output[-1] != self.detail)):
            self.recent_output.append(self.detail)
            self.recent_output = self.recent_output[-self.CONTEXT_LINES:]
        if percent is not None:
            self.percent = percent
        self.tasks.clear()
        self.tasks['stage'] = {
            'name': self.stage_label,
            'status': self.detail,
            'percent': self.percent,
        }
        if self.tty:
            self.render_progress(int(time.monotonic() - self.stage_started))
        else:
            self.non_tty_update('stage', self.percent)

    def render_progress(self, elapsed):
        percent = self.percent
        if percent is None:
            self.spinner_index += 1
            percent_text = 'Working'
        else:
            percent_text = f'{round(percent)}%'

        elapsed_text = f'{elapsed // 60:02}:{elapsed % 60:02}'
        art = self.creeper_frame()
        recent = self.recent_output[-self.CONTEXT_LINES:]
        result = self.phase_result or percent_text
        if percent is None and not self.phase_result:
            result += ' ' + '|/-\\'[self.spinner_index % 4]
        details = [self.stage_label + ' | ' + elapsed_text + ' | ' + result]
        if recent:
            details.extend(recent)
        elif not self.phase_result and self.detail:
            details.append(self.detail)
        details = details[:len(art)]
        rows = []
        for index, drawing in enumerate(art):
            formatted = self.format_creeper_row(drawing)
            drawing_width = self.display_width(formatted)
            left = '  ' + formatted + ' ' * max(0, self.art_slot_width - drawing_width)
            prefix = left + '   | '
            detail = details[index] if index < len(details) else ''
            detail_width = max(0, self.terminal_width - self.display_width(prefix))
            clipped = self.clip_display(detail, detail_width)
            color = '1;31' if index == 0 and self.phase_result == 'FAILED' else None
            row = prefix + (self.paint(clipped, color) if color else clipped)
            rows.append(row + ' ' * max(
                0, self.terminal_width - self.display_width(row)))

        if self.panel_rows:
            self.stream.write(f'\033[{self.panel_rows}A')
        for row in rows:
            self.stream.write('\r\033[K' + row + '\n')
        self.panel_rows = len(rows)
        self.stream.flush()

    def non_tty_update(self, key, percent):
        now = time.monotonic()
        task = self.tasks.get(key)
        if not task:
            return
        if percent is not None:
            milestone = min(4, int(percent // 25))
            if self.non_tty_buckets.get(key) == milestone:
                return
            self.non_tty_buckets[key] = milestone
            message = f'{task["name"]}: {round(percent)}% — {task["status"]}'
        else:
            if self.last_non_tty_update and now - self.last_non_tty_update < 2:
                return
            message = f'{task["name"]}: {task["status"]}'
        self.last_non_tty_update = now
        self.stream.write('  ' + self.paint('›', '1;34') + ' ' + message[:72] + '\n')
        self.stream.flush()

    def event(self, text, *, success=False, failed=False, cause=None, log=None):
        if failed:
            self.failure_reported = True
        if self.tty:
            if failed:
                self.phase_result = 'FAILED'
                self.recent_output = []
                if cause:
                    self.recent_output.append('Cause: ' + cause)
                    if 'Close the standalone game and its account helper' in cause:
                        self.recent_output.append(
                            'Action: quit the game/helper normally, then retry.')
                else:
                    self.recent_output.append(text)
                if log:
                    try:
                        log_name = log.relative_to(ROOT).as_posix()
                    except ValueError:
                        log_name = log.name
                    self.recent_output.append('Full log: ' + log_name)
                self.recent_output = self.recent_output[-self.CONTEXT_LINES:]
            else:
                self.recent_output = [text]
            self.update_stage(text[:self.MAX_DETAIL_LENGTH], include_context=False)
            return
        self.clear_panel()
        marker, color = ('!', '1;31') if failed else (
            ('✓', '1;32') if success else ('↻', '1;33'))
        self.stream.write('  ' + self.paint(marker, color) + ' ' + text[:68] + '\n')
        if cause:
            self.stream.write('    Cause: ' + cause[:180] + '\n')
        if failed:
            self.stream.write('    Reopen Start.command to retry after resolving the cause.\n')
        if log:
            try:
                log_name = log.relative_to(ROOT).as_posix()
            except ValueError:
                log_name = log.name
            self.stream.write('    Full log: ' + log_name + '\n')
        self.stream.flush()

    def output_line(self, raw):
        text = raw.decode('utf-8', errors='replace')
        text = ANSI_ESCAPE.sub('', text)
        text = ''.join(character for character in text
                       if character.isprintable() or character == '\t')
        text = ' '.join(text.split())
        if not text or text.startswith('!') or SENSITIVE_OUTPUT.search(text):
            return
        if (CONFIGURE_PROBE.match(text)
                or not (COMPLETED_STATUS.match(text) or DOWNLOAD_NAME.search(text)
                        or RESUME_NAME.search(text) or CURL_PROGRESS.match(text)
                        or CMAKE_PROGRESS.search(text) or ACTIVE_STATUS.match(text)
                        or COMPILER_OUTPUT.match(text)
                        or re.search(r'(?i)^config\.status:\s*linking\b', text))):
            return
        text = text.replace(str(Path.home()), '~').replace(str(ROOT), '$SOURCE')
        completed = bool(COMPLETED_STATUS.search(text))
        download = DOWNLOAD_NAME.search(text) or RESUME_NAME.search(text)
        dependency = DEPENDENCY_NAME.match(text)
        archive_event = ARCHIVE_EVENT.search(text)
        if completed:
            if archive_event:
                self.current_download = None
                self.stage_label = 'Native dependencies'
                self.percent = None
                self.recent_output = [
                    'Preparing source builds',
                    'Reused verified archive: ' + archive_event.group(1).lower(),
                ]
                self.update_stage('Preparing source builds', include_context=False)
                return
            elif 'Verified dependency prefix' in text:
                self.percent = None
                self.detail = 'Verified'
                self.tasks.clear()
                self.current_download = None
                self.update_stage('Verified', include_context=False)
                return
            self.update_stage(text[:self.MAX_DETAIL_LENGTH])
            return
        if CURL_PROGRESS.match(text):
            if self.current_download:
                match = CURL_PERCENT.search(text)
                if match:
                    percent = min(100.0, max(0.0, float(match.group(1))))
                    self.update_stage(self.detail, percent)
            return
        cmake = CMAKE_PERCENT.search(text)
        if cmake:
            percent = min(100.0, max(0.0, float(cmake.group(1))))
            self.update_stage(cmake.group(2).strip() or self.detail, percent)
            return
        if dependency:
            component = dependency.group(1).lower()
            label = NATIVE_COMPONENT_LABELS.get(component)
            if label:
                self.recent_output.clear()
                self.current_download = None
                self.percent = None
                self.stage_label = 'Building ' + label
                self.update_stage('Compiling source', include_context=False)
            return
        if download:
            component = download.group(1).lower()
            label = NATIVE_COMPONENT_LABELS.get(component)
            if label:
                self.recent_output.clear()
                self.current_download = component
                self.percent = 0
                self.stage_label = 'Downloading ' + label
                self.update_stage('Fetching verified source', include_context=False)
            return
        if COMPILER_OUTPUT.match(text):
            self.update_stage(text[:self.MAX_DETAIL_LENGTH])
            return
        if (ACTIVE_STATUS.search(text)
                or re.search(r'(?i)config\.status:\s*linking\b', text)):
            self.update_stage(text[:self.MAX_DETAIL_LENGTH])

    def progress(self, elapsed):
        total_elapsed = int(time.monotonic() - self.started)
        if self.fun_facts and total_elapsed - self.last_fact_elapsed >= 300:
            self.last_fact_elapsed = total_elapsed
            self.fact()
        if self.tty:
            self.render_progress(elapsed)
        else:
            suffix = ' — ' + self.detail[:48] if self.detail else ''
            print(f'  Working ({elapsed // 60:02}:{elapsed % 60:02}){suffix}',
                  file=self.stream, flush=True)

    def finish(self, result):
        if self.tty:
            if result:
                self.phase_result = result
                if result == 'Complete':
                    self.percent = 100
                self.recent_output = []
                self.update_stage(result, include_context=False)
            return
        self.clear_panel()
        if result:
            self.stream.write('  ' + self.paint(result, '1;32' if result == 'Complete' else '1;33') + '\n')
        self.stream.flush()

    def intro(self, log_directory):
        if self.tty:
            return
        self.stream.write('First setup builds Wine and downloads required components and your game. '
                          'You can resume if interrupted.\n')
        self.stream.write('Logs: ' + str(log_directory) + '\n')
        self.stream.flush()


def setup_reporter():
    global REPORTER
    if REPORTER is None or REPORTER.stream is not sys.stdout:
        REPORTER = SetupReporter()
    return REPORTER


def safe_failure_summary(log):
    if log is None:
        return None
    try:
        lines = log.read_bytes()[-256 * 1024:].decode('utf-8', errors='replace').splitlines()
    except OSError:
        return None
    candidates = []
    for raw in reversed(lines):
        line = ANSI_ESCAPE.sub('', raw).strip()
        if not (line.startswith('Setup: ')
                or re.match(r'(?i)^error(?:\[E\d+\])?:\s+', line)
                or re.match(r'(?i)^Caused by:\s+', line)):
            continue
        if SENSITIVE_OUTPUT.search(line):
            continue
        line = line.replace(str(Path.home()), '~').replace(str(ROOT), '$SOURCE')
        line = re.sub(r'https?://\S+', '<URL>', line)
        line = re.sub(r'(?i)([A-Za-z0-9_-]*(?:token|secret|password|credential|cookie)[A-Za-z0-9_-]*\s*[:=]\s*)\S+',
                      r'\1<redacted>', line)
        line = re.sub(r'\s+', ' ', line)
        line = line[:180]
        if line not in candidates:
            candidates.append(line)
        if len(candidates) == 2:
            break
    return ' '.join(reversed(candidates)) or None


def module(name):
    directory = 'client' if name in ('standalone', 'standalone-setup', 'standalone-pickers',
                                     'proprietary_consent') else 'tools'
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / directory / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(value)
    finally:
        sys.dont_write_bytecode = previous
    return value


def proprietary_consent():
    source = ROOT / 'client/proprietary_consent.py'
    spec = importlib.util.spec_from_file_location('bedrock_proprietary_consent', source)
    if spec is None or spec.loader is None:
        raise RuntimeError('The proprietary-component acceptance checker is missing.')
    value = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(value)
    finally:
        sys.dont_write_bytecode = previous
    return value


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def installed():
    """Reuse a complete local installation; Start is not a game/runtime updater."""
    try:
        if not proprietary_consent().acceptance_status(ROOT)[0]:
            return False
        launcher = module('standalone')
        launcher.prerequisites(ROOT / 'game')
        launcher.verify_unix_bridge()
        if not launcher.HELPER.is_file():
            return False
        setup = module('standalone-setup')
        status = setup.game_status(ROOT / 'game')
        return all(status[key] for key in ('prepared_windows_x64_game',
                   'minecraft_store_product', 'package_metadata_present', 'gameinput_installer_present'))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return False


def native_dependencies(python, jobs):
    """Reuse only verified outputs; preserve interrupted builds in private attempts."""
    dependencies = module('build-native-dependencies')
    selected = os.environ.get('STANDALONE_DEPENDENCY_PREFIX')
    if selected:
        prefix = Path(selected).expanduser().absolute()
        dependencies.verify(prefix)
        return prefix
    inputs = ('tools/build-native-dependencies.py', 'config/native-dependencies.lock.json')
    identity = hashlib.sha256(''.join(digest(ROOT / name) for name in inputs).encode()).hexdigest()
    directory = ROOT / 'build/start-native-dependencies' / identity
    checked = directory
    while checked != ROOT.parent:
        if checked.is_symlink():
            raise RuntimeError('The native dependency build directory must not be a symbolic link.')
        checked = checked.parent
    directory.mkdir(parents=True, exist_ok=True)
    for attempt in sorted(directory.glob('attempt-*')):
        if attempt.is_symlink():
            raise RuntimeError('A native dependency build attempt is a symbolic link; it was kept.')
        prefix = attempt / 'prefix'
        if os.path.lexists(prefix / 'dependency-manifest.json'):
            dependencies.verify(prefix)
            reporter = setup_reporter()
            message = 'Native source dependencies — already verified.'
            if reporter.tty:
                reporter.begin('Building native source dependencies')
                reporter.update_stage('Already verified')
            else:
                print(message, flush=True)
            return prefix
    # The builder refuses existing compiled outputs. A retry gets one fresh
    # attempt, while earlier source, archives and diagnostics remain available.
    attempt = Path(tempfile.mkdtemp(prefix='attempt-', dir=directory))
    cache = directory / 'download-cache'
    run([python, str(ROOT / 'tools/build-native-dependencies.py'), '--destination', str(attempt),
         '--cache-dir', str(cache), '--jobs', str(min(jobs, 8)), '--live-output'],
        'Building native source dependencies',
        ROOT / 'logs/standalone/setup/native-dependencies.log')
    prefix = attempt / 'prefix'
    dependencies.verify(prefix)
    return prefix


@contextlib.contextmanager
def lock(name):
    directory = ROOT / 'runtime/standalone'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Minecraft or another setup is already running. Let it finish first.')
        yield


def run(command, label, log=None):
    reporter = setup_reporter()
    reporter.begin(label)
    if log is None:
        try:
            subprocess.run(command, cwd=ROOT, check=True)
        except BaseException:
            reporter.finish('')
            raise
        reporter.finish('Complete')
        return
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('ab') as output:
        if output.tell():
            output.write(b'\n--- retry started; previous output retained ---\n')
            output.flush()
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True,
                                   bufsize=0)
        started = time.monotonic()
        last_progress = started
        pending = bytearray()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map() or process.poll() is None:
                events = selector.select(timeout=1 if reporter.tty else 20)
                for key, _ in events:
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.write(chunk)
                    output.flush()
                    pending.extend(chunk)
                    while True:
                        newline = min((index for marker in (b'\n', b'\r')
                                       if (index := pending.find(marker)) >= 0),
                                      default=-1)
                        if newline < 0:
                            break
                        reporter.output_line(bytes(pending[:newline]))
                        del pending[:newline + 1]
                now = time.monotonic()
                interval = 1 if reporter.tty else 20
                if now - last_progress >= interval and process.poll() is None:
                    elapsed = int(time.monotonic() - started)
                    reporter.progress(elapsed)
                    last_progress = now
            if pending:
                reporter.output_line(bytes(pending))
            code = process.wait()
        except BaseException:
            reporter.finish('')
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
        finally:
            selector.close()
            process.stdout.close()
        if code:
            reporter.event(f'{label} failed; see the setup log.', failed=True,
                           cause=safe_failure_summary(log), log=log)
            reporter.finish('')
            raise RuntimeError(f'{label} failed; see the setup log. Reopen Start.command to retry.')
    reporter.finish('Complete')


class Steps:
    """Record successes only; validate input and output bytes before reusing work."""
    def __init__(self):
        self.path = ROOT / 'runtime/standalone/start-state.json'
        try:
            self.state = json.loads(self.path.read_text())
            if not isinstance(self.state, dict):
                self.state = {}
        except (OSError, ValueError):
            self.state = {}
        self.previous = ''

    def step(self, name, label, commands, inputs, outputs):
        signature = hashlib.sha256(json.dumps({
            'previous': self.previous, 'commands': commands,
            'inputs': {p: digest(ROOT / p) for p in sorted(inputs)},
        }, sort_keys=True).encode()).hexdigest()
        receipt = self.state.get(name, {})
        if isinstance(receipt, dict) and receipt.get('signature') == signature:
            try:
                current = {p: digest(ROOT / p) for p in outputs}
                if current == receipt.get('outputs'):
                    reporter = setup_reporter()
                    if reporter.tty:
                        reporter.begin(label)
                        reporter.update_stage('Already done')
                    else:
                        print(label + ' — already done.', flush=True)
                    self.previous = signature
                    return
            except OSError:
                pass
        # Invalidate downstream receipts before changing any files. A failed
        # rebuild must never make an older downstream success reusable.
        for downstream in STAGE_NAMES[STAGE_NAMES.index(name):]:
            self.state.pop(downstream, None)
        self.save()
        for i, command in enumerate(commands):
            run(command, label, ROOT / 'logs/standalone/setup' / f'{name}-{i + 1}.log')
        self.state[name] = {'signature': signature,
                            'outputs': {p: digest(ROOT / p) for p in outputs}}
        self.save()
        self.previous = signature

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            # Create exclusively in the destination directory. A predictable
            # .tmp path can be a stale link to unrelated recovery data.
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                    prefix='.start-state-', suffix='.tmp',
                    dir=self.path.parent, delete=False) as output:
                temporary = Path(output.name)
                json.dump(self.state, output, indent=2)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def setup(jobs, market):
    dependencies_builder = module('build-native-dependencies')
    dependencies_builder.require_build_path(ROOT, 'Source checkout')
    if os.environ.get('STANDALONE_DEPENDENCY_PREFIX'):
        dependencies_builder.require_build_path(
            Path(os.environ['STANDALONE_DEPENDENCY_PREFIX']).expanduser(), 'Dependency prefix')
    proprietary_consent().require_acceptance(ROOT)
    run(['/bin/bash', str(ROOT / 'tools/install-build-tools.sh')], 'Preparing build tools')
    # Start.command deliberately bootstraps with Apple's Python. Use the modern
    # source-build interpreter installed above for extraction and subprocesses.
    python = '/opt/homebrew/opt/python@3.13/bin/python3.13'
    run([python, '-c', 'import sys; assert sys.version_info >= (3, 12)'], 'Checking source-build Python')
    os.environ['PATH'] = '/opt/homebrew/opt/python@3.13/libexec/bin:' + os.environ.get('PATH', '')
    dependencies = native_dependencies(python, jobs)
    bootstrap = module('bootstrap-sources')
    for name, project in bootstrap.PROJECTS.items():
        if (ROOT / 'sources' / name).exists():
            if name == 'xodus':
                # These examples are generated copies of this checkout's
                # authoritative helper inputs. Refresh them before verifying a
                # previously prepared tree after a source update.
                bootstrap.copy_helper_inputs(ROOT / 'sources' / name, ROOT)
            bootstrap.verify_project(ROOT / 'sources' / name, name, project)
        else:
            bootstrap.bootstrap(ROOT / 'sources', (name,))
    steps = Steps()
    wine = ROOT / 'runtime/standalone/wine'
    wine_inputs = ['tools/build-standalone-wine.sh', 'tools/verify-wine-source.py', 'tools/bootstrap-sources.py',
                   'tools/build-native-dependencies.py', 'config/native-dependencies.lock.json',
                   str(dependencies / 'dependency-manifest.json')]
    wine_inputs += [str(p.relative_to(ROOT)) for p in (ROOT / 'patches').glob('*wine*.patch')]
    wine_inputs += ['patches/minecraft-store-callback.patch']
    wine_inputs += list(module('standalone-pickers').SOURCE_INPUTS)
    steps.step('wine', 'Building the Windows compatibility runtime',
        [['/usr/bin/env', 'STANDALONE_DEPENDENCY_PREFIX=' + str(dependencies),
          '/bin/sh', str(ROOT / 'tools/build-standalone-wine.sh')]], wine_inputs,
        ['runtime/standalone/wine/bin/wine', 'runtime/standalone/wine/build-manifest.json',
         'runtime/standalone/wine/lib/wine/x86_64-unix/ntdll.so',
         'runtime/standalone/wine/lib/wine/x86_64-windows/xgameruntime.dll',
         'runtime/standalone/wine/lib/wine/x86_64-windows/bedrock-pickers.dll',
         'runtime/standalone/wine/file-pickers-build.json'])
    graphics = [f'x86_64-windows/{name}.dll' for name in ('d3d11', 'dxgi', 'd3d10core', 'winemetal')]
    steps.step('graphics', 'Installing graphics support',
        [['/bin/sh', str(ROOT / 'tools/build-standalone-graphics.sh'), '--wine-runtime', str(wine)]],
        ['tools/build-standalone-graphics.sh'],
        ['runtime/standalone/dxmt/' + p for p in graphics + ['x86_64-unix/winemetal.so']]
        + ['runtime/standalone/wine/lib/wine/' + p for p in graphics])
    helper_inputs = ['client/standalone-setup.py', 'tools/bootstrap-sources.py', 'patches/Cargo.lock',
                     *bootstrap.HELPER_INPUTS]
    helper_inputs += ['patches/' + name for name in bootstrap.PROJECTS['xodus']['patches']]
    steps.step('helper', 'Building Microsoft sign-in',
        [[python, str(ROOT / 'client/standalone-setup.py'), '--build-helper', '--jobs', str(jobs)]],
        helper_inputs,
        ['sources/xodus/target/release/examples/standalone_helper'])
    probes = ['standalone-graphics-probe', 'standalone-graphics-shader-probe',
              'runtime-auth-probe', 'store-callback-probe', 'runtime-privilege-probe']
    steps.step('probes', 'Building graphics checks',
        [['/bin/sh', str(ROOT / 'tools/build-standalone-probes.sh')]],
        ['tools/build-standalone-probes.sh', 'tests/xuser-claims.c'] + ['native/probes/' + p + '.c' for p in probes],
        ['runtime/standalone/probes/' + p + '.exe' for p in probes + ['xuser-claims-test']])
    bridge = ['/bin/sh', str(ROOT / 'tools/build-standalone-dxmt-unix.sh')]
    steps.step('bridge', 'Building and testing the graphics memory fixes',
        [bridge, bridge + ['--validate'], bridge + ['--stage-existing-runtime', str(wine)]],
        ['tools/build-standalone-dxmt-unix.sh', 'tools/standalone-dxmt-unix.py',
         'native/graphics/standalone-dxmt-display-trace.c', 'native/probes/standalone-dxmt-display-probe.m',
         'patches/standalone-dxmt-memory-lifetime.patch'],
        ['runtime/standalone/wine/dxmt-unix-bridge.json',
         'runtime/standalone/wine/lib/wine/x86_64-unix/winemetal.so',
         'runtime/standalone/wine/lib/wine/x86_64-unix/winemetal-upstream.so'])
    run([python, str(ROOT / 'client/standalone-setup.py'), '--threading'], 'Installing Microsoft game support')
    run([python, str(ROOT / 'client/standalone-setup.py'), '--download-game', '--market', market],
        'Downloading Minecraft')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Inspect readiness without installing, signing in, or launching')
    parser.add_argument('--setup-only', action='store_true', help='Complete setup without starting Minecraft')
    parser.add_argument('--fun-facts', action='store_true', help='Show occasional short setup facts')
    parser.add_argument('--market', default='CA', help='Two-letter Store country code (default CA)')
    parser.add_argument('--jobs', type=int, default=2, help='Build workers (default 2, suitable for initial M1 Air testing)')
    args = parser.parse_args(argv)
    global REPORTER
    REPORTER = SetupReporter(fun_facts=args.fun_facts)
    reporter = REPORTER
    if len(args.market) != 2 or not args.market.isascii() or not args.market.isupper() or not args.market.isalpha():
        parser.error('--market must be a two-letter uppercase country code')
    if not 1 <= args.jobs <= 12:
        parser.error('--jobs must be between 1 and 12')
    # Source setup owns this checkout's locks, runtime, game and saves. Do not
    # let inherited packaged-worker variables redirect only part of the flow.
    os.environ['BEDROCK_HOME'] = str(ROOT)
    os.environ.pop('BEDROCK_BUNDLE', None)
    if args.check:
        accepted, reason = module('proprietary_consent').acceptance_status(ROOT)
        if not accepted:
            print('Proprietary components are disabled: ' + reason + '. Review EULA.md and enable its acceptance setting.')
            return 1
        ready = installed()
        print('Ready to launch.' if ready else 'Setup needed. Open Start.command to continue.')
        return 0 if ready else 1
    if sys.platform != 'darwin' or platform.machine() != 'arm64' or int(platform.mac_ver()[0].split('.')[0]) < 15:
        raise RuntimeError('Open Start.command on an Apple Silicon Mac running macOS 15 or later.')
    proprietary_consent().require_acceptance(ROOT)
    os.umask(0o077)
    os.environ['STANDALONE_JOBS'] = str(args.jobs)
    os.environ['PATH'] = ':'.join([str(Path(os.environ.get('CARGO_HOME', str(Path.home() / '.cargo'))) / 'bin'),
        '/opt/homebrew/opt/rustup/bin', '/opt/homebrew/bin', '/opt/homebrew/opt/llvm/bin',
        '/opt/homebrew/opt/bison/bin', os.environ.get('PATH', '')])
    with lock('start.lock'):
        launcher = module('standalone')
        if launcher.game_running():
            print('Minecraft is already running.')
            return 0
        if not installed():
            reporter.intro(ROOT / 'logs/standalone/setup')
            with lock('launcher.lock'):
                launcher.wait_for_idle_prefix()
                setup(args.jobs, args.market)
            if not installed():
                raise RuntimeError('Installation verification failed. Open Start.command again to retry.')
        if args.setup_only:
            run([sys.executable, str(ROOT / 'client/standalone.py'), 'prepare'], 'Preparing Minecraft')
            print('Ready. Open Start.command when you want to play.')
            return 0
        run([sys.executable, str(ROOT / 'client/bedrock-monitor.py'), 'play', '--', '--market', args.market], 'Starting Minecraft')
    return 0


def interrupted(_signal, _frame):
    # Unwind run() so its separately owned compiler group cannot outlive Start
    # when Terminal closes or the worker receives a termination request.
    raise KeyboardInterrupt


def cli():
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        return main()
    except KeyboardInterrupt:
        reporter = setup_reporter()
        message = 'Stopped. Open Start.command again to retry.'
        if reporter.tty and reporter.panel_rows:
            reporter.event(message)
        else:
            print('\n' + message, file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        reporter = setup_reporter()
        message = 'Start: ' + str(error)
        if not reporter.failure_reported:
            if reporter.tty and reporter.panel_rows:
                reporter.event(message, failed=True)
            else:
                print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(cli())
