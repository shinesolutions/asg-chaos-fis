# ASG Chaos FIS

A Lambda that tests whether Auto Scaling Groups recover on their own: at most once a day, it terminates one random instance of one of the configured Auto Scaling Groups, using an [AWS Fault Injection Service (FIS)](https://docs.aws.amazon.com/fis/latest/userguide/what-is.html) experiment template.

It works like the [Simian Army](https://github.com/Netflix/SimianArmy) Chaos Monkey, without a dedicated EC2 instance:

1. **A scheduler**, such as Amazon EventBridge Scheduler, invokes the Lambda several times each day
2. **The Lambda** (this repository) decides whether to terminate an instance today, and from which Auto Scaling Group
3. **FIS** terminates one running instance of that Auto Scaling Group

The Auto Scaling Group then replaces the instance.

The library only knows about Auto Scaling Groups and FIS experiment templates. The deployment creates the schedule, the experiment templates, the IAM roles and the [configuration](#configuration-parameter), which holds all application-specific knowledge: which Auto Scaling Groups may be targeted, and which others must be healthy first. For example, [aem-aws-stack-builder](https://github.com/shinesolutions/aem-aws-stack-builder) deploys it as the AEM Chaos Engine.

## Safety guardrails

* **At most one termination per day, across all targets.** The local calendar day (in the configured timezone) is claimed by creating an SSM parameter with `Overwrite=False`, which succeeds for only one caller. The claim is created *before* FIS is called and is kept even if FIS fails, so neither a failure nor a duplicate invocation can lead to a second termination on the same day.
* **Only experiment templates whose selection the pre-checks fully cover.** A target's template must have exactly one target that selects one (`COUNT(1)`) running EC2 instance by tags, with no other filter and no instance ARNs. Otherwise the run is skipped with `PRECHECK_FIS_TEMPLATE_UNSUPPORTED`.
* **Pre-checks before every termination.** The engine skips the run unless:
  * the CloudFormation stack in `cloudFormationStackName`, if set, is in a stable `*_COMPLETE` state
  * the target Auto Scaling Group has all desired instances healthy and InService, at least `minInService` of them, **no instance in any other state** (for example `Standby`, which may not count towards the desired capacity), and no scaling activity in progress
  * every one of those instances is running in EC2, and was launched longer ago than the Auto Scaling Group's health check grace period. The Auto Scaling Group's own view lags behind: it notices a terminated instance only after about a minute, and it reports new instances as healthy without checking them during the grace period
  * the running instances that FIS could select (by all of the template's target tags) are exactly those instances. This catches, for example, an instance detached from the Auto Scaling Group but still running, and an Auto Scaling Group member without the tags, which FIS could never select. A template that matches no instance at all is skipped too, instead of failing after the day has been claimed
  * every Auto Scaling Group in the target's `requiredHealthy` list passes the same checks, with its own `minInService`. Use this when a termination affects other Auto Scaling Groups too, for example when another component replaces a paired instance
  * every stop condition alarm of the target's template is `OK`. FIS also refuses to run an experiment whose stop condition is not `OK`, but only after the day has been claimed, so checking first lets a later run that day try again. Stop conditions only stop an experiment from starting: a terminate action finishes in seconds, so they cannot stop one in progress
  * no experiment from the configured templates is still active
* **Idempotent FIS calls.** Every invocation for the same schedule slot sends the same FIS `clientToken`.
* **Dry run.** With `dryRun` enabled, the engine logs what it would terminate without claiming the day or calling FIS.

### What decides which instances can be terminated

The configuration has no list of allowed targets. Which instances can be terminated is decided by IAM, which the deployment controls:

* The FIS role, attached to the experiment templates, should only allow `ec2:TerminateInstances` on instances with the targets' tags.
* The Lambda role should only allow `fis:StartExperiment` on the deployment's own experiment templates.

Someone who can change the SSM configuration can therefore choose between the deployment's templates, but cannot widen either boundary.

## How a run decides

| Step | Outcome when the step stops the run |
|------|-------------------------------------|
| Load and validate the configuration | Error (the Lambda fails, so a missing, invalid or unsupported configuration is noticed) |
| `enabled` is `false` | `SKIPPED` / `ENGINE_DISABLED` |
| Today is already claimed | `SKIPPED` / `DAILY_LIMIT_REACHED` |
| Probability roll misses | `SKIPPED` / `PROBABILITY_MISSED` |
| Weighted random choice of one target, then pre-checks | `SKIPPED` / `PRECHECK_*` |
| `dryRun` is `true` | `DRY_RUN` / `WOULD_TERMINATE` |
| Claim today; another invocation claimed it first | `SKIPPED` / `DAILY_LIMIT_REACHED` |
| Start the FIS experiment | `EXECUTED` / `TERMINATION_STARTED`, or `FAILED` / `FIS_START_FAILED` followed by an error |

The probability roll works like Simian Army's: `dailyProbability` is spread over the day's runs, so each run attempts a termination with probability `dailyProbability / runsPerDay`.

* `1.0` means one termination attempt per day on average. With 6 runs per day, about 2 in 3 days see a termination.
* A value equal to `runsPerDay` means the first run of each day that passes the pre-checks terminates an instance.

The pre-check reasons are `PRECHECK_STACK_NOT_FOUND`, `PRECHECK_STACK_NOT_STABLE`, `PRECHECK_FIS_TEMPLATE_NOT_FOUND`, `PRECHECK_FIS_TEMPLATE_UNSUPPORTED`, `PRECHECK_TARGET_ASG_*` and `PRECHECK_REQUIRED_ASG_*` (`NOT_FOUND`, `DEGRADED`, `BELOW_MIN_IN_SERVICE`, `INSTANCE_NOT_IN_SERVICE`, `INSTANCE_NOT_RUNNING`, `IN_GRACE_PERIOD`, `SCALING_IN_PROGRESS`, and for the target only `UNMANAGED_INSTANCE` and `UNTAGGED_INSTANCE`), `PRECHECK_STOP_CONDITION_ALARM`, `PRECHECK_STOP_CONDITION_ALARM_NOT_FOUND` and `PRECHECK_EXPERIMENT_ACTIVE`.

## Configuration

### Environment variables

| Name | Description |
|------|-------------|
| `CONFIG_PARAMETER_NAME` | SSM parameter holding the JSON configuration. Also keeps the FIS idempotency tokens of different deployments apart |
| `STATE_PARAMETER_PATH` | SSM path for the daily claims |
| `LOG_LEVEL` | Optional, defaults to `INFO` |

All variables except `LOG_LEVEL` are required.

### Configuration parameter

The configuration is a JSON document in SSM Parameter Store. See [conf/sample-config.json](conf/sample-config.json).

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `schemaVersion` | yes | | Must be `1`. A configuration written for another version fails the invocation |
| `enabled` | yes | | Turns the engine on or off |
| `dryRun` | no | `false` | Log decisions without claiming the day or calling FIS |
| `timezone` | yes | | IANA timezone that defines the local day, e.g. `Australia/Sydney` |
| `dailyProbability` | yes | | Expected termination attempts per day, from `0` to `runsPerDay` |
| `runsPerDay` | yes | | Number of scheduled runs per day |
| `retentionDays` | no | `7` | Days of claims to keep in SSM |
| `cloudFormationStackName` | no | | Stack that must be stable before a termination |
| `metricNamespace` | no | `ASGChaosFIS` | CloudWatch namespace of the decision metric |
| `metricDimensions` | no | `{}` | Dimensions added to the decision metric, e.g. `{"App": "demo"}`. Up to 29 |
| `experimentTags` | no | `{}` | Tags added to every experiment, besides `ChaosTarget`, `TriggeredBy` and `LocalDate`. Up to 47 |
| `targets` | yes, if enabled | | List of targets, each name at most once. At least one must be enabled when the engine is enabled |
| `targets[].name` | yes | | Name used in logs, claims and the `ChaosTarget` experiment tag |
| `targets[].enabled` | no | `true` | Disabled targets are validated but never chosen |
| `targets[].asgName` | yes | | Target Auto Scaling Group name |
| `targets[].fisTemplateId` | yes | | FIS experiment template that terminates one instance of this Auto Scaling Group. Its target tags and stop condition alarms are read at run time |
| `targets[].weight` | no | `1` | Relative chance of this target being chosen |
| `targets[].minInService` | no | `2` | Healthy InService instances required before a termination. The default of 2 means the last instance is never terminated |
| `targets[].requiredHealthy` | no | `[]` | Other Auto Scaling Groups that must pass the same checks before a termination |
| `targets[].requiredHealthy[].asgName` | yes | | Auto Scaling Group name |
| `targets[].requiredHealthy[].minInService` | no | `1` | Healthy InService instances it requires |

### Experiment templates

Each target's template must:

* have exactly one target, with `resourceType` `aws:ec2:instance`, `resourceTags`, `selectionMode` `COUNT(1)`, a single `State.Name` filter with the value `running`, and no `resourceArns`
* only have stop conditions from `aws:cloudwatch:alarm`, or none

The instances with the target tags must be exactly the Auto Scaling Group's instances, for example by propagating the tags from the Auto Scaling Group at launch. The `UNMANAGED_INSTANCE` and `UNTAGGED_INSTANCE` pre-checks skip the run if they are not.

### Invocation event

EventBridge Scheduler should send:

```json
{"scheduledTime": "<aws.scheduler.scheduled-time>", "executionId": "<aws.scheduler.execution-id>"}
```

`scheduledTime` is used for the FIS idempotency token. Without it, the Lambda request id is used instead.

### Daily claims

Each claimed day is an SSM parameter under `STATE_PARAMETER_PATH`, named after its local date, for example `/demo/chaos/kills/2026-10-14`. Its value records the chosen target, the experiment id and the status (`CLAIMED`, `STARTED` or `FAILED`). Claims older than `retentionDays` are deleted on each run.

### Deployment settings

* Runtime `python3.12`, handler `asg_chaos_fis.handler`
* Asynchronous invocation retries `0`, and schedule retries `0`

Reserved concurrency is not needed: the atomic daily claim and the FIS `clientToken` already allow only one termination per day, even when invocations overlap.

### Using the library from your own handler

`handler()` reads the settings from the environment and the configuration from SSM, then calls `run()`. A deployment that builds its configuration in code can call `run()` itself:

```python
import asg_chaos_fis

def handler(event, context):
    settings = asg_chaos_fis.Settings.from_env()
    config = asg_chaos_fis.parse_config(build_config())  # the same JSON structure as the SSM parameter
    return asg_chaos_fis.run(config, settings, event, context)
```

## IAM permissions

The Lambda execution role needs:

| Action | Resource |
|--------|----------|
| `ssm:GetParameter` | The configuration parameter |
| `ssm:GetParametersByPath` | `STATE_PARAMETER_PATH` |
| `ssm:PutParameter`, `ssm:DeleteParameters` | `STATE_PARAMETER_PATH/*` |
| `fis:GetExperimentTemplate` | The targets' experiment templates |
| `fis:StartExperiment` | The targets' experiment templates, and `experiment/*` |
| `fis:TagResource` | `experiment/*` |
| `fis:ListExperiments` | `*` |
| `autoscaling:DescribeAutoScalingGroups`, `autoscaling:DescribeScalingActivities`, `ec2:DescribeInstances` | `*` |
| `cloudwatch:DescribeAlarms` | `*` (AWS authorises this action against `alarm:*`, so it cannot be limited to specific alarms) |
| `cloudformation:DescribeStacks` | The stack in `cloudFormationStackName`, if set |
| `logs:CreateLogStream`, `logs:PutLogEvents` | The function's log group |

`iam:PassRole` is not needed, because the FIS role is attached to the experiment templates.

## Metrics

Every run writes one decision record to stdout in [CloudWatch Embedded Metric Format](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format.html). This creates the `Decisions` metric in `metricNamespace`, with the `metricDimensions` plus either `Status` or `Reason`, without needing `cloudwatch:PutMetricData`.

## Development

Requirements: Python 3.12 and `zip`.

```shell
python3.12 -m venv .venv
source .venv/bin/activate
make ci
```

`make ci` runs `clean`, `deps`, `lint` (pylint), `test` (pytest) and `package`. The package is created at `stage/asg-chaos-fis-<version>.zip`, with its checksum in `stage/asg-chaos-fis-<version>.zip.sha256`. It contains only `asg_chaos_fis.py`, because boto3 is provided by the Lambda runtime.

The tests use botocore Stubbers, so they make no AWS calls and check every request and response against the AWS API models.

[test/integration](test/integration/README.md) contains integration tests that run the engine against a test Auto Scaling Group in a real AWS account.

## Release

Run the `Release Patch`, `Release Minor` or `Release Major` GitHub workflow. It updates `Makefile` and `CHANGELOG.md`, and tags the release. The `Publish` workflow then attaches `asg-chaos-fis-<version>.zip` and `asg-chaos-fis-<version>.zip.sha256` to the GitHub release.

aem-aws-stack-builder downloads that zip using its `library.asg_chaos_fis_version` configuration.
