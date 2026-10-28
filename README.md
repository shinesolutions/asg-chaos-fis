AEM Chaos Engine
----------------

A serverless replacement for the [Simian Army](https://github.com/Netflix/SimianArmy) Chaos Monkey used by [AEM OpenCloud](https://github.com/shinesolutions/aem-aws-stack-builder) full-set stacks.

Instead of a dedicated EC2 instance running Simian Army, the engine uses:

1. **Amazon EventBridge Scheduler** to invoke the engine several times each business day
2. **AWS Lambda** (this repository) to decide whether to terminate an instance today, and which one
3. **AWS Fault Injection Service (FIS)** to terminate one running instance of the chosen Auto Scaling Group

The Auto Scaling Group then replaces the instance, which tests that the stack recovers on its own.

The schedule, FIS experiment templates, IAM roles and configuration are created by [aem-aws-stack-builder](https://github.com/shinesolutions/aem-aws-stack-builder). This repository only contains the Lambda.

Safety guardrails
-----------------

* **At most one termination per day, across all targets.** The local calendar day (in the configured timezone) is claimed by creating an SSM parameter with `Overwrite=False`, which succeeds for only one caller. The claim is created *before* FIS is called and is kept even if FIS fails, so neither a failure nor a duplicate invocation can lead to a second termination on the same day.
* **Only `author-dispatcher`, `publish` and `publish-dispatcher` can be targeted.** Any other component in the configuration is rejected. In particular, `author-primary` and `author-standby` are never targeted.
* **Publish and publish-dispatcher are terminated in pairs.** When one instance of a publish / publish-dispatcher pair is terminated, the AEM orchestrator terminates the other one too, then pairs the replacements. So a `publish` or `publish-dispatcher` termination takes down one instance of each, and the pre-checks cover both Auto Scaling Groups and the orchestrator.
* **Pre-checks before every termination.** The engine skips the run unless:
  * the CloudFormation stack is in a stable `*_COMPLETE` state
  * the target Auto Scaling Group has all desired instances healthy and InService, at least `minInService` of them, **no instance in any other state** (for example `Standby`, which may not count towards the desired capacity), and no scaling activity in progress
  * every one of those instances is running in EC2, and was launched longer ago than the Auto Scaling Group's health check grace period. The Auto Scaling Group's own view lags behind: it notices a terminated instance only after about a minute, and it reports new instances as healthy without checking them during the grace period
  * every running instance that FIS could select (by the target's `StackPrefix` and `Component` tags) is one of those instances. This catches, for example, an instance detached from the Auto Scaling Group but still running
  * for `publish` and `publish-dispatcher`, the paired Auto Scaling Group passes the same checks (with the same `minInService`), and so does the orchestrator Auto Scaling Group
  * every stop condition alarm is `OK`. FIS also refuses to run an experiment whose stop condition is not `OK`, but only after the day has been claimed, so checking first lets a later run that day try again. Stop conditions only stop an experiment from starting: a terminate action finishes in seconds, so they cannot stop one in progress
  * no experiment from the engine's FIS templates is still active
* **Idempotent FIS calls.** Every invocation for the same schedule slot sends the same FIS `clientToken`.
* **Dry run.** With `dryRun` enabled, the engine logs what it would terminate without claiming the day or calling FIS.

How a run decides
-----------------

| Step | Outcome when the step stops the run |
|------|-------------------------------------|
| Load and validate the configuration | Error (the Lambda fails, so a missing or invalid configuration is noticed) |
| `enabled` is `false` | `SKIPPED` / `ENGINE_DISABLED` |
| Today is already claimed | `SKIPPED` / `DAILY_LIMIT_REACHED` |
| Probability roll misses | `SKIPPED` / `PROBABILITY_MISSED` |
| Weighted random choice of one target, then pre-checks | `SKIPPED` / `PRECHECK_*` |
| `dryRun` is `true` | `DRY_RUN` / `WOULD_TERMINATE` |
| Claim today; another invocation claimed it first | `SKIPPED` / `DAILY_LIMIT_REACHED` |
| Start the FIS experiment | `EXECUTED` / `TERMINATION_STARTED`, or `FAILED` / `FIS_START_FAILED` followed by an error |

The probability roll works like Simian Army's: `dailyProbability` is spread over the day's runs, so each run attempts a termination with probability `dailyProbability / runsPerDay`.

* `1.0` means one termination attempt per day on average. With 6 runs per day, about 2 in 3 business days see a termination.
* A value equal to `runsPerDay` means the first run of each day that passes the pre-checks terminates an instance.

Configuration
-------------

### Environment variables

| Name | Description |
|------|-------------|
| `STACK_PREFIX` | AEM stack prefix, used in tags, metrics and the FIS idempotency token |
| `CONFIG_PARAMETER_NAME` | SSM parameter holding the JSON configuration, e.g. `/aem-opencloud/<stack_prefix>/chaos-engine/config` |
| `STATE_PARAMETER_PATH` | SSM path for the daily claims, e.g. `/aem-opencloud/<stack_prefix>/chaos-engine/kills` |
| `LOG_LEVEL` | Optional, defaults to `INFO` |

All variables except `LOG_LEVEL` are required.

### Configuration parameter

The configuration is a JSON document in SSM Parameter Store. See [conf/sample-config.json](conf/sample-config.json).

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `enabled` | yes | | Turns the engine on or off |
| `dryRun` | no | `false` | Log decisions without claiming the day or calling FIS |
| `timezone` | yes | | IANA timezone that defines the local day, e.g. `Australia/Sydney` |
| `dailyProbability` | yes | | Expected termination attempts per day, from `0` to `runsPerDay` |
| `runsPerDay` | yes | | Number of scheduled runs per day |
| `retentionDays` | no | `7` | Days of claims to keep in SSM |
| `cloudFormationStackName` | yes | | Stack that must be stable before a termination |
| `orchestratorAsgName` | if `publish` or `publish-dispatcher` is enabled | | Orchestrator Auto Scaling Group checked before a `publish` or `publish-dispatcher` termination |
| `stopConditionAlarmNames` | no | `[]` | CloudWatch alarms that must be `OK` before a termination, normally the FIS templates' stop condition alarms. Up to 100 |
| `targets` | yes, if enabled | | List of targets, each component at most once. At least one must be enabled when the engine is enabled |
| `targets[].component` | yes | | `author-dispatcher`, `publish` or `publish-dispatcher`. An enabled `publish` or `publish-dispatcher` target needs the other one listed too, enabled or not, for the paired Auto Scaling Group check |
| `targets[].enabled` | no | `true` | Disabled targets are validated but never chosen |
| `targets[].asgName` | yes | | Target Auto Scaling Group name |
| `targets[].fisTemplateId` | yes | | FIS experiment template that terminates one instance of this Auto Scaling Group |
| `targets[].weight` | no | `1` | Relative chance of this target being chosen |
| `targets[].minInService` | no | `2` | Healthy InService instances required before a termination. The default of 2 means the last instance is never terminated |

### Invocation event

EventBridge Scheduler should send:

```json
{"scheduledTime": "<aws.scheduler.scheduled-time>", "executionId": "<aws.scheduler.execution-id>"}
```

`scheduledTime` is used for the FIS idempotency token. Without it, the Lambda request id is used instead.

### Daily claims

Each claimed day is an SSM parameter named after its local date, for example `/aem-opencloud/sandpit/chaos-engine/kills/2026-10-14`. Its value records the chosen component, the experiment id and the status (`CLAIMED`, `STARTED` or `FAILED`). Claims older than `retentionDays` are deleted on each run.

### Deployment settings

The Lambda relies on these settings in aem-aws-stack-builder:

* Runtime `python3.12`, handler `aem_chaos_engine.handler`
* Asynchronous invocation retries `0`, and schedule retries `0`

Reserved concurrency is not needed: the atomic daily claim and the FIS `clientToken` already allow only one termination per day, even when invocations overlap.

IAM permissions
---------------

The Lambda execution role needs:

| Action | Resource |
|--------|----------|
| `ssm:GetParameter` | The configuration parameter |
| `ssm:GetParametersByPath` | `STATE_PARAMETER_PATH` |
| `ssm:PutParameter`, `ssm:DeleteParameters` | `STATE_PARAMETER_PATH/*` |
| `fis:StartExperiment` | The targets' experiment templates, and `experiment/*` |
| `fis:TagResource` | `experiment/*` |
| `fis:ListExperiments` | `*` |
| `autoscaling:DescribeAutoScalingGroups`, `autoscaling:DescribeScalingActivities`, `ec2:DescribeInstances` | `*` |
| `cloudwatch:DescribeAlarms` | `*` (AWS authorises this action against `alarm:*`, so it cannot be limited to specific alarms) |
| `cloudformation:DescribeStacks` | The stack in `cloudFormationStackName` |
| `logs:CreateLogStream`, `logs:PutLogEvents` | The function's log group |

`iam:PassRole` is not needed, because the FIS role is attached to the experiment templates.

Metrics
-------

Every run writes one decision record to stdout in [CloudWatch Embedded Metric Format](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format.html). This creates the `Decisions` metric in the `AEMOpenCloud/ChaosEngine` namespace, with dimensions `StackPrefix` plus either `Status` or `Reason`, without needing `cloudwatch:PutMetricData`.

Development
-----------

Requirements: Python 3.12 and `zip`.

```
python3.12 -m venv .venv
source .venv/bin/activate
make ci
```

`make ci` runs `clean`, `deps`, `lint` (pylint), `test` (pytest) and `package`. The package is created at `stage/aem-chaos-engine-<version>.zip`. It contains only `aem_chaos_engine.py`, because boto3 is provided by the Lambda runtime.

The tests use botocore Stubbers, so they make no AWS calls and check every request and response against the AWS API models.

[test/integration](test/integration/README.md) contains integration tests that run the engine against a non-AEM test Auto Scaling Group in a real AWS account.

Release
-------

Run the `Release Patch`, `Release Minor` or `Release Major` GitHub workflow. It updates `Makefile` and `CHANGELOG.md`, and tags the release. The `Publish` workflow then attaches `aem-chaos-engine-<version>.zip` to the GitHub release.

aem-aws-stack-builder downloads that zip using its `library.aem_chaos_engine_version` configuration.
