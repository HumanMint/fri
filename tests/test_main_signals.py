"""The production entrypoint must unwind both calls and ordinary Fri on Docker stop."""

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fri import fri as runtime


@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize('calling', [False, True])
def test_main_signal_finishes_call_and_chat_cleanup(tmp_path, signum, calling):
    script = '''
import asyncio
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
import discord
from fri import fri as runtime
marker = Path(sys.argv[1])
def record(name):
    with marker.open('a') as f:
        f.write(name + '\\n')
async def closed(name):
    await asyncio.sleep(.02)
    record(name)
class Runner(runtime.Fri):
    def __init__(self):
        discord.Client.__init__(self, intents=discord.Intents.none())
        self.jpcall_calls = SimpleNamespace(close=lambda: closed('calls'), flush_events=lambda: closed('reports'))
        self.jpcall_engine = SimpleNamespace(close=lambda: closed('engine'))
        if sys.argv[2] == 'False':
            self.jpcall_calls = self.jpcall_engine = None
        self.agent = SimpleNamespace(close=lambda: record('chat'))
        self.health = self.owner_reviews = SimpleNamespace(cancel=lambda: None)
        self.watching = self.sign_in_notice = None
        self.session = SimpleNamespace(close=lambda: closed('http'))
    async def start(self, *args, **kwargs):
        marker.with_suffix('.ready').touch()
        await asyncio.Event().wait()
runtime.Fri = Runner
runtime.sessions = lambda client: SimpleNamespace(close_all=lambda: closed('panels'))
previous = signal.getsignal(signal.SIGTERM)
runtime.main()
assert signal.getsignal(signal.SIGTERM) is previous
record('handler restored')
'''
    marker = tmp_path / 'shutdown.txt'
    env = {'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
           'PYTHONPATH': str(Path(runtime.__file__).resolve().parents[1]),
           'FRI_DATA': str(tmp_path / 'data'), 'DISCORD_TOKEN': 'synthetic-unused'}
    process = subprocess.Popen([sys.executable, '-B', '-c', script, str(marker), str(calling)], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not marker.with_suffix('.ready').exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                pytest.fail(f'main runner did not start: {process.communicate(timeout=1)}')
            time.sleep(.01)
        process.send_signal(signum)
        output = process.communicate(timeout=5)
        assert process.returncode == 0, output
        assert marker.read_text().splitlines() == (
            (['calls', 'engine', 'reports'] if calling else [])
            + ['chat', 'panels', 'http', 'handler restored'])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_main_restores_signal_handler_when_startup_fails(monkeypatch, tmp_path):
    monkeypatch.setenv('FRI_DATA', str(tmp_path))
    monkeypatch.setenv('DISCORD_TOKEN', 'synthetic-unused')

    def fail():
        raise RuntimeError('synthetic startup failure')

    monkeypatch.setattr(runtime, 'Fri', fail)
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(RuntimeError, match='synthetic startup failure'):
        runtime.main()
    assert signal.getsignal(signal.SIGTERM) is previous
