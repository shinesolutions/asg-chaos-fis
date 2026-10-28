Integration tests
-----------------

These tests run the chaos engine against real AWS services before it is deployed with aem-aws-stack-builder. They deploy a throwaway harness stack ([harness.yaml](harness.yaml)) that points the engine at an existing **non-AEM** test Auto Scaling Group, then invoke the Lambda and check the results.

The `kill` step terminates one instance of the test Auto Scaling Group. Every other step terminates nothing.

### What the harness creates

* The chaos engine Lambda, with no retries. The stack creates it with placeholder code; `deploy` then uploads the locally built zip straight to the function, so no S3 bucket is needed
* A FIS experiment template that terminates one running instance tagged with **both** the target `StackPrefix` and `Component`
* A stop condition alarm on the test load balancer's `UnHealthyHostCount`
* IAM roles for the Lambda, FIS and, during the scheduler test, EventBridge Scheduler
* The engine configuration in SSM, at `/aem-opencloud/<ENGINE_STACK_PREFIX>/chaos-engine/config`

### Safety guards

The engine labels the target `author-dispatcher` in its configuration, because it only accepts AEM component names. The label is only used for validation, logs and tags. **Which instances can be terminated is decided by FIS and IAM, never by the label:**

1. The FIS template only selects instances that have both target tags.
2. The FIS role may only terminate instances that have both target tags. Even a wrong template would be refused by IAM.
3. The Lambda role may only start the harness experiment template.
4. `preflight`, which also runs before `deploy` and `kill`, stops if `TARGET_COMPONENT` is an AEM component name, or if any instance with the target tags is outside the test Auto Scaling Group.
5. `kill` asks you to type `TERMINATE` before it continues.

The `iam` step checks guards 2 and 3 with the IAM policy simulator.

### Prerequisites

* AWS CLI v2, logged in to the test account (`aws login`). The termination tests take up to about 30 minutes, so log in again just before running them.
* Permission to create IAM roles, Lambda functions, FIS templates, alarms and SSM parameters, and to use the IAM policy simulator
* Python 3, `zip`, `openssl` and `base64`
* A test Auto Scaling Group with at least 2 healthy instances, behind a classic load balancer, created by a CloudFormation stack

### Setup

```
cp test/integration/integration.env.sample test/integration/integration.env
```

Fill in `test/integration/integration.env`. The file is ignored by git.

| Setting | Description |
|---------|-------------|
| `AWS_REGION` | Region of the test Auto Scaling Group |
| `HARNESS_STACK_NAME` | Name of the harness stack |
| `ENGINE_STACK_PREFIX` | Stack prefix the engine runs as. Must not be the prefix of a real stack |
| `TARGET_STACK_PREFIX`, `TARGET_COMPONENT` | Tags of the instances that may be terminated |
| `TARGET_ASG_NAME` | Full name of the test Auto Scaling Group |
| `TARGET_STACK_NAME` | CloudFormation stack of the test Auto Scaling Group |
| `TARGET_LOAD_BALANCER_NAME` | Classic load balancer in front of the test Auto Scaling Group |

### Running the tests

Run the steps one at a time from the repository root, in this order. Each step prints `[PASS]` or `[FAIL]` lines and a summary.

| Step | Command | Terminates an instance? | Duration |
|------|---------|-------------------------|----------|
| 1 | `test/integration/run-integration-tests.sh preflight` | No, and changes nothing | Seconds |
| 2 | `test/integration/run-integration-tests.sh deploy` | No | ~3 min |
| 3 | `test/integration/run-integration-tests.sh iam` | No, and changes nothing | Seconds |
| 4 | `test/integration/run-integration-tests.sh dry-run` | No | ~3 min, plus up to 20 min if an instance was replaced recently |
| 5 | `test/integration/run-integration-tests.sh scheduler` | No | ~8 min |
| 6 | `test/integration/run-integration-tests.sh kill` | **Yes, one** | ~15–40 min, mostly waiting for the replacement to serve traffic |
| 7 | `test/integration/run-integration-tests.sh teardown` | No | ~2 min |

`all` runs steps 1 to 6 in one go, still asking for confirmation before step 6.

### What each step checks

**preflight**: you are logged in; the target is not an AEM component; every instance with the target tags belongs to the test Auto Scaling Group. Lists the instances that may be terminated.

**deploy**: packages `lambda/aem_chaos_engine.py`, deploys the harness in dry run mode, uploads the zip straight to the function (`aws lambda update-function-code`), and checks that the deployed code matches the local zip. Run `deploy` again after changing the Lambda code. Later stack updates by the other steps leave the uploaded code in place.

**iam**: the FIS role can terminate an instance with the target tags, but not one with another `Component`, another `StackPrefix` or no tags. The Lambda role can start the harness template only, cannot terminate instances itself, and can read the stop condition alarm.

**dry-run**: first waits until the test Auto Scaling Group is healthy in both the Auto Scaling Group and the load balancer, and past the health check grace period. This can take up to 20 minutes if an instance was replaced recently. Then:
* A dry run returns `DRY_RUN` / `WOULD_TERMINATE`, claims nothing and terminates nothing.
* With `MinInService` raised to 3, the pre-check returns `PRECHECK_TARGET_ASG_BELOW_MIN_IN_SERVICE`.
* With the stop condition alarm forced into `ALARM`, the engine returns `PRECHECK_STOP_CONDITION_ALARM` and does not claim the day.

**scheduler**: creates a one-off EventBridge Scheduler schedule 4 minutes ahead and checks that the engine is invoked with the scheduled time as input.

**kill** (`DryRun=false`). Like `dry-run`, it first waits until the test Auto Scaling Group is healthy and past the grace period.
1. **FIS backstop**: with the stop condition alarm forced into `ALARM`, the script starts the FIS template directly, bypassing the engine's own alarm check. The experiment does not complete and no instance is terminated.
2. A real run returns `EXECUTED`, and the day's claim records the experiment id.
3. A second run returns `DAILY_LIMIT_REACHED`.
4. The experiment completes and exactly one instance is terminated.
5. **Right after the termination**, before the Auto Scaling Group has noticed (about a minute), a run is blocked by a `PRECHECK_TARGET_ASG_*` check. The script prints whether the new EC2 check (`PRECHECK_TARGET_ASG_INSTANCE_NOT_RUNNING`) caught it.
6. **While the replacement is in the health check grace period**, a run returns `PRECHECK_TARGET_ASG_IN_GRACE_PERIOD`.
7. The load balancer reports all instances `InService` again. The script prints how long recovery took after the termination.

Tests 5 and 6 delete the day's claim so that the run reaches the pre-checks. They run with dry run switched on directly in SSM, which takes seconds, rather than through a stack update. So even if a pre-check failed to block, nothing would be terminated. The real engine never deletes the current day's claim.

At the end, the harness is switched back to dry run mode. If you stop the script part way, nothing invokes the engine by itself, because the harness has no schedule outside the `scheduler` step. Run `dry-run` or `teardown` afterwards to reset it.

**teardown**: deletes the harness stack, including the function and its code, and the claims. FIS experiment history cannot be deleted and expires after 120 days.

### Troubleshooting

* **`Not logged in to AWS`**: run `aws login`. If the session expires during `kill`, log in again and rerun `kill`. The script checks that the Auto Scaling Group is healthy before terminating anything.
* **`Runtime.ImportModuleError` when a test invokes the Lambda**: the function still has the placeholder code, for example because `deploy` stopped before the upload. Run `deploy` again.
* **Stack in `ROLLBACK_COMPLETE` after a failed first deploy**: run `teardown`, then `deploy` again.
* **The FIS backstop test fails to start the experiment**: FIS may refuse to start an experiment whose stop condition is already in `ALARM`, instead of starting and failing it. That is an acceptable outcome, as long as no instance is terminated.
