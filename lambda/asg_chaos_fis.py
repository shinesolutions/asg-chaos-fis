# -*- coding: utf8 -*-

"""
ASG Chaos FIS.

Terminates, at most once per day, one random instance of one of the configured
Auto Scaling Groups, using an AWS Fault Injection Service (FIS) experiment
template. A scheduler such as Amazon EventBridge Scheduler invokes the Lambda
several times per day. Each invocation decides whether to start an experiment
and, if so, which target's.

The library only knows about Auto Scaling Groups and FIS experiment templates.
Everything specific to the application, such as which Auto Scaling Groups may
be targeted and which others must be healthy first, comes from the JSON
configuration.

Safety guardrails:

* At most ONE termination per local calendar day across all targets. The day
  is claimed by creating an SSM parameter with ``Overwrite=False``, which is
  atomic, BEFORE FIS is called. The claim is kept even if FIS fails, so a
  failure or a duplicate invocation can never cause a second termination.
* Each target's FIS experiment template must select exactly one running
  instance by tags. Other templates are not used.
* Pre-checks before every termination: the CloudFormation stack, if
  configured, is stable; the target Auto Scaling Group is fully healthy with
  enough InService instances, all of them running in EC2 and past the health
  check grace period, no other instance in it, exactly its instances running
  with the template's target tags, and no scaling activity in progress; the
  target's required Auto Scaling Groups pass the same checks; the template's
  stop condition alarms are OK; and no experiment of the configured templates
  is active.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
from botocore.exceptions import ClientError

__copyright__ = 'Shine Solutions'
__license__ = 'Apache License, Version 2.0'

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv('LOG_LEVEL', 'INFO').upper())

SCHEMA_VERSION = 1
DEFAULT_MIN_IN_SERVICE = 2
DEFAULT_REQUIRED_MIN_IN_SERVICE = 1
DEFAULT_RETENTION_DAYS = 7
DEFAULT_METRIC_NAMESPACE = 'ASGChaosFIS'
TRIGGERED_BY = 'asg-chaos-fis'

# Tags the library adds to every experiment, and the limits FIS sets on tags.
FIXED_EXPERIMENT_TAG_KEYS = frozenset({'ChaosTarget', 'TriggeredBy', 'LocalDate'})
MAX_EXPERIMENT_TAGS = 50 - len(FIXED_EXPERIMENT_TAG_KEYS)
# Members of the decision record that metric dimensions must not overwrite.
RESERVED_RECORD_KEYS = frozenset({
    '_aws', 'Status', 'Reason', 'Decisions',
    'localDate', 'scheduledTime', 'roll', 'threshold', 'target', 'asgName', 'experimentId', 'error',
})
# CloudWatch allows 30 dimensions per metric; Status or Reason is always added.
MAX_METRIC_DIMENSIONS = 29

STABLE_STACK_STATUSES = frozenset({
    'CREATE_COMPLETE',
    'UPDATE_COMPLETE',
    'UPDATE_ROLLBACK_COMPLETE',
    'IMPORT_COMPLETE',
    'IMPORT_ROLLBACK_COMPLETE',
})
TERMINAL_SCALING_ACTIVITY_STATUSES = frozenset({'Successful', 'Failed', 'Cancelled'})
ACTIVE_EXPERIMENT_STATUSES = frozenset({'pending', 'initiating', 'running', 'stopping'})

STATUS_EXECUTED = 'EXECUTED'
STATUS_SKIPPED = 'SKIPPED'
STATUS_DRY_RUN = 'DRY_RUN'
STATUS_FAILED = 'FAILED'

_REQUIRED = object()

# Indirections so tests can control randomness, time and AWS clients.
_rng = random.Random()


def _now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


@functools.cache
def _client(service: str) -> Any:
    return boto3.client(service)


class ConfigurationError(Exception):
    """Raised when the environment or the SSM configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Deployment settings passed in as Lambda environment variables."""

    config_parameter_name: str
    state_parameter_path: str

    @classmethod
    def from_env(cls, environ: Any = None) -> Settings:
        """Read settings from the environment, failing fast if any are missing."""
        environ = os.environ if environ is None else environ
        names = ('CONFIG_PARAMETER_NAME', 'STATE_PARAMETER_PATH')
        missing = [name for name in names if not environ.get(name)]
        if missing:
            raise ConfigurationError(f"Missing required environment variables: {', '.join(missing)}")
        return cls(
            config_parameter_name=environ['CONFIG_PARAMETER_NAME'],
            state_parameter_path=environ['STATE_PARAMETER_PATH'].rstrip('/'),
        )


@dataclass(frozen=True)
class RequiredAsg:
    """An Auto Scaling Group that must be healthy before the target is terminated."""

    asg_name: str
    min_in_service: int


@dataclass(frozen=True)
class Target:  # pylint: disable=too-many-instance-attributes
    """An Auto Scaling Group that may have one of its instances terminated."""

    name: str
    enabled: bool
    asg_name: str
    fis_template_id: str
    weight: float
    min_in_service: int
    required_healthy: tuple[RequiredAsg, ...]


@dataclass(frozen=True)
class EngineConfig:  # pylint: disable=too-many-instance-attributes
    """Engine configuration, stored as JSON in SSM Parameter Store."""

    enabled: bool
    dry_run: bool
    timezone: ZoneInfo
    daily_probability: float
    runs_per_day: int
    retention_days: int
    cloudformation_stack_name: str | None
    metric_namespace: str
    metric_dimensions: dict[str, str]
    experiment_tags: dict[str, str]
    targets: tuple[Target, ...]
    all_targets: tuple[Target, ...]

    @property
    def run_probability(self) -> float:
        """
        Probability that a single invocation attempts a termination.

        Like Simian Army, ``dailyProbability`` is spread over the runs of a
        day: 1.0 means one termination attempt per day on average, and a value
        equal to ``runsPerDay`` means every run attempts one until the daily
        limit is reached.
        """
        return min(1.0, self.daily_probability / self.runs_per_day)


@dataclass(frozen=True)
class TemplateTarget:
    """What a supported FIS experiment template can select and must check before it runs."""

    resource_tags: dict[str, str]
    stop_condition_alarm_names: tuple[str, ...]


def _field(raw: dict, key: str, kind: type, default: Any = _REQUIRED, where: str = '') -> Any:
    """Return ``raw[key]`` after checking its type, or ``default`` if absent."""
    name = f'{where}{key}'
    if key not in raw:
        if default is _REQUIRED:
            raise ConfigurationError(f"Config field '{name}' is required")
        return default
    value = raw[key]
    if kind is bool:
        valid = isinstance(value, bool)
    elif kind is int:
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif kind is float:
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        value = float(value) if valid else value
    else:
        valid = isinstance(value, str) and value != ''
    if not valid:
        raise ConfigurationError(f"Config field '{name}' must be a {kind.__name__}, got {value!r}")
    return value


def _string_map(raw: dict, key: str, max_items: int,  # pylint: disable=too-many-arguments,too-many-positional-arguments
                reserved: frozenset[str], key_length: int, value_length: int) -> dict[str, str]:
    """Return ``raw[key]`` as a dict of non-empty strings, or {} if absent."""
    value = raw.get(key, {})
    valid = (isinstance(value, dict) and len(value) <= max_items
             and all(isinstance(k, str) and 0 < len(k) <= key_length and not k.lower().startswith('aws:')
                     and isinstance(v, str) and 0 < len(v) <= value_length for k, v in value.items()))
    if not valid:
        raise ConfigurationError(
            f"Config field '{key}' must be an object of up to {max_items} non-empty strings "
            f"(keys up to {key_length} characters and not starting with 'aws:', values up to {value_length})"
        )
    clashes = sorted(reserved.intersection(value))
    if clashes:
        raise ConfigurationError(f"Config field '{key}' must not contain {', '.join(clashes)}, which the engine sets")
    return dict(value)


def _parse_required_asg(raw: Any, where: str) -> RequiredAsg:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"Config field '{where.rstrip('.')}' must be an object")
    min_in_service = _field(raw, 'minInService', int, DEFAULT_REQUIRED_MIN_IN_SERVICE, where)
    if min_in_service < 1:
        raise ConfigurationError(f"Config field '{where}minInService' must be at least 1")
    return RequiredAsg(asg_name=_field(raw, 'asgName', str, where=where), min_in_service=min_in_service)


def _parse_target(raw: Any, index: int) -> Target:
    where = f'targets[{index}].'
    if not isinstance(raw, dict):
        raise ConfigurationError(f"Config field 'targets[{index}]' must be an object")
    name = _field(raw, 'name', str, where=where)
    if len(name) > 256:
        raise ConfigurationError(f"Config field '{where}name' must be at most 256 characters")
    weight = _field(raw, 'weight', float, 1.0, where)
    if weight <= 0:
        raise ConfigurationError(f"Config field '{where}weight' must be greater than 0")
    min_in_service = _field(raw, 'minInService', int, DEFAULT_MIN_IN_SERVICE, where)
    if min_in_service < 1:
        raise ConfigurationError(f"Config field '{where}minInService' must be at least 1")
    raw_required = raw.get('requiredHealthy', [])
    if not isinstance(raw_required, list):
        raise ConfigurationError(f"Config field '{where}requiredHealthy' must be a list")
    return Target(
        name=name,
        enabled=_field(raw, 'enabled', bool, True, where),
        asg_name=_field(raw, 'asgName', str, where=where),
        fis_template_id=_field(raw, 'fisTemplateId', str, where=where),
        weight=weight,
        min_in_service=min_in_service,
        required_healthy=tuple(
            _parse_required_asg(item, f'{where}requiredHealthy[{position}].')
            for position, item in enumerate(raw_required)
        ),
    )


def parse_config(raw: Any) -> EngineConfig:
    """Validate the raw JSON configuration and convert it to an EngineConfig."""
    if not isinstance(raw, dict):
        raise ConfigurationError('Config must be a JSON object')

    schema_version = _field(raw, 'schemaVersion', int)
    if schema_version != SCHEMA_VERSION:
        raise ConfigurationError(
            f"Config field 'schemaVersion' is {schema_version}, but this version of the engine "
            f"only supports {SCHEMA_VERSION}"
        )

    enabled = _field(raw, 'enabled', bool)

    timezone_name = _field(raw, 'timezone', str)
    try:
        local_timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise ConfigurationError(f"Config field 'timezone' has unknown timezone '{timezone_name}'") from err

    runs_per_day = _field(raw, 'runsPerDay', int)
    if runs_per_day < 1:
        raise ConfigurationError("Config field 'runsPerDay' must be at least 1")
    daily_probability = _field(raw, 'dailyProbability', float)
    if not 0 <= daily_probability <= runs_per_day:
        raise ConfigurationError(
            f"Config field 'dailyProbability' must be between 0 and runsPerDay ({runs_per_day})"
        )
    retention_days = _field(raw, 'retentionDays', int, DEFAULT_RETENTION_DAYS)
    if retention_days < 1:
        raise ConfigurationError("Config field 'retentionDays' must be at least 1")

    raw_targets = raw.get('targets', [])
    if not isinstance(raw_targets, list):
        raise ConfigurationError("Config field 'targets' must be a list")
    all_targets = [_parse_target(item, index) for index, item in enumerate(raw_targets)]
    names = [target.name for target in all_targets]
    if len(names) != len(set(names)):
        raise ConfigurationError("Config field 'targets' must not contain the same name twice")
    # Disabled targets are validated but otherwise ignored.
    targets = tuple(target for target in all_targets if target.enabled)
    if enabled and not targets:
        raise ConfigurationError("Config field 'targets' must contain an enabled target when the engine is enabled")

    return EngineConfig(
        enabled=enabled,
        dry_run=_field(raw, 'dryRun', bool, False),
        timezone=local_timezone,
        daily_probability=daily_probability,
        runs_per_day=runs_per_day,
        retention_days=retention_days,
        cloudformation_stack_name=_field(raw, 'cloudFormationStackName', str, None),
        metric_namespace=_field(raw, 'metricNamespace', str, DEFAULT_METRIC_NAMESPACE),
        metric_dimensions=_string_map(raw, 'metricDimensions', MAX_METRIC_DIMENSIONS, RESERVED_RECORD_KEYS,
                                      250, 1024),
        experiment_tags=_string_map(raw, 'experimentTags', MAX_EXPERIMENT_TAGS, FIXED_EXPERIMENT_TAG_KEYS,
                                    128, 256),
        targets=targets,
        all_targets=tuple(all_targets),
    )


def load_config(parameter_name: str) -> EngineConfig:
    """Read and validate the engine configuration from SSM Parameter Store."""
    try:
        response = _client('ssm').get_parameter(Name=parameter_name)
    except ClientError as err:
        if err.response['Error']['Code'] == 'ParameterNotFound':
            raise ConfigurationError(f"Config parameter '{parameter_name}' does not exist") from err
        raise
    try:
        raw = json.loads(response['Parameter']['Value'])
    except json.JSONDecodeError as err:
        raise ConfigurationError(f"Config parameter '{parameter_name}' is not valid JSON") from err
    return parse_config(raw)


def list_claims(state_parameter_path: str) -> dict[date, str]:
    """Return the existing daily claims as {local date: parameter name}."""
    claims = {}
    paginator = _client('ssm').get_paginator('get_parameters_by_path')
    for page in paginator.paginate(Path=state_parameter_path, Recursive=False):
        for parameter in page['Parameters']:
            name = parameter['Name']
            try:
                claims[date.fromisoformat(name.rsplit('/', 1)[-1])] = name
            except ValueError:
                logger.warning('Ignoring parameter %s: its name is not a date', name)
    return claims


def prune_claims(claims: dict[date, str], today: date, retention_days: int) -> None:
    """Delete claims older than the retention window. Failures are only logged."""
    cutoff = today - timedelta(days=retention_days)
    expired = sorted(name for claim_date, name in claims.items() if claim_date < cutoff)
    for start in range(0, len(expired), 10):
        batch = expired[start:start + 10]
        try:
            _client('ssm').delete_parameters(Names=batch)
            logger.info('Deleted expired claims: %s', ', '.join(batch))
        except ClientError as err:
            logger.warning('Failed to delete expired claims %s: %s', ', '.join(batch), err)


def create_claim(parameter_name: str, record: dict) -> bool:
    """Atomically claim the day. Returns False if the day is already claimed."""
    try:
        _client('ssm').put_parameter(
            Name=parameter_name,
            Description='ASG Chaos FIS daily termination claim',
            Value=json.dumps(record),
            Type='String',
            Overwrite=False,
        )
        return True
    except ClientError as err:
        if err.response['Error']['Code'] == 'ParameterAlreadyExists':
            return False
        raise


def update_claim(parameter_name: str, record: dict) -> None:
    """Record the outcome on a claim this invocation owns. Failures are only logged."""
    try:
        _client('ssm').put_parameter(
            Name=parameter_name,
            Value=json.dumps(record),
            Type='String',
            Overwrite=True,
        )
    except ClientError as err:
        logger.warning('Failed to update claim %s with %s: %s', parameter_name, record, err)


def choose_target(targets: tuple[Target, ...]) -> Target:
    """Pick one target at random, weighted by its ``weight``."""
    return _rng.choices(targets, weights=[target.weight for target in targets], k=1)[0]


def check_stack_stable(stack_name: str | None) -> str | None:
    """Fail if the CloudFormation stack, if configured, is missing or being created, updated or deleted."""
    if stack_name is None:
        return None
    try:
        stacks = _client('cloudformation').describe_stacks(StackName=stack_name)['Stacks']
    except ClientError as err:
        if err.response['Error']['Code'] == 'ValidationError':
            return 'PRECHECK_STACK_NOT_FOUND'
        raise
    status = stacks[0]['StackStatus']
    if status not in STABLE_STACK_STATUSES:
        logger.info('Stack %s is not stable: %s', stack_name, status)
        return 'PRECHECK_STACK_NOT_STABLE'
    return None


def _unsupported_template_reason(template: dict) -> str | None:  # pylint: disable=too-many-return-statements
    """Explain why the library cannot vouch for what the template selects, or None if it can."""
    targets = template.get('targets') or {}
    if len(targets) != 1:
        return f'it has {len(targets)} targets instead of 1'
    target = next(iter(targets.values()))
    filters = target.get('filters') or []
    if target.get('resourceType') != 'aws:ec2:instance':
        return f"its target selects {target.get('resourceType')} resources, not aws:ec2:instance"
    if target.get('resourceArns'):
        return 'its target selects instances by ARN'
    if not target.get('resourceTags'):
        return 'its target does not select instances by tags'
    if target.get('selectionMode') != 'COUNT(1)':
        return f"its target selection mode is {target.get('selectionMode')}, not COUNT(1)"
    if (len(filters) != 1 or filters[0].get('path') != 'State.Name'
            or filters[0].get('values') != ['running']):
        return 'its target is not filtered to State.Name running only'
    for stop_condition in template.get('stopConditions') or []:
        if stop_condition.get('source') not in ('none', 'aws:cloudwatch:alarm'):
            return f"it has a stop condition from {stop_condition.get('source')}"
    return None


def read_template_target(template_id: str) -> TemplateTarget | str:
    """
    Read what the FIS template selects and which alarms stop it, or return the reason to skip.

    The template is the single source of truth for the instances FIS can pick,
    so only templates whose selection the pre-checks can fully cover are used:
    one target that picks one running EC2 instance by tags.
    """
    try:
        template = _client('fis').get_experiment_template(id=template_id)['experimentTemplate']
    except ClientError as err:
        if err.response['Error']['Code'] == 'ResourceNotFoundException':
            logger.warning('FIS experiment template %s does not exist', template_id)
            return 'PRECHECK_FIS_TEMPLATE_NOT_FOUND'
        raise
    reason = _unsupported_template_reason(template)
    if reason:
        logger.warning('FIS experiment template %s is not supported: %s', template_id, reason)
        return 'PRECHECK_FIS_TEMPLATE_UNSUPPORTED'
    target = next(iter(template['targets'].values()))
    alarm_names = tuple(
        stop_condition['value'].split(':alarm:', 1)[-1]
        for stop_condition in template.get('stopConditions') or []
        if stop_condition.get('source') == 'aws:cloudwatch:alarm'
    )
    return TemplateTarget(resource_tags=dict(target['resourceTags']), stop_condition_alarm_names=alarm_names)


def check_instances_settled(instance_ids: list[str], grace_period: int, label: str) -> str | None:
    """
    Fail unless every instance is running in EC2 and older than the health check grace period.

    The Auto Scaling Group's own view is not enough: it notices a terminated
    instance only after about a minute, and it reports new instances as
    Healthy without checking them during the grace period.
    """
    try:
        reservations = _client('ec2').describe_instances(InstanceIds=instance_ids)['Reservations']
    except ClientError as err:
        if err.response['Error']['Code'] == 'InvalidInstanceID.NotFound':
            logger.info('Instances %s not found in EC2', ', '.join(instance_ids))
            return f'PRECHECK_{label}_INSTANCE_NOT_RUNNING'
        raise
    instances = {instance['InstanceId']: instance for r in reservations for instance in r['Instances']}
    now = _now(timezone.utc)
    for instance_id in instance_ids:
        instance = instances.get(instance_id)
        state = instance['State']['Name'] if instance else 'missing'
        if state != 'running':
            logger.info('Instance %s is %s in EC2', instance_id, state)
            return f'PRECHECK_{label}_INSTANCE_NOT_RUNNING'
        age = (now - instance['LaunchTime']).total_seconds()
        if age < grace_period:
            logger.info('Instance %s launched %ds ago, within the %ds health check grace period',
                        instance_id, age, grace_period)
            return f'PRECHECK_{label}_IN_GRACE_PERIOD'
    return None


def check_fis_targets_in_asg(resource_tags: dict[str, str], asg_instance_ids: set[str]) -> str | None:
    """
    Fail unless the instances FIS can pick are exactly the healthy InService members of the ASG.

    FIS selects any running instance with all of the template's target tags, so
    this covers exactly what it can terminate, e.g. an instance that was
    detached from the Auto Scaling Group but is still running. The other way
    round, an ASG member without the tags could never be picked, and a
    template that matches no instance at all would fail only after the day has
    been claimed.
    """
    paginator = _client('ec2').get_paginator('describe_instances')
    filters = [{'Name': f'tag:{key}', 'Values': [value]} for key, value in sorted(resource_tags.items())]
    filters.append({'Name': 'instance-state-name', 'Values': ['running']})
    pages = paginator.paginate(Filters=filters)
    running = {instance['InstanceId'] for page in pages for r in page['Reservations'] for instance in r['Instances']}
    outside = sorted(running - asg_instance_ids)
    if outside:
        logger.info('Running instances with tags %s outside the healthy ASG members: %s',
                    resource_tags, ', '.join(outside))
        return 'PRECHECK_TARGET_ASG_UNMANAGED_INSTANCE'
    untagged = sorted(asg_instance_ids - running)
    if untagged:
        logger.info('Healthy ASG members without the tags %s, which FIS cannot pick: %s',
                    resource_tags, ', '.join(untagged))
        return 'PRECHECK_TARGET_ASG_UNTAGGED_INSTANCE'
    return None


def check_asg_healthy(asg_name: str, min_in_service: int, label: str,  # pylint: disable=too-many-return-statements
                      resource_tags: dict[str, str] | None = None) -> str | None:
    """
    Fail unless the Auto Scaling Group is at desired capacity, healthy, settled and idle.

    ``resource_tags`` are the FIS template's target tags for the target ASG, whose instances FIS selects by tag.
    """
    autoscaling = _client('autoscaling')
    groups = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])['AutoScalingGroups']
    if not groups:
        return f'PRECHECK_{label}_NOT_FOUND'
    group = groups[0]
    healthy = [
        instance['InstanceId'] for instance in group['Instances']
        if instance['LifecycleState'] == 'InService' and instance['HealthStatus'] == 'Healthy'
    ]
    desired = group['DesiredCapacity']
    logger.info('ASG %s: %d healthy InService of %d desired', asg_name, len(healthy), desired)
    if len(healthy) < desired:
        return f'PRECHECK_{label}_DEGRADED'
    if len(healthy) < min_in_service:
        return f'PRECHECK_{label}_BELOW_MIN_IN_SERVICE'
    # E.g. an instance in Standby with the desired capacity decremented still runs and can be selected by FIS.
    others = [f"{i['InstanceId']} {i['LifecycleState']}/{i['HealthStatus']}"
              for i in group['Instances'] if i['InstanceId'] not in healthy]
    if others:
        logger.info('ASG %s has instances that are not healthy and InService: %s', asg_name, ', '.join(others))
        return f'PRECHECK_{label}_INSTANCE_NOT_IN_SERVICE'
    reason = check_instances_settled(healthy, group.get('HealthCheckGracePeriod', 0), label)
    if reason:
        return reason
    if resource_tags:
        reason = check_fis_targets_in_asg(resource_tags, set(healthy))
        if reason:
            return reason
    activities = autoscaling.describe_scaling_activities(AutoScalingGroupName=asg_name, MaxRecords=10)['Activities']
    if any(activity['StatusCode'] not in TERMINAL_SCALING_ACTIVITY_STATUSES for activity in activities):
        return f'PRECHECK_{label}_SCALING_IN_PROGRESS'
    return None


def check_stop_condition_alarms(alarm_names: tuple[str, ...]) -> str | None:
    """
    Fail unless every stop condition alarm is OK.

    FIS refuses to run an experiment whose stop condition is not OK, but only
    after the day has been claimed. Checking first lets a later run retry.
    """
    if not alarm_names:
        return None
    response = _client('cloudwatch').describe_alarms(
        AlarmNames=list(alarm_names), AlarmTypes=['MetricAlarm', 'CompositeAlarm'])
    states = {
        alarm['AlarmName']: alarm['StateValue']
        for alarm in response.get('MetricAlarms', []) + response.get('CompositeAlarms', [])
    }
    for name in alarm_names:
        if name not in states:
            logger.warning('Stop condition alarm %s does not exist', name)
            return 'PRECHECK_STOP_CONDITION_ALARM_NOT_FOUND'
        if states[name] != 'OK':
            logger.info('Stop condition alarm %s is %s', name, states[name])
            return 'PRECHECK_STOP_CONDITION_ALARM'
    return None


def check_no_active_experiment(template_ids: set[str]) -> str | None:
    """Fail if an experiment from any of the configured templates is still active."""
    fis = _client('fis')
    for template_id in sorted(template_ids):
        kwargs: dict[str, Any] = {'experimentTemplateId': template_id, 'maxResults': 100}
        while True:
            response = fis.list_experiments(**kwargs)
            for experiment in response.get('experiments', []):
                if experiment.get('state', {}).get('status') in ACTIVE_EXPERIMENT_STATUSES:
                    logger.info('Experiment %s is still active', experiment.get('id'))
                    return 'PRECHECK_EXPERIMENT_ACTIVE'
            if not response.get('nextToken'):
                break
            kwargs['nextToken'] = response['nextToken']
    return None


def run_prechecks(config: EngineConfig, target: Target) -> str | None:
    """Return the reason for the first failing pre-check, or None if all pass."""
    reason = check_stack_stable(config.cloudformation_stack_name)
    if reason:
        return reason
    template_target = read_template_target(target.fis_template_id)
    if isinstance(template_target, str):
        return template_target
    reason = check_asg_healthy(target.asg_name, target.min_in_service, 'TARGET_ASG',
                               resource_tags=template_target.resource_tags)
    for required in target.required_healthy:
        if reason:
            break
        reason = check_asg_healthy(required.asg_name, required.min_in_service, 'REQUIRED_ASG')
    if reason is None:
        reason = check_stop_condition_alarms(template_target.stop_condition_alarm_names)
    if reason is None:
        reason = check_no_active_experiment({t.fis_template_id for t in config.all_targets})
    return reason


def client_token(seed_prefix: str, scheduled_time: str | None, context: Any) -> str:
    """
    Build the FIS idempotency token.

    All invocations for the same schedule slot share a token, so FIS starts at
    most one experiment for them. ``seed_prefix`` keeps the tokens of different
    deployments apart.
    """
    seed = scheduled_time or getattr(context, 'aws_request_id', None) or str(uuid4())
    return hashlib.sha256(f'{seed_prefix}:{seed}'.encode()).hexdigest()


def start_experiment(config: EngineConfig, target: Target, token: str, local_date: str) -> str:
    """Start the target's FIS experiment and return the experiment id."""
    response = _client('fis').start_experiment(
        clientToken=token,
        experimentTemplateId=target.fis_template_id,
        tags={
            **config.experiment_tags,
            'ChaosTarget': target.name,
            'TriggeredBy': TRIGGERED_BY,
            'LocalDate': local_date,
        },
    )
    return response['experiment']['id']


def _decision(config: EngineConfig, status: str, reason: str, **details: Any) -> dict:
    """Log the decision as a CloudWatch Embedded Metric Format record and return it."""
    dimensions = dict(config.metric_dimensions)
    record = {
        '_aws': {
            'Timestamp': int(time.time() * 1000),
            'CloudWatchMetrics': [{
                'Namespace': config.metric_namespace,
                'Dimensions': [[*dimensions, 'Status'], [*dimensions, 'Reason']],
                'Metrics': [{'Name': 'Decisions', 'Unit': 'Count'}],
            }],
        },
        **dimensions,
        'Status': status,
        'Reason': reason,
        'Decisions': 1,
        **details,
    }
    # EMF records must be written to stdout as raw JSON lines.
    print(json.dumps(record, default=str))
    return {'status': status, 'reason': reason, **details}


def run(config: EngineConfig, settings: Settings,  # pylint: disable=too-many-locals,too-many-return-statements
        event: Any = None, context: Any = None) -> dict:
    """Decide on one run with an already parsed configuration. Each return is one decision outcome."""
    event = event if isinstance(event, dict) else {}
    if not config.enabled:
        return _decision(config, STATUS_SKIPPED, 'ENGINE_DISABLED')

    now = _now(config.timezone)
    today = now.date()
    scheduled_time = event.get('scheduledTime')
    details: dict[str, Any] = {'localDate': today.isoformat(), 'scheduledTime': scheduled_time}

    claims = list_claims(settings.state_parameter_path)
    prune_claims(claims, today, config.retention_days)
    if today in claims:
        return _decision(config, STATUS_SKIPPED, 'DAILY_LIMIT_REACHED', **details)

    roll = _rng.random()
    threshold = config.run_probability
    details.update(roll=round(roll, 4), threshold=round(threshold, 4))
    if roll >= threshold:
        return _decision(config, STATUS_SKIPPED, 'PROBABILITY_MISSED', **details)

    target = choose_target(config.targets)
    details.update(target=target.name, asgName=target.asg_name)

    reason = run_prechecks(config, target)
    if reason:
        return _decision(config, STATUS_SKIPPED, reason, **details)

    if config.dry_run:
        return _decision(config, STATUS_DRY_RUN, 'WOULD_TERMINATE', **details)

    claim_name = f'{settings.state_parameter_path}/{today.isoformat()}'
    claim = {
        'status': 'CLAIMED',
        'claimedAt': now.isoformat(),
        'target': target.name,
        'asgName': target.asg_name,
        'scheduledTime': scheduled_time,
        'experimentId': None,
    }
    if not create_claim(claim_name, claim):
        return _decision(config, STATUS_SKIPPED, 'DAILY_LIMIT_REACHED', **details)

    token = client_token(settings.config_parameter_name, scheduled_time, context)
    try:
        experiment_id = start_experiment(config, target, token, today.isoformat())
    except ClientError as err:
        # Keep the claim: no further termination is attempted today.
        update_claim(claim_name, {**claim, 'status': 'FAILED', 'error': str(err)})
        _decision(config, STATUS_FAILED, 'FIS_START_FAILED', error=str(err), **details)
        raise

    update_claim(claim_name, {**claim, 'status': 'STARTED', 'experimentId': experiment_id})
    return _decision(config, STATUS_EXECUTED, 'TERMINATION_STARTED', experimentId=experiment_id, **details)


def handler(event: Any, context: Any) -> dict:
    """Lambda entry point: reads the settings and the configuration, then decides on one run."""
    settings = Settings.from_env()
    config = load_config(settings.config_parameter_name)
    return run(config, settings, event, context)
