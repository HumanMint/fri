import os
from tempfile import TemporaryDirectory

# Match compat_tests: Red/Gene imports must never touch a real user config directory.
replay_config = TemporaryDirectory(prefix='fri-replay-config-')
os.environ['XDG_CONFIG_HOME'] = replay_config.name
