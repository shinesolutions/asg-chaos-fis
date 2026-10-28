# -*- coding: utf8 -*-

"""Unit tests for the AEM Chaos Engine Lambda. Test names describe the behaviour under test."""

# pylint: disable=missing-function-docstring,too-many-arguments,too-many-positional-arguments

import json
import random
from collections import Counter
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import aem_chaos_engine as engine
from conftest import (
    GRACE_PERIOD, LONG_AGO, NOW, SCHEDULED_TIME, STACK_PREFIX, STATE_PARAMETER_PATH, STOP_CONDITION_ALARMS,
    TEMPLATE_IDS, TODAY, asg_name, choice_value, make_config,
)

EVENT = {'scheduledTime': SCHEDULED_TIME, 'executionId': 'execution-1'}
CONTEXT = SimpleNamespace(aws_request_id='request-1')
HIT = 0.01


def run():
    return engine.handler(EVENT, CONTEXT)


# --- Configuration -----------------------------------------------------------

def test_missing_environment_variables_raise(monkeypatch):
    monkeypatch.delenv('STATE_PARAMETER_PATH')
    with pytest.raises(engine.ConfigurationError, match='STATE_PARAMETER_PATH'):
        run()


def test_missing_config_parameter_raises(aws):
    aws.ssm.add_client_error('get_parameter', 'ParameterNotFound')
    with pytest.raises(engine.ConfigurationError, match='does not exist'):
        run()


def test_config_that_is_not_json_raises(aws):
    aws.ssm.add_response('get_parameter', {'Parameter': {'Name': 'x', 'Type': 'String', 'Value': 'not json'}})
    with pytest.raises(engine.ConfigurationError, match='not valid JSON'):
        run()


def test_valid_config_defaults():
    config = make_config()
    del config['dryRun'], config['retentionDays']
    parsed = engine.parse_config(config)
    assert parsed.dry_run is False
    assert parsed.retention_days == 7
    assert [t.min_in_service for t in parsed.targets] == [2, 2, 2]
    assert [t.weight for t in parsed.targets] == [1.0, 1.0, 1.0]


def test_author_dispatcher_alone_needs_no_orchestrator():
    config = make_config(targets=make_config()['targets'][:1])
    del config['orchestratorAsgName']
    parsed = engine.parse_config(config)
    assert parsed.orchestrator_asg_name is None
    assert [t.component for t in parsed.targets] == ['author-dispatcher']


@pytest.mark.parametrize('component', ['publish', 'publish-dispatcher'])
def test_paired_target_needs_its_partner_listed(component):
    partner = engine.PAIRED_COMPONENTS[component]
    config = make_config(targets=[t for t in make_config()['targets'] if t['component'] != partner])
    with pytest.raises(engine.ConfigurationError, match='the orchestrator also terminates the paired instance'):
        engine.parse_config(config)


def test_paired_target_needs_the_orchestrator():
    config = make_config()
    del config['orchestratorAsgName']
    with pytest.raises(engine.ConfigurationError, match="'orchestratorAsgName'"):
        engine.parse_config(config)


def test_disabled_partner_is_enough_and_its_asg_is_known():
    config = make_config()
    config['targets'][2]['enabled'] = False
    parsed = engine.parse_config(config)
    assert parsed.asg_name('publish-dispatcher') == asg_name('publish-dispatcher')


@pytest.mark.parametrize('overrides, message', [
    ({'enabled': 'true'}, "'enabled' must be a bool"),
    ({'timezone': 'Mars/Olympus_Mons'}, 'unknown timezone'),
    ({'runsPerDay': 0}, "'runsPerDay' must be at least 1"),
    ({'dailyProbability': 7.0}, 'between 0 and runsPerDay'),
    ({'dailyProbability': -0.1}, 'between 0 and runsPerDay'),
    ({'retentionDays': 0}, "'retentionDays' must be at least 1"),
    ({'cloudFormationStackName': ''}, "'cloudFormationStackName' must be a str"),
    ({'stopConditionAlarmNames': 'alarm'}, "'stopConditionAlarmNames' must be a list"),
    ({'stopConditionAlarmNames': ['alarm', '']}, "'stopConditionAlarmNames' must be a list"),
    ({'stopConditionAlarmNames': [f'alarm-{n}' for n in range(101)]}, "up to 100"),
    ({'targets': []}, 'must contain an enabled target'),
    ({'targets': [{'component': 'publish', 'asgName': 'a', 'fisTemplateId': 't', 'enabled': False}]},
     'must contain an enabled target'),
    ({'targets': [{'component': 'publish', 'asgName': 'a', 'fisTemplateId': 't', 'enabled': 'yes'}]},
     "'targets\\[0\\].enabled' must be a bool"),
    ({'targets': 'publish'}, 'must be a list'),
    ({'targets': [{'component': 'author-primary', 'asgName': 'a', 'fisTemplateId': 't'}]}, 'only author-dispatcher'),
    ({'targets': [{'component': 'orchestrator', 'asgName': 'a', 'fisTemplateId': 't'}]}, 'only author-dispatcher'),
    ({'targets': [{'component': 'publish', 'fisTemplateId': 't'}]}, "'targets\\[0\\].asgName' is required"),
    ({'targets': [{'component': 'publish', 'asgName': 'a', 'fisTemplateId': 't', 'weight': 0}]}, 'weight'),
    ({'targets': [{'component': 'publish', 'asgName': 'a', 'fisTemplateId': 't', 'minInService': 0}]},
     'minInService'),
    ({'targets': [{'component': 'publish', 'asgName': 'a', 'fisTemplateId': 't'}] * 2}, 'same component twice'),
])
def test_invalid_config_is_rejected(overrides, message):
    with pytest.raises(engine.ConfigurationError, match=message):
        engine.parse_config(make_config(**overrides))


def test_sample_config_is_valid():
    with open(Path(__file__).parent.parent / 'conf' / 'sample-config.json', encoding='utf-8') as sample:
        config = engine.parse_config(json.load(sample))
    assert {target.component for target in config.targets} == engine.ALLOWED_COMPONENTS


def test_disabled_targets_are_ignored():
    config = make_config()
    config['targets'][1]['enabled'] = False
    assert [t.component for t in engine.parse_config(config).targets] == ['author-dispatcher', 'publish-dispatcher']


def test_disabled_target_is_still_validated():
    config = make_config()
    config['targets'][1].update(enabled=False, component='author-primary')
    with pytest.raises(engine.ConfigurationError, match='only author-dispatcher'):
        engine.parse_config(config)


def test_disabled_engine_may_have_no_targets():
    assert not engine.parse_config(make_config(enabled=False, targets=[])).targets


@pytest.mark.parametrize('daily, runs, expected', [
    (1.0, 6, 1 / 6),
    (3.0, 6, 0.5),
    (6.0, 6, 1.0),
    (0.0, 6, 0.0),
])
def test_run_probability_spreads_daily_probability_over_runs(daily, runs, expected):
    config = engine.parse_config(make_config(dailyProbability=daily, runsPerDay=runs))
    assert config.run_probability == pytest.approx(expected)


# --- Decisions before any termination -----------------------------------------

def test_disabled_engine_skips(aws):
    aws.config(make_config(enabled=False))
    assert run()['reason'] == 'ENGINE_DISABLED'


def test_skips_when_today_already_claimed(aws, rng):
    rng(HIT)
    aws.config(make_config())
    aws.claims([TODAY])
    result = run()
    assert result['status'] == 'SKIPPED'
    assert result['reason'] == 'DAILY_LIMIT_REACHED'


def test_claim_from_yesterday_does_not_block_today(aws, rng):
    rng(0.99)
    aws.config(make_config())
    aws.claims(['2026-10-13'])
    assert run()['reason'] == 'PROBABILITY_MISSED'


def test_expired_claims_are_pruned(aws, rng):
    rng(0.99)
    aws.config(make_config(retentionDays=7))
    aws.claims(['2026-10-01', '2026-10-06', '2026-10-07', '2026-10-13', 'not-a-date'])
    aws.ssm.add_response(
        'delete_parameters',
        {'DeletedParameters': [f'{STATE_PARAMETER_PATH}/2026-10-01', f'{STATE_PARAMETER_PATH}/2026-10-06']},
        {'Names': [f'{STATE_PARAMETER_PATH}/2026-10-01', f'{STATE_PARAMETER_PATH}/2026-10-06']},
    )
    assert run()['reason'] == 'PROBABILITY_MISSED'


def test_prune_failure_does_not_stop_the_run(aws, rng):
    rng(0.99)
    aws.config(make_config())
    aws.claims(['2026-09-01'])
    aws.ssm.add_client_error('delete_parameters', 'ThrottlingException')
    assert run()['reason'] == 'PROBABILITY_MISSED'


def test_probability_miss_skips(aws, rng):
    rng(1 / 6)  # equal to the threshold for dailyProbability 1.0 over 6 runs: a miss
    aws.config(make_config())
    aws.claims([])
    result = run()
    assert result['reason'] == 'PROBABILITY_MISSED'
    assert result['threshold'] == pytest.approx(1 / 6, abs=1e-4)


def test_choose_target_respects_weights(monkeypatch):
    monkeypatch.setattr(engine, '_rng', random.Random(42))
    config = engine.parse_config(make_config(targets=[
        {'component': 'publish', 'asgName': 'a', 'fisTemplateId': 't1', 'weight': 1},
        {'component': 'publish-dispatcher', 'asgName': 'b', 'fisTemplateId': 't2', 'weight': 3},
    ]))
    counts = Counter(engine.choose_target(config.targets).component for _ in range(10000))
    assert counts['publish-dispatcher'] / 10000 == pytest.approx(0.75, abs=0.02)


# --- Pre-checks -----------------------------------------------------------------

def test_skips_when_stack_is_updating(aws, rng):
    rng(HIT, choice_value('publish-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.stack('UPDATE_IN_PROGRESS')
    result = run()
    assert result['reason'] == 'PRECHECK_STACK_NOT_STABLE'
    assert result['component'] == 'publish-dispatcher'


def test_skips_when_stack_does_not_exist(aws, rng):
    rng(HIT, choice_value('publish-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.cloudformation.add_client_error('describe_stacks', 'ValidationError')
    assert run()['reason'] == 'PRECHECK_STACK_NOT_FOUND'


@pytest.mark.parametrize('desired, healthy, unhealthy, reason', [
    (2, 1, 1, 'PRECHECK_TARGET_ASG_DEGRADED'),
    (3, 2, 0, 'PRECHECK_TARGET_ASG_DEGRADED'),
    (1, 1, 0, 'PRECHECK_TARGET_ASG_BELOW_MIN_IN_SERVICE'),
])
def test_skips_when_target_asg_is_not_fully_healthy(aws, rng, desired, healthy, unhealthy, reason):
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.asg(asg_name('author-dispatcher'), desired=desired, healthy=healthy, unhealthy=unhealthy)
    assert run()['reason'] == reason


def test_skips_when_target_asg_is_scaling(aws, rng):
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.asg(asg_name('author-dispatcher'))
    aws.ec2_instances()
    aws.fis_targets('author-dispatcher')
    aws.activities(asg_name('author-dispatcher'), ['Successful', 'InProgress'])
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_SCALING_IN_PROGRESS'


@pytest.mark.parametrize('lifecycle', ['Standby', 'Pending', 'Terminating', 'Detaching', 'EnteringStandby'])
def test_skips_when_asg_has_an_instance_that_is_not_in_service(aws, rng, lifecycle):
    # E.g. Standby with the desired capacity decremented: 2 healthy of 2 desired, but FIS could pick the third.
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.asg(asg_name('author-dispatcher'), desired=2, healthy=2, other_states=[lifecycle])
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_INSTANCE_NOT_IN_SERVICE'


def test_skips_when_a_running_instance_with_the_target_tags_is_outside_the_asg(aws, rng):
    # E.g. an instance detached from the ASG but still running: FIS selects by tag, so it could pick it.
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.asg(asg_name('author-dispatcher'))
    aws.ec2_instances()
    aws.fis_targets('author-dispatcher', ids=['i-00000000000000000', 'i-00000000000000001', 'i-0detached00000000'])
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_UNMANAGED_INSTANCE'


@pytest.mark.parametrize('state', ['shutting-down', 'terminated', 'stopped'])
def test_skips_when_asg_still_counts_an_instance_that_is_not_running(aws, rng, state):
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.asg(asg_name('author-dispatcher'))
    aws.ec2_instances(states=['running', state])
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_INSTANCE_NOT_RUNNING'


def test_skips_when_asg_instance_no_longer_exists_in_ec2(aws, rng):
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.asg(asg_name('author-dispatcher'))
    aws.ec2.add_client_error('describe_instances', 'InvalidInstanceID.NotFound')
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_INSTANCE_NOT_RUNNING'


def test_skips_while_an_instance_is_in_the_health_check_grace_period(aws, rng):
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.asg(asg_name('author-dispatcher'))
    aws.ec2_instances(launched=[LONG_AGO, NOW - timedelta(seconds=GRACE_PERIOD - 1)])
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_IN_GRACE_PERIOD'


def test_instance_just_past_the_grace_period_is_settled(aws, rng):
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config(dryRun=True))
    aws.claims([])
    aws.stack()
    aws.asg(asg_name('author-dispatcher'))
    aws.ec2_instances(launched=[LONG_AGO, NOW - timedelta(seconds=GRACE_PERIOD)])
    aws.fis_targets('author-dispatcher')
    aws.activities(asg_name('author-dispatcher'))
    aws.alarms()
    aws.experiments()
    assert run()['status'] == 'DRY_RUN'


def test_author_dispatcher_does_not_check_the_publish_pair_or_orchestrator(aws, rng):
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config(dryRun=True))
    aws.claims([])
    aws.passing_prechecks('author-dispatcher')
    assert run()['status'] == 'DRY_RUN'


@pytest.mark.parametrize('component, partner', [('publish', 'publish-dispatcher'), ('publish-dispatcher', 'publish')])
def test_skips_when_the_paired_asg_is_not_healthy(aws, rng, component, partner):
    # The orchestrator terminates the paired instance too, so the pair's other ASG must be healthy as well.
    rng(HIT, choice_value(component))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.healthy_asg(asg_name(component), component)
    aws.asg(asg_name(partner), desired=2, healthy=1, unhealthy=1)
    assert run()['reason'] == 'PRECHECK_PAIRED_ASG_DEGRADED'


def test_paired_asg_needs_min_in_service_too(aws, rng):
    rng(HIT, choice_value('publish-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.healthy_asg(asg_name('publish-dispatcher'), 'publish-dispatcher')
    aws.asg(asg_name('publish'), desired=1, healthy=1)
    assert run()['reason'] == 'PRECHECK_PAIRED_ASG_BELOW_MIN_IN_SERVICE'


@pytest.mark.parametrize('component', ['publish', 'publish-dispatcher'])
def test_publish_and_publish_dispatcher_require_a_healthy_orchestrator(aws, rng, component):
    rng(HIT, choice_value(component))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.healthy_asg(asg_name(component), component)
    aws.healthy_asg(asg_name(engine.PAIRED_COMPONENTS[component]))
    aws.asg(asg_name('orchestrator'), desired=1, healthy=0, unhealthy=1)
    assert run()['reason'] == 'PRECHECK_ORCHESTRATOR_ASG_DEGRADED'


def test_new_orchestrator_blocks_publish_termination(aws, rng):
    rng(HIT, choice_value('publish'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.healthy_asg(asg_name('publish'), 'publish')
    aws.healthy_asg(asg_name('publish-dispatcher'))
    aws.asg(asg_name('orchestrator'))
    aws.ec2_instances(launched=[NOW - timedelta(seconds=10), LONG_AGO])
    assert run()['reason'] == 'PRECHECK_ORCHESTRATOR_ASG_IN_GRACE_PERIOD'


@pytest.mark.parametrize('state', ['ALARM', 'INSUFFICIENT_DATA'])
def test_skips_without_claiming_when_a_stop_condition_alarm_is_not_ok(aws, rng, state):
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.healthy_asg(asg_name('author-dispatcher'), 'author-dispatcher')
    aws.alarms(states=['OK', state])
    # No put_parameter is queued: the stubber fails the test if the day is claimed.
    assert run()['reason'] == 'PRECHECK_STOP_CONDITION_ALARM'


def test_skips_when_a_stop_condition_alarm_is_missing(aws, rng):
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.healthy_asg(asg_name('author-dispatcher'), 'author-dispatcher')
    aws.alarms(names=STOP_CONDITION_ALARMS[:1])
    assert run()['reason'] == 'PRECHECK_STOP_CONDITION_ALARM_NOT_FOUND'


def test_no_stop_condition_alarms_configured_skips_the_alarm_check(aws, rng):
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config(dryRun=True, stopConditionAlarmNames=[]))
    aws.claims([])
    aws.stack()
    aws.healthy_asg(asg_name('author-dispatcher'), 'author-dispatcher')
    aws.experiments()
    assert run()['status'] == 'DRY_RUN'


def test_skips_when_target_asg_is_missing(aws, rng):
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.autoscaling.add_response('describe_auto_scaling_groups', {'AutoScalingGroups': []})
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_NOT_FOUND'


@pytest.mark.parametrize('component', ['author-dispatcher', 'publish', 'publish-dispatcher'])
def test_skips_when_an_engine_experiment_is_active(aws, rng, component):
    # Experiments are looked up per template, so a busy account's other experiments are never paged through.
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.healthy_asg(asg_name('author-dispatcher'), 'author-dispatcher')
    aws.alarms()
    aws.experiments(active_in=TEMPLATE_IDS[component])
    assert run()['reason'] == 'PRECHECK_EXPERIMENT_ACTIVE'


def test_experiment_lookup_follows_pages(aws, rng):
    rng(HIT, choice_value('author-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.healthy_asg(asg_name('author-dispatcher'), 'author-dispatcher')
    aws.alarms()
    first = sorted(TEMPLATE_IDS.values())[0]
    aws.fis.add_response('list_experiments',
                         {'experiments': [{'id': 'EXPDONE', 'state': {'status': 'completed'}}], 'nextToken': 'page-2'},
                         {'experimentTemplateId': first, 'maxResults': 100})
    aws.fis.add_response('list_experiments',
                         {'experiments': [{'id': 'EXPACTIVE', 'state': {'status': 'stopping'}}]},
                         {'experimentTemplateId': first, 'maxResults': 100, 'nextToken': 'page-2'})
    assert run()['reason'] == 'PRECHECK_EXPERIMENT_ACTIVE'


# --- Terminations ---------------------------------------------------------------

def test_dry_run_does_not_claim_or_terminate(aws, rng):
    rng(HIT, choice_value('publish'))
    aws.config(make_config(dryRun=True))
    aws.claims([])
    aws.passing_prechecks('publish')
    result = run()
    assert result['status'] == 'DRY_RUN'
    assert result['reason'] == 'WOULD_TERMINATE'
    assert result['component'] == 'publish'


def test_executes_termination(aws, rng, capsys):
    rng(HIT, choice_value('publish-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.passing_prechecks('publish-dispatcher')
    aws.create_claim()
    aws.fis.add_response(
        'start_experiment',
        {'experiment': {'id': 'EXPNEW'}},
        {
            'clientToken': engine.client_token(STACK_PREFIX, SCHEDULED_TIME, CONTEXT),
            'experimentTemplateId': TEMPLATE_IDS['publish-dispatcher'],
            'tags': {
                'StackPrefix': STACK_PREFIX,
                'Component': 'publish-dispatcher',
                'TriggeredBy': 'aem-chaos-engine',
                'LocalDate': TODAY,
            },
        },
    )
    aws.update_claim()

    result = run()

    assert result['status'] == 'EXECUTED'
    assert result['experimentId'] == 'EXPNEW'
    assert result['component'] == 'publish-dispatcher'
    metric = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert metric['Status'] == 'EXECUTED'
    assert metric['_aws']['CloudWatchMetrics'][0]['Namespace'] == 'AEMOpenCloud/ChaosEngine'


def test_skips_when_another_invocation_claimed_the_day_first(aws, rng):
    rng(HIT, choice_value('publish-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.passing_prechecks('publish-dispatcher')
    aws.create_claim(error_code='ParameterAlreadyExists')
    assert run()['reason'] == 'DAILY_LIMIT_REACHED'


def test_fis_failure_keeps_claim_and_raises(aws, rng):
    rng(HIT, choice_value('publish-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.passing_prechecks('publish-dispatcher')
    aws.create_claim()
    aws.fis.add_client_error('start_experiment', 'ValidationException')
    aws.update_claim()
    with pytest.raises(ClientError):
        run()


def test_claim_update_failure_after_start_still_reports_execution(aws, rng):
    rng(HIT, choice_value('publish-dispatcher'))
    aws.config(make_config())
    aws.claims([])
    aws.passing_prechecks('publish-dispatcher')
    aws.create_claim()
    aws.fis.add_response('start_experiment', {'experiment': {'id': 'EXPNEW'}})
    aws.update_claim(error_code='ThrottlingException')
    assert run()['status'] == 'EXECUTED'


def test_client_token_is_stable_per_schedule_slot():
    first = engine.client_token(STACK_PREFIX, SCHEDULED_TIME, SimpleNamespace(aws_request_id='a'))
    retry = engine.client_token(STACK_PREFIX, SCHEDULED_TIME, SimpleNamespace(aws_request_id='b'))
    other_stack = engine.client_token('other', SCHEDULED_TIME, None)
    manual = engine.client_token(STACK_PREFIX, None, SimpleNamespace(aws_request_id='a'))
    assert first == retry
    assert first != other_stack
    assert first != manual
    assert len(first) == 64
