"""Fri's own info lines reach the container log, without every library's chatter or a secret."""

import os
import subprocess
import sys

SCRIPT = '''
import logging
from fri.logging import configure, install_secret_filter
install_secret_filter()
configure()
logging.getLogger('fri').info('Member profiles: 2 of 3 configured channels are readable by everyone')
logging.getLogger('fri.agent').info('Mention 1/2 not answered, told why: stale')
logging.getLogger('discord.gateway').info('Shard ID None has connected to Gateway')
logging.getLogger('httpx').info('HTTP Request: GET http://controller:8000/agent/state "HTTP/1.1 200 OK"')
logging.getLogger('httpx').warning('connection pool is full')
logging.getLogger('fri.watch').warning('token fri-secret-value-000000 was refused')
'''


def test_fri_info_lines_reach_the_log_and_other_libraries_stay_at_warnings():
    result = subprocess.run([sys.executable, '-c', SCRIPT], capture_output=True, text=True, timeout=60,
                            env={**os.environ, 'GENE_FRI_TOKEN': 'fri-secret-value-000000'})
    assert result.returncode == 0, result.stderr
    log = result.stderr
    assert 'Member profiles: 2 of 3 configured channels' in log
    assert 'fri.agent: Mention 1/2 not answered' in log
    assert 'Shard ID None has connected' in log
    assert 'HTTP Request' not in log and 'connection pool is full' in log
    assert 'fri-secret-value' not in log and '[REDACTED]' in log
    assert log.count('Member profiles') == 1  # One handler, so nothing is printed twice.
