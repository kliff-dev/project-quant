"""Offline wrapper checks; underlying native exit verification has broker-fake tests."""

from contextlib import nullcontext
import json
from types import SimpleNamespace as Record
from unittest.mock import Mock

import pytest

from test_monday_session import pilot
from engine import monday_session as monday


def prior(pilot, **changes):
    state = dict(status='open_verified', protection={'stop_loss': 1.09, 'take_profit': 1.12},
                 symbol=monday.SYMBOL, account_identity=dict(login=monday.IDENTITY[0], server=monday.IDENTITY[1]),
                 account={'broker': monday.IDENTITY[2]})
    state.update(changes)
    path = pilot.root / 'logs/sessions/previous/demo_test_state.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(state), encoding='utf-8')
    return path


def test_no_prior_state_means_no_connection(pilot):
    result = monday.reconcile_prior_execution(pilot.root, connector_factory=pilot.factory)
    assert result['status'] == 'nothing_to_reconcile' and result['orders_sent'] == 0
    pilot.factory.assert_not_called()


@pytest.mark.parametrize('changes', [dict(status='open_unverified'), dict(protection=None),
    dict(close_result={'order': 1}), dict(symbol='XAUUSDm'), dict(account_identity={'login': 1}),
    dict(account={'broker': 'Other broker'})])
def test_uncertain_or_unrelated_attempt_is_not_reset(pilot, changes):
    path = prior(pilot, **changes)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='only accepts a verified protected entry'):
        monday.reconcile_prior_execution(pilot.root, connector_factory=pilot.factory)
    assert path.read_bytes() == before
    pilot.factory.assert_not_called()


def test_reconciliation_calls_only_actual_exit_verifier_and_preserves_no_send_scope(pilot):
    path = prior(pilot)
    executor = Record(state_path=path, _lock=lambda: nullcontext())
    def verify(*, closing):
        assert closing is True
        result = dict(status='closed_verified', position_ticket=123, realized_net_pnl=4.1,
                      automatic_exit={'verified_from_actual_history': True}, close_verified_deals=[{'ticket': 9}])
        path.write_text(json.dumps(result), encoding='utf-8')
        return result
    executor.verify = Mock(side_effect=verify)
    factory = Mock(return_value=executor)
    result = monday.reconcile_prior_execution(pilot.root, connector_factory=pilot.factory, executor_factory=factory)
    assert result['status'] == 'reconciled' and result['orders_sent'] == 0
    executor.verify.assert_called_once_with(closing=True)
    factory.assert_called_once_with(pilot.connector, pilot.root / 'logs', session_id='previous')
    pilot.auth_factory.create.assert_not_called()
    pilot.monitor_factory.assert_not_called()
    assert not (pilot.root / monday.RESERVATION).exists()


def test_still_open_or_missing_exit_deals_cannot_unblock_startup(pilot):
    path = prior(pilot)
    executor = Record(state_path=path, _lock=lambda: nullcontext(),
                      verify=Mock(return_value={'status': 'open_verified'}))
    with pytest.raises(ValueError, match='exit/deal evidence is missing'):
        monday.reconcile_prior_execution(pilot.root, connector_factory=pilot.factory,
                                         executor_factory=Mock(return_value=executor))
    assert json.loads(path.read_text())['status'] == 'open_verified'
    pilot.auth_factory.create.assert_not_called()


def test_reconciliation_rejects_real_account(pilot):
    prior(pilot)
    pilot.account.trade_mode = 2
    factory = Mock()
    with pytest.raises(ValueError, match='Fresh native'):
        monday.reconcile_prior_execution(pilot.root, connector_factory=pilot.factory, executor_factory=factory)
    factory.assert_not_called()
