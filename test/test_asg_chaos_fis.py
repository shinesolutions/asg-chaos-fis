# -*- coding: utf8 -*-

"""Unit tests for the ASG Chaos FIS Lambda. Test names describe the behaviour under test."""

# pylint: disable=missing-function-docstring,too-many-arguments,too-many-positional-arguments

import json
import random
import re
from collections import Counter
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import asg_chaos_fis as engine
from conftest import (
    APP, CONFIG_PARAMETER_NAME, GRACE_PERIOD, LONG_AGO, NOW, SCHEDULED_TIME, STATE_PARAMETER_PATH,
    STOP_CONDITION_ALARMS, TEMPLATE_IDS, TODAY, alarm_arn, asg_name, choice_value, make_config, make_template,
    resource_tags,
)

EVENT = {'scheduledTime': SCHEDULED_TIME, 'executionId': 'execution-1'}
CONTEXT = SimpleNamespace(aws_request_id='request-1')
HIT = 0.01
SOURCE = Path(engine.__file__)


def run():
    return engine.handler(EVENT, CONTEXT)


def target(**overrides):
    return {'name': 'web', 'asgName': 'a', 'fisTemplateId': 't', **overrides}


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
    config = make_config(targets=[target()])
    for optional in ('dryRun', 'retentionDays', 'cloudFormationStackName', 'metricNamespace', 'metricDimensions',
                     'experimentTags'):
        del config[optional]
    parsed = engine.parse_config(config)
    assert parsed.dry_run is False
    assert parsed.retention_days == 7
    assert parsed.cloudformation_stack_name is None
    assert parsed.metric_namespace == 'ASGChaosFIS'
    assert not parsed.metric_dimensions
    assert not parsed.experiment_tags
    assert parsed.targets[0].min_in_service == 2
    assert parsed.targets[0].weight == 1.0
    assert parsed.targets[0].required_healthy == ()


def test_required_healthy_is_parsed_with_default_min_in_service():
    parsed = engine.parse_config(make_config(targets=[target(requiredHealthy=[
        {'asgName': 'other', 'minInService': 3}, {'asgName': 'coordinator'},
    ])]))
    assert parsed.targets[0].required_healthy == (
        engine.RequiredAsg('other', 3), engine.RequiredAsg('coordinator', 1),
    )


@pytest.mark.parametrize('version', [None, 0, 2, '1'])
def test_unsupported_schema_version_raises(version):
    config = make_config()
    if version is None:
        del config['schemaVersion']
    else:
        config['schemaVersion'] = version
    with pytest.raises(engine.ConfigurationError, match="'schemaVersion'"):
        engine.parse_config(config)


def test_unsupported_schema_version_fails_the_invocation(aws):
    # Raising, not skipping, makes a mismatch between the deployed config and the library visible as an error.
    aws.config(make_config(schemaVersion=2))
    with pytest.raises(engine.ConfigurationError, match='only supports 1'):
        run()


@pytest.mark.parametrize('overrides, message', [
    ({'enabled': 'true'}, "'enabled' must be a bool"),
    ({'timezone': 'Mars/Olympus_Mons'}, 'unknown timezone'),
    ({'runsPerDay': 0}, "'runsPerDay' must be at least 1"),
    ({'dailyProbability': 7.0}, 'between 0 and runsPerDay'),
    ({'dailyProbability': -0.1}, 'between 0 and runsPerDay'),
    ({'retentionDays': 0}, "'retentionDays' must be at least 1"),
    ({'cloudFormationStackName': ''}, "'cloudFormationStackName' must be a str"),
    ({'metricNamespace': ''}, "'metricNamespace' must be a str"),
    ({'metricDimensions': ['App']}, "'metricDimensions' must be an object"),
    ({'metricDimensions': {'App': ''}}, "'metricDimensions' must be an object"),
    ({'metricDimensions': {f'D{n}': 'x' for n in range(30)}}, "'metricDimensions' must be an object of up to 29"),
    ({'metricDimensions': {'Status': 'x'}}, "'metricDimensions' must not contain Status"),
    ({'metricDimensions': {'target': 'x'}}, "'metricDimensions' must not contain target"),
    ({'experimentTags': {'aws:App': 'x'}}, "'experimentTags' must be an object"),
    ({'experimentTags': {'App': 'x' * 257}}, "'experimentTags' must be an object"),
    ({'experimentTags': {f'T{n}': 'x' for n in range(48)}}, "'experimentTags' must be an object of up to 47"),
    ({'experimentTags': {'ChaosTarget': 'x', 'LocalDate': 'y'}},
     "'experimentTags' must not contain ChaosTarget, LocalDate"),
    ({'targets': []}, 'must contain an enabled target'),
    ({'targets': [target(enabled=False)]}, 'must contain an enabled target'),
    ({'targets': [target(enabled='yes')]}, "'targets\\[0\\].enabled' must be a bool"),
    ({'targets': 'web'}, 'must be a list'),
    ({'targets': ['web']}, "'targets\\[0\\]' must be an object"),
    ({'targets': [{'asgName': 'a', 'fisTemplateId': 't'}]}, "'targets\\[0\\].name' is required"),
    ({'targets': [target(name='x' * 257)]}, "'targets\\[0\\].name' must be at most 256"),
    ({'targets': [target(asgName=None)]}, "'targets\\[0\\].asgName' must be a str"),
    ({'targets': [{'name': 'web', 'fisTemplateId': 't'}]}, "'targets\\[0\\].asgName' is required"),
    ({'targets': [target(weight=0)]}, 'weight'),
    ({'targets': [target(minInService=0)]}, 'minInService'),
    ({'targets': [target()] * 2}, 'same name twice'),
    ({'targets': [target(requiredHealthy={'asgName': 'b'})]}, "'targets\\[0\\].requiredHealthy' must be a list"),
    ({'targets': [target(requiredHealthy=['b'])]}, "'targets\\[0\\].requiredHealthy\\[0\\]' must be an object"),
    ({'targets': [target(requiredHealthy=[{'minInService': 1}])]},
     "'targets\\[0\\].requiredHealthy\\[0\\].asgName' is required"),
    ({'targets': [target(requiredHealthy=[{'asgName': 'b', 'minInService': 0}])]},
     "'targets\\[0\\].requiredHealthy\\[0\\].minInService' must be at least 1"),
])
def test_invalid_config_is_rejected(overrides, message):
    with pytest.raises(engine.ConfigurationError, match=message):
        engine.parse_config(make_config(**overrides))


def test_any_target_name_is_accepted():
    # Which Auto Scaling Groups may be targeted is decided by the deployment, not by the library.
    parsed = engine.parse_config(make_config(targets=[target(name='author-primary')]))
    assert parsed.targets[0].name == 'author-primary'


def test_sample_config_is_valid():
    with open(Path(__file__).parent.parent / 'conf' / 'sample-config.json', encoding='utf-8') as sample:
        config = engine.parse_config(json.load(sample))
    assert config.targets


def test_disabled_targets_are_ignored():
    config = make_config()
    config['targets'][1]['enabled'] = False
    assert [t.name for t in engine.parse_config(config).targets] == ['web', 'worker']


def test_disabled_target_is_still_validated():
    config = make_config()
    config['targets'][1].update(enabled=False, minInService=0)
    with pytest.raises(engine.ConfigurationError, match='minInService'):
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


def test_library_contains_no_application_names():
    # The library only knows about Auto Scaling Groups: application knowledge belongs in the configuration.
    source = SOURCE.read_text(encoding='utf-8')
    for word in ('aem', 'publish', 'dispatcher', 'orchestrator', 'author', 'StackPrefix', 'Component'):
        assert not re.search(rf'\b{word}\b', source, re.IGNORECASE), word


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
        target(name='api', fisTemplateId='t1', weight=1),
        target(name='worker', fisTemplateId='t2', weight=3),
    ]))
    counts = Counter(engine.choose_target(config.targets).name for _ in range(10000))
    assert counts['worker'] / 10000 == pytest.approx(0.75, abs=0.02)


# --- Pre-checks -----------------------------------------------------------------

def test_skips_when_stack_is_updating(aws, rng):
    rng(HIT, choice_value('worker'))
    aws.config(make_config())
    aws.claims([])
    aws.stack('UPDATE_IN_PROGRESS')
    result = run()
    assert result['reason'] == 'PRECHECK_STACK_NOT_STABLE'
    assert result['target'] == 'worker'


def test_skips_when_stack_does_not_exist(aws, rng):
    rng(HIT, choice_value('worker'))
    aws.config(make_config())
    aws.claims([])
    aws.cloudformation.add_client_error('describe_stacks', 'ValidationError')
    assert run()['reason'] == 'PRECHECK_STACK_NOT_FOUND'


def test_stack_check_is_skipped_without_a_stack_name(aws, rng):
    rng(HIT, choice_value('web'))
    config = make_config(dryRun=True)
    del config['cloudFormationStackName']
    aws.config(config)
    aws.claims([])
    # No describe_stacks is queued: the stubber fails the test if the stack is looked up.
    aws.template('web')
    aws.healthy_asg(asg_name('web'), resource_tags('web'))
    aws.alarms()
    aws.experiments()
    assert run()['status'] == 'DRY_RUN'


def test_skips_when_the_fis_template_does_not_exist(aws, rng):
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.fis.add_client_error('get_experiment_template', 'ResourceNotFoundException')
    assert run()['reason'] == 'PRECHECK_FIS_TEMPLATE_NOT_FOUND'


@pytest.mark.parametrize('template', [
    make_template('web', resourceArns=[f'arn:aws:ec2:ap-southeast-2:123456789012:instance/i-{0:017d}']),
    make_template('web', resourceTags={}),
    make_template('web', resourceType='aws:ecs:task'),
    make_template('web', selectionMode='COUNT(2)'),
    make_template('web', selectionMode='ALL'),
    make_template('web', selectionMode='PERCENT(10)'),
    make_template('web', filters=[]),
    make_template('web', filters=[{'path': 'State.Name', 'values': ['running', 'stopped']}]),
    make_template('web', filters=[{'path': 'State.Name', 'values': ['running']},
                                  {'path': 'Placement.AvailabilityZone', 'values': ['ap-southeast-2a']}]),
    {**make_template('web'), 'targets': {}},
    {**make_template('web'), 'targets': {**make_template('web')['targets'],
                                         'More': make_template('api')['targets']['Instances']}},
    {**make_template('web'), 'stopConditions': [
        {'source': 'aws:other', 'value': 'arn:aws:other:ap-southeast-2:123456789012:thing/x'},
    ]},
], ids=['resource-arns', 'no-tags', 'not-ec2', 'count-2', 'all', 'percent', 'no-filter', 'stopped-too',
        'extra-filter', 'no-target', 'two-targets', 'other-stop-condition'])
def test_skips_without_claiming_when_the_fis_template_is_not_supported(aws, rng, template):
    # The pre-checks could not cover everything FIS might select, so the run fails closed.
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.template('web', template)
    assert run()['reason'] == 'PRECHECK_FIS_TEMPLATE_UNSUPPORTED'


@pytest.mark.parametrize('desired, healthy, unhealthy, reason', [
    (2, 1, 1, 'PRECHECK_TARGET_ASG_DEGRADED'),
    (3, 2, 0, 'PRECHECK_TARGET_ASG_DEGRADED'),
    (1, 1, 0, 'PRECHECK_TARGET_ASG_BELOW_MIN_IN_SERVICE'),
])
def test_skips_when_target_asg_is_not_fully_healthy(aws, rng, desired, healthy, unhealthy, reason):
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.template('web')
    aws.asg(asg_name('web'), desired=desired, healthy=healthy, unhealthy=unhealthy)
    assert run()['reason'] == reason


def test_skips_when_target_asg_is_scaling(aws, rng):
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.template('web')
    aws.asg(asg_name('web'))
    aws.ec2_instances()
    aws.fis_targets(resource_tags('web'))
    aws.activities(asg_name('web'), ['Successful', 'InProgress'])
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_SCALING_IN_PROGRESS'


@pytest.mark.parametrize('lifecycle', ['Standby', 'Pending', 'Terminating', 'Detaching', 'EnteringStandby'])
def test_skips_when_asg_has_an_instance_that_is_not_in_service(aws, rng, lifecycle):
    # E.g. Standby with the desired capacity decremented: 2 healthy of 2 desired, but FIS could pick the third.
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.template('web')
    aws.asg(asg_name('web'), desired=2, healthy=2, other_states=[lifecycle])
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_INSTANCE_NOT_IN_SERVICE'


def test_skips_when_a_running_instance_with_the_template_tags_is_outside_the_asg(aws, rng):
    # E.g. an instance detached from the ASG but still running: FIS selects by tag, so it could pick it.
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.template('web')
    aws.asg(asg_name('web'))
    aws.ec2_instances()
    aws.fis_targets(resource_tags('web'), ids=['i-00000000000000000', 'i-00000000000000001', 'i-0detached00000000'])
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_UNMANAGED_INSTANCE'


@pytest.mark.parametrize('ids', [['i-00000000000000000'], []], ids=['one-untagged', 'none-tagged'])
def test_skips_without_claiming_when_a_healthy_asg_member_lacks_the_template_tags(aws, rng, ids):
    # FIS could never pick an untagged member, and with no match at all it would fail after the day is claimed.
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.template('web')
    aws.asg(asg_name('web'))
    aws.ec2_instances()
    aws.fis_targets(resource_tags('web'), ids=ids)
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_UNTAGGED_INSTANCE'


def test_unmanaged_instance_check_uses_every_template_tag(aws, rng):
    rng(HIT, choice_value('web'))
    aws.config(make_config(dryRun=True))
    aws.claims([])
    aws.stack()
    tags = {'App': APP, 'Role': 'web', 'Tier': 'frontend'}
    aws.template('web', make_template('web', resourceTags=tags))
    aws.healthy_asg(asg_name('web'), tags)
    aws.alarms()
    aws.experiments()
    assert run()['status'] == 'DRY_RUN'


@pytest.mark.parametrize('state', ['shutting-down', 'terminated', 'stopped'])
def test_skips_when_asg_still_counts_an_instance_that_is_not_running(aws, rng, state):
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.template('web')
    aws.asg(asg_name('web'))
    aws.ec2_instances(states=['running', state])
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_INSTANCE_NOT_RUNNING'


def test_skips_when_asg_instance_no_longer_exists_in_ec2(aws, rng):
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.template('web')
    aws.asg(asg_name('web'))
    aws.ec2.add_client_error('describe_instances', 'InvalidInstanceID.NotFound')
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_INSTANCE_NOT_RUNNING'


def test_skips_while_an_instance_is_in_the_health_check_grace_period(aws, rng):
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.template('web')
    aws.asg(asg_name('web'))
    aws.ec2_instances(launched=[LONG_AGO, NOW - timedelta(seconds=GRACE_PERIOD - 1)])
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_IN_GRACE_PERIOD'


def test_instance_just_past_the_grace_period_is_settled(aws, rng):
    rng(HIT, choice_value('web'))
    aws.config(make_config(dryRun=True))
    aws.claims([])
    aws.stack()
    aws.template('web')
    aws.asg(asg_name('web'))
    aws.ec2_instances(launched=[LONG_AGO, NOW - timedelta(seconds=GRACE_PERIOD)])
    aws.fis_targets(resource_tags('web'))
    aws.activities(asg_name('web'))
    aws.alarms()
    aws.experiments()
    assert run()['status'] == 'DRY_RUN'


def test_target_without_required_asgs_checks_only_itself(aws, rng):
    rng(HIT, choice_value('web'))
    aws.config(make_config(dryRun=True))
    aws.claims([])
    aws.passing_prechecks('web')
    assert run()['status'] == 'DRY_RUN'


@pytest.mark.parametrize('name, required', [('api', 'worker'), ('worker', 'api')])
def test_skips_when_a_required_asg_is_not_healthy(aws, rng, name, required):
    rng(HIT, choice_value(name))
    aws.config(make_config())
    aws.claims([])
    aws.target_ready(name)
    aws.asg(asg_name(required), desired=2, healthy=1, unhealthy=1)
    assert run()['reason'] == 'PRECHECK_REQUIRED_ASG_DEGRADED'


def test_required_asg_needs_its_own_min_in_service(aws, rng):
    rng(HIT, choice_value('worker'))
    aws.config(make_config())
    aws.claims([])
    aws.target_ready('worker')
    aws.asg(asg_name('api'), desired=1, healthy=1)
    assert run()['reason'] == 'PRECHECK_REQUIRED_ASG_BELOW_MIN_IN_SERVICE'


@pytest.mark.parametrize('name', ['api', 'worker'])
def test_every_required_asg_is_checked_in_order(aws, rng, name):
    rng(HIT, choice_value(name))
    aws.config(make_config())
    aws.claims([])
    aws.target_ready(name)
    aws.healthy_asg(asg_name('worker' if name == 'api' else 'api'))
    aws.asg(asg_name('coordinator'), desired=1, healthy=0, unhealthy=1)
    assert run()['reason'] == 'PRECHECK_REQUIRED_ASG_DEGRADED'


def test_new_instance_in_a_required_asg_blocks_the_termination(aws, rng):
    rng(HIT, choice_value('api'))
    aws.config(make_config())
    aws.claims([])
    aws.target_ready('api')
    aws.healthy_asg(asg_name('worker'))
    aws.asg(asg_name('coordinator'))
    aws.ec2_instances(launched=[NOW - timedelta(seconds=10), LONG_AGO])
    assert run()['reason'] == 'PRECHECK_REQUIRED_ASG_IN_GRACE_PERIOD'


def test_a_target_can_require_more_healthy_instances_of_its_own_asg(aws, rng):
    # The target ASG passes with 2 of minInService 2, but requiring 3 of the same ASG blocks the termination.
    rng(HIT, choice_value('web'))
    aws.config(make_config(targets=[
        target(asgName=asg_name('web'), fisTemplateId=TEMPLATE_IDS['web'],
               requiredHealthy=[{'asgName': asg_name('web'), 'minInService': 3}]),
    ]))
    aws.claims([])
    aws.target_ready('web')
    aws.asg(asg_name('web'))
    assert run()['reason'] == 'PRECHECK_REQUIRED_ASG_BELOW_MIN_IN_SERVICE'


@pytest.mark.parametrize('state', ['ALARM', 'INSUFFICIENT_DATA'])
def test_skips_without_claiming_when_a_stop_condition_alarm_is_not_ok(aws, rng, state):
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.target_ready('web')
    aws.alarms(states=['OK', state])
    # No put_parameter is queued: the stubber fails the test if the day is claimed.
    assert run()['reason'] == 'PRECHECK_STOP_CONDITION_ALARM'


def test_skips_when_a_stop_condition_alarm_is_missing(aws, rng):
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.target_ready('web')
    aws.alarms(names=STOP_CONDITION_ALARMS[:1])
    assert run()['reason'] == 'PRECHECK_STOP_CONDITION_ALARM_NOT_FOUND'


def test_stop_condition_alarms_come_from_the_chosen_template(aws, rng):
    rng(HIT, choice_value('web'))
    aws.config(make_config(dryRun=True))
    aws.claims([])
    aws.stack()
    template = {**make_template('web'), 'stopConditions': [
        {'source': 'aws:cloudwatch:alarm', 'value': alarm_arn('only-this-one')},
    ]}
    aws.template('web', template)
    aws.healthy_asg(asg_name('web'), resource_tags('web'))
    aws.cloudwatch.add_response(
        'describe_alarms',
        {'MetricAlarms': [{'AlarmName': 'only-this-one', 'StateValue': 'OK'}]},
        {'AlarmNames': ['only-this-one'], 'AlarmTypes': ['MetricAlarm', 'CompositeAlarm']},
    )
    aws.experiments()
    assert run()['status'] == 'DRY_RUN'


@pytest.mark.parametrize('stop_conditions', [[], [{'source': 'none'}]])
def test_template_without_stop_condition_alarms_skips_the_alarm_check(aws, rng, stop_conditions):
    rng(HIT, choice_value('web'))
    aws.config(make_config(dryRun=True))
    aws.claims([])
    aws.stack()
    aws.template('web', {**make_template('web'), 'stopConditions': stop_conditions})
    aws.healthy_asg(asg_name('web'), resource_tags('web'))
    aws.experiments()
    assert run()['status'] == 'DRY_RUN'


def test_skips_when_target_asg_is_missing(aws, rng):
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.stack()
    aws.template('web')
    aws.autoscaling.add_response('describe_auto_scaling_groups', {'AutoScalingGroups': []})
    assert run()['reason'] == 'PRECHECK_TARGET_ASG_NOT_FOUND'


@pytest.mark.parametrize('name', ['web', 'api', 'worker'])
def test_skips_when_an_engine_experiment_is_active(aws, rng, name):
    # Experiments are looked up per template, so a busy account's other experiments are never paged through.
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.target_ready('web')
    aws.alarms()
    aws.experiments(active_in=TEMPLATE_IDS[name])
    assert run()['reason'] == 'PRECHECK_EXPERIMENT_ACTIVE'


def test_experiment_lookup_follows_pages(aws, rng):
    rng(HIT, choice_value('web'))
    aws.config(make_config())
    aws.claims([])
    aws.target_ready('web')
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
    rng(HIT, choice_value('api'))
    aws.config(make_config(dryRun=True))
    aws.claims([])
    aws.passing_prechecks('api')
    result = run()
    assert result['status'] == 'DRY_RUN'
    assert result['reason'] == 'WOULD_TERMINATE'
    assert result['target'] == 'api'


def test_executes_termination(aws, rng, capsys):
    rng(HIT, choice_value('worker'))
    aws.config(make_config())
    aws.claims([])
    aws.passing_prechecks('worker')
    aws.create_claim()
    aws.fis.add_response(
        'start_experiment',
        {'experiment': {'id': 'EXPNEW'}},
        {
            'clientToken': engine.client_token(CONFIG_PARAMETER_NAME, SCHEDULED_TIME, CONTEXT),
            'experimentTemplateId': TEMPLATE_IDS['worker'],
            'tags': {
                'App': APP,
                'ChaosTarget': 'worker',
                'TriggeredBy': 'asg-chaos-fis',
                'LocalDate': TODAY,
            },
        },
    )
    aws.update_claim()

    result = run()

    assert result['status'] == 'EXECUTED'
    assert result['experimentId'] == 'EXPNEW'
    assert result['target'] == 'worker'
    metric = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert metric['Status'] == 'EXECUTED'
    assert metric['App'] == APP
    assert metric['_aws']['CloudWatchMetrics'][0]['Namespace'] == 'Demo/Chaos'
    assert metric['_aws']['CloudWatchMetrics'][0]['Dimensions'] == [['App', 'Status'], ['App', 'Reason']]


def test_metrics_default_to_the_library_namespace_without_dimensions(aws, capsys):
    config = make_config(enabled=False)
    del config['metricNamespace'], config['metricDimensions']
    aws.config(config)
    run()
    metric = json.loads(capsys.readouterr().out.strip())
    assert metric['_aws']['CloudWatchMetrics'][0]['Namespace'] == 'ASGChaosFIS'
    assert metric['_aws']['CloudWatchMetrics'][0]['Dimensions'] == [['Status'], ['Reason']]


def test_skips_when_another_invocation_claimed_the_day_first(aws, rng):
    rng(HIT, choice_value('worker'))
    aws.config(make_config())
    aws.claims([])
    aws.passing_prechecks('worker')
    aws.create_claim(error_code='ParameterAlreadyExists')
    assert run()['reason'] == 'DAILY_LIMIT_REACHED'


def test_fis_failure_keeps_claim_and_raises(aws, rng):
    rng(HIT, choice_value('worker'))
    aws.config(make_config())
    aws.claims([])
    aws.passing_prechecks('worker')
    aws.create_claim()
    aws.fis.add_client_error('start_experiment', 'ValidationException')
    aws.update_claim()
    with pytest.raises(ClientError):
        run()


def test_claim_update_failure_after_start_still_reports_execution(aws, rng):
    rng(HIT, choice_value('worker'))
    aws.config(make_config())
    aws.claims([])
    aws.passing_prechecks('worker')
    aws.create_claim()
    aws.fis.add_response('start_experiment', {'experiment': {'id': 'EXPNEW'}})
    aws.update_claim(error_code='ThrottlingException')
    assert run()['status'] == 'EXECUTED'


def test_run_accepts_a_config_built_in_code(aws, rng):
    # A deployment can build the configuration itself and call run() without the SSM configuration parameter.
    rng(HIT, choice_value('web'))
    config = engine.parse_config(make_config(dryRun=True))
    settings = engine.Settings(config_parameter_name='in-code', state_parameter_path=STATE_PARAMETER_PATH)
    aws.claims([])
    aws.passing_prechecks('web')
    assert engine.run(config, settings, EVENT, CONTEXT)['status'] == 'DRY_RUN'


def test_client_token_is_stable_per_schedule_slot():
    first = engine.client_token(CONFIG_PARAMETER_NAME, SCHEDULED_TIME, SimpleNamespace(aws_request_id='a'))
    retry = engine.client_token(CONFIG_PARAMETER_NAME, SCHEDULED_TIME, SimpleNamespace(aws_request_id='b'))
    other_deployment = engine.client_token('/other/chaos/config', SCHEDULED_TIME, None)
    manual = engine.client_token(CONFIG_PARAMETER_NAME, None, SimpleNamespace(aws_request_id='a'))
    assert first == retry
    assert first != other_deployment
    assert first != manual
    assert len(first) == 64
