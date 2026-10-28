# -*- coding: utf8 -*-

"""
Shared fixtures for the AEM Chaos Engine tests.

AWS calls are intercepted with botocore Stubbers, which also validate request
parameters and responses against the real AWS API models.
"""

# pylint: disable=missing-function-docstring

import json
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
import pytest
from botocore.stub import ANY, Stubber

import aem_chaos_engine as engine

STACK_PREFIX = 'sandpit'
CONFIG_PARAMETER_NAME = f'/aem-opencloud/{STACK_PREFIX}/chaos-engine/config'
STATE_PARAMETER_PATH = f'/aem-opencloud/{STACK_PREFIX}/chaos-engine/kills'
STACK_NAME = f'{STACK_PREFIX}-aem-full-set-main-stack'
NOW = datetime(2026, 10, 14, 10, 5, tzinfo=ZoneInfo('Australia/Sydney'))
TODAY = NOW.date().isoformat()
SCHEDULED_TIME = '2026-10-13T23:00:00Z'
GRACE_PERIOD = 300
LONG_AGO = NOW - timedelta(days=1)
STOP_CONDITION_ALARMS = [
    f'{STACK_PREFIX}-ChaosEngine-PublishDispatcher-UnHealthyHostsAlarm',
    f'{STACK_PREFIX}-ChaosEngine-AuthorDispatcher-UnHealthyHostsAlarm',
]

TEMPLATE_IDS = {
    'author-dispatcher': 'EXTAUTHORDISPATCHER',
    'publish': 'EXTPUBLISH',
    'publish-dispatcher': 'EXTPUBLISHDISPATCHER',
}


def asg_name(component):
    return f'{STACK_PREFIX}-{component}-asg'


def make_config(**overrides):
    config = {
        'enabled': True,
        'dryRun': False,
        'timezone': 'Australia/Sydney',
        'dailyProbability': 1.0,
        'runsPerDay': 6,
        'retentionDays': 7,
        'cloudFormationStackName': STACK_NAME,
        'orchestratorAsgName': asg_name('orchestrator'),
        'stopConditionAlarmNames': STOP_CONDITION_ALARMS,
        'targets': [
            {'component': component, 'asgName': asg_name(component), 'fisTemplateId': template_id}
            for component, template_id in TEMPLATE_IDS.items()
        ],
    }
    config.update(overrides)
    return config


class SequenceRandom(random.Random):
    """random.Random whose random() returns preset values, used by both the roll and choices()."""

    def __init__(self, values):
        super().__init__()
        self.values = list(values)

    def random(self):
        return self.values.pop(0)


def choice_value(component):
    """random() value that makes choices() pick ``component`` from the default equal-weight targets."""
    index = list(TEMPLATE_IDS).index(component)
    return (index + 0.5) / len(TEMPLATE_IDS)


class Aws:
    """Stubbed AWS clients plus helpers to queue the expected calls in order."""

    def __init__(self):
        credentials = {
            'region_name': 'ap-southeast-2',
            'aws_access_key_id': 'testing',
            'aws_secret_access_key': 'testing',
        }
        self.clients = {
            name: boto3.client(name, **credentials)
            for name in ('ssm', 'fis', 'autoscaling', 'cloudformation', 'ec2', 'cloudwatch')
        }
        self.stubbers = {name: Stubber(client) for name, client in self.clients.items()}

    def __getattr__(self, name):
        return self.stubbers[name]

    def config(self, config):
        self.ssm.add_response(
            'get_parameter',
            {'Parameter': {'Name': CONFIG_PARAMETER_NAME, 'Type': 'String', 'Value': json.dumps(config)}},
            {'Name': CONFIG_PARAMETER_NAME},
        )

    def claims(self, dates):
        self.ssm.add_response(
            'get_parameters_by_path',
            {'Parameters': [
                {'Name': f'{STATE_PARAMETER_PATH}/{claim_date}', 'Type': 'String', 'Value': '{}'}
                for claim_date in dates
            ]},
            {'Path': STATE_PARAMETER_PATH, 'Recursive': False},
        )

    def stack(self, status='UPDATE_COMPLETE'):
        self.cloudformation.add_response(
            'describe_stacks',
            {'Stacks': [{
                'StackName': STACK_NAME,
                'CreationTime': datetime(2026, 1, 1, tzinfo=timezone.utc),
                'StackStatus': status,
            }]},
            {'StackName': STACK_NAME},
        )

    def asg(self, name, desired=2, healthy=2, unhealthy=0, other_states=()):
        """An ASG with healthy, then unhealthy InService instances, then one instance per other lifecycle state."""
        states = [('InService', 'Healthy')] * healthy + [('InService', 'Unhealthy')] * unhealthy
        states += [(state, 'Healthy') for state in other_states]
        instances = [
            {
                'InstanceId': f'i-{index:017d}',
                'AvailabilityZone': 'ap-southeast-2a',
                'LifecycleState': lifecycle,
                'HealthStatus': health,
                'ProtectedFromScaleIn': False,
            }
            for index, (lifecycle, health) in enumerate(states)
        ]
        self.autoscaling.add_response(
            'describe_auto_scaling_groups',
            {'AutoScalingGroups': [{
                'AutoScalingGroupName': name,
                'MinSize': 1,
                'MaxSize': 4,
                'DesiredCapacity': desired,
                'DefaultCooldown': 300,
                'AvailabilityZones': ['ap-southeast-2a'],
                'HealthCheckType': 'ELB',
                'HealthCheckGracePeriod': GRACE_PERIOD,
                'CreatedTime': datetime(2026, 1, 1, tzinfo=timezone.utc),
                'Instances': instances,
            }]},
            {'AutoScalingGroupNames': [name]},
        )

    def ec2_instances(self, count=2, states=None, launched=None):
        """EC2 view of the first ``count`` instances that asg() created."""
        states = states or ['running'] * count
        launched = launched or [LONG_AGO] * count
        ids = [f'i-{index:017d}' for index in range(count)]
        self.ec2.add_response(
            'describe_instances',
            {'Reservations': [{'Instances': [
                {'InstanceId': instance_id, 'State': {'Name': state}, 'LaunchTime': launch_time}
                for instance_id, state, launch_time in zip(ids, states, launched)
            ]}]},
            {'InstanceIds': ids},
        )

    def fis_targets(self, component, ids=None):
        """Running instances with the target tags, as FIS would select them (default: the 2 healthy ones)."""
        ids = [f'i-{index:017d}' for index in range(2)] if ids is None else ids
        self.ec2.add_response(
            'describe_instances',
            {'Reservations': [{'Instances': [{'InstanceId': instance_id} for instance_id in ids]}]},
            {'Filters': [
                {'Name': 'tag:StackPrefix', 'Values': [STACK_PREFIX]},
                {'Name': 'tag:Component', 'Values': [component]},
                {'Name': 'instance-state-name', 'Values': ['running']},
            ]},
        )

    def activities(self, name, statuses=('Successful',)):
        self.autoscaling.add_response(
            'describe_scaling_activities',
            {'Activities': [
                {
                    'ActivityId': f'activity-{index}',
                    'AutoScalingGroupName': name,
                    'Cause': 'test',
                    'StartTime': datetime(2026, 10, 1, tzinfo=timezone.utc),
                    'StatusCode': status,
                }
                for index, status in enumerate(statuses)
            ]},
            {'AutoScalingGroupName': name, 'MaxRecords': 10},
        )

    def healthy_asg(self, name, component=None):
        """A passing ASG check; ``component`` makes it the target ASG, whose FIS targets are checked too."""
        self.asg(name)
        self.ec2_instances()
        if component:
            self.fis_targets(component)
        self.activities(name)

    def alarms(self, states=None, names=None):
        names = STOP_CONDITION_ALARMS if names is None else names
        states = states or ['OK'] * len(names)
        self.cloudwatch.add_response(
            'describe_alarms',
            {'MetricAlarms': [
                {'AlarmName': name, 'StateValue': state} for name, state in zip(names, states)
            ]},
            {'AlarmNames': STOP_CONDITION_ALARMS, 'AlarmTypes': ['MetricAlarm', 'CompositeAlarm']},
        )

    def experiments(self, active_in=None):
        """One ListExperiments call per template, in template id order, up to the one with an active experiment."""
        for template_id in sorted(TEMPLATE_IDS.values()):
            active = template_id == active_in
            self.fis.add_response(
                'list_experiments',
                {'experiments': [{'id': 'EXPACTIVE', 'state': {'status': 'running'}}] if active else []},
                {'experimentTemplateId': template_id, 'maxResults': 100},
            )
            if active:
                return

    def passing_prechecks(self, component):
        self.stack()
        self.healthy_asg(asg_name(component), component)
        partner = engine.PAIRED_COMPONENTS.get(component)
        if partner:
            self.healthy_asg(asg_name(partner))
            self.healthy_asg(asg_name('orchestrator'))
        self.alarms()
        self.experiments()

    def create_claim(self, error_code=None):
        params = {
            'Name': f'{STATE_PARAMETER_PATH}/{TODAY}',
            'Description': ANY,
            'Value': ANY,
            'Type': 'String',
            'Overwrite': False,
        }
        if error_code:
            self.ssm.add_client_error('put_parameter', error_code, expected_params=params)
        else:
            self.ssm.add_response('put_parameter', {'Version': 1}, params)

    def update_claim(self, error_code=None):
        params = {'Name': f'{STATE_PARAMETER_PATH}/{TODAY}', 'Value': ANY, 'Type': 'String', 'Overwrite': True}
        if error_code:
            self.ssm.add_client_error('put_parameter', error_code, expected_params=params)
        else:
            self.ssm.add_response('put_parameter', {'Version': 2}, params)


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv('STACK_PREFIX', STACK_PREFIX)
    monkeypatch.setenv('CONFIG_PARAMETER_NAME', CONFIG_PARAMETER_NAME)
    monkeypatch.setenv('STATE_PARAMETER_PATH', STATE_PARAMETER_PATH)
    monkeypatch.setattr(engine, '_now', NOW.astimezone)


@pytest.fixture
def aws(monkeypatch):
    stubbed = Aws()
    monkeypatch.setattr(engine, '_client', stubbed.clients.__getitem__)
    for stubber in stubbed.stubbers.values():
        stubber.activate()
    yield stubbed
    for stubber in stubbed.stubbers.values():
        stubber.assert_no_pending_responses()
        stubber.deactivate()


@pytest.fixture
def rng(monkeypatch):
    def install(*values):
        monkeypatch.setattr(engine, '_rng', SequenceRandom(values))
    return install
