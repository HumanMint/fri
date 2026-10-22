import builtins
import importlib
import pkgutil

import pytest

from fri.fri_instance import instance_lock
from fri.fri_protocol import compatible, contract


def test_every_runtime_module_imports_without_gene_or_red(monkeypatch):
    import fri
    original = builtins.__import__
    def isolated(name, *args, **kwargs):
        assert name.split('.')[0] not in {'gene', 'redbot'}, name
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', isolated)
    for module in pkgutil.iter_modules(fri.__path__):
        importlib.import_module('fri.'+module.name)


def test_distinct_failure_categories_preserved():
    from fri.feedback import UserFeedbackCheckFailure
    from fri.fri_errors import ControllerRejected
    assert issubclass(ControllerRejected, UserFeedbackCheckFailure)
    assert not isinstance(UserFeedbackCheckFailure('uncertain'), ControllerRejected)


@pytest.mark.parametrize('value', [None, {}, {'protocol':'gene-fri/2'},
    {**contract(), 'capabilities': []}, {**contract(), 'deployment_authority':'fri'}])
def test_unknown_or_incomplete_contract_is_rejected(value):
    assert not compatible(value)
    assert compatible(contract())


def test_runtime_lock_rejects_duplicate_and_releases_after_failure(tmp_path):
    with pytest.raises(RuntimeError, match='simulated failure'):
        with instance_lock(tmp_path):
            with pytest.raises(RuntimeError, match='another fri'):
                with instance_lock(tmp_path):
                    pytest.fail('duplicate acquired lock')
            raise RuntimeError('simulated failure')
    with instance_lock(tmp_path):
        assert (tmp_path/'fri-runtime.lock').stat().st_mode & 0o777 == 0o600


def test_no_state_deployment_or_shell_modules_packaged():
    import fri
    names={item.name for item in pkgutil.iter_modules(fri.__path__)}
    assert not names & {'store','worker','deploy','client','policy','repair_policy'}
    assert not any(name.endswith('_store') for name in names)
