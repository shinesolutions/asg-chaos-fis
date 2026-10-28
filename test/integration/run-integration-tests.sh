#!/usr/bin/env bash
#
# ASG Chaos FIS integration tests.
#
# Deploys the engine (test/integration/harness.yaml) against an existing test
# Auto Scaling Group and checks its behaviour against real AWS services. See
# test/integration/README.md.
#
# Usage: test/integration/run-integration-tests.sh <command>
#
#   preflight   Safety checks only. Changes nothing.
#   deploy      Package the Lambda, upload it and deploy the harness stack (dry run mode)
#   iam         Check the IAM scoping with the IAM policy simulator. Changes nothing.
#   dry-run     Dry run tests. Terminates nothing.
#   scheduler   One-off EventBridge Scheduler invocation, in dry run mode. Terminates nothing.
#   kill        Termination tests. TERMINATES ONE TARGET INSTANCE, after confirmation.
#   teardown    Delete the harness stack and the claims
#   all         preflight, deploy, iam, dry-run, scheduler and kill (not teardown)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/integration.env}"

# Components of real AEM OpenCloud stacks, which may run in the same test account.
# The harness must never target these.
PROTECTED_COMPONENTS="author author-primary author-standby author-dispatcher publish publish-dispatcher
preview-publish preview-publish-dispatcher orchestrator chaos-monkey"

PASSED=0
FAILED=0
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

# --- Helpers -------------------------------------------------------------------

log() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die() { printf '\n[ERROR] %s\n' "$*" >&2; exit 1; }

pass() { PASSED=$((PASSED + 1)); printf '    [PASS] %s\n' "$*"; }
fail() { FAILED=$((FAILED + 1)); printf '    [FAIL] %s\n' "$*"; }

check() {
  # check <description> <expected> <actual>
  if [[ "$2" == "$3" ]]; then pass "$1 ($3)"; else fail "$1: expected '$2', got '$3'"; fi
}

check_one_of() {
  # check_one_of <description> <actual> <expected...>
  local description="$1" actual="$2"
  shift 2
  for expected in "$@"; do
    if [[ "${actual}" == "${expected}" ]]; then pass "${description} (${actual})"; return; fi
  done
  fail "${description}: expected one of '$*', got '${actual}'"
}

json_get() {
  # json_get <file> <key>: print a top-level field of a JSON file
  python3 -c 'import json, sys; v = json.load(open(sys.argv[1])).get(sys.argv[2]); print("" if v is None else v)' "$1" "$2"
}

load_env() {
  [[ -f "${ENV_FILE}" ]] || die "Missing ${ENV_FILE}. Copy integration.env.sample to integration.env and fill it in."
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
  local name
  for name in AWS_REGION HARNESS_STACK_NAME ENGINE_NAME TARGET_STACK_PREFIX \
              TARGET_COMPONENT TARGET_ASG_NAME TARGET_STACK_NAME TARGET_LOAD_BALANCER_NAME; do
    [[ -n "${!name:-}" ]] || die "${name} is not set in ${ENV_FILE}"
  done
  export AWS_REGION AWS_DEFAULT_REGION="${AWS_REGION}"
  FUNCTION_NAME="${ENGINE_NAME}-asg-chaos-fis"
  STATE_PATH="/asg-chaos-fis/${ENGINE_NAME}/kills"
}

stack_output() {
  aws cloudformation describe-stacks --stack-name "${HARNESS_STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

stack_parameter() {
  aws cloudformation describe-stacks --stack-name "${HARNESS_STACK_NAME}" \
    --query "Stacks[0].Parameters[?ParameterKey=='$1'].ParameterValue" --output text
}

update_stack() {
  # update_stack Key=Value...: change harness parameters, keeping all others
  info "Updating harness stack: $*"
  aws cloudformation deploy --stack-name "${HARNESS_STACK_NAME}" \
    --template-file "${SCRIPT_DIR}/harness.yaml" --capabilities CAPABILITY_IAM \
    --no-fail-on-empty-changeset --parameter-overrides "$@" >/dev/null
}

today() { TZ=Australia/Sydney date +%Y-%m-%d; }

claim_value() {
  aws ssm get-parameter --name "${STATE_PATH}/$(today)" --query Parameter.Value --output text 2>/dev/null || true
}

delete_claim() {
  aws ssm delete-parameter --name "${STATE_PATH}/$(today)" 2>/dev/null || true
}

running_instances() {
  aws ec2 describe-instances \
    --filters "Name=tag:StackPrefix,Values=${TARGET_STACK_PREFIX}" "Name=tag:Component,Values=${TARGET_COMPONENT}" \
              "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[].InstanceId' --output text | tr '\t' '\n' | sort
}

healthy_in_service() {
  aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "${TARGET_ASG_NAME}" \
    --query "length(AutoScalingGroups[0].Instances[?LifecycleState=='InService' && HealthStatus=='Healthy'])" \
    --output text
}

desired_capacity() {
  aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "${TARGET_ASG_NAME}" \
    --query 'AutoScalingGroups[0].DesiredCapacity' --output text
}

elb_in_service() {
  aws elb describe-instance-health --load-balancer-name "${TARGET_LOAD_BALANCER_NAME}" \
    --query "length(InstanceStates[?State=='InService'])" --output text
}

asg_replaced() {
  # The ASG's own view: desired number of InService/Healthy instances, all running in EC2
  local desired
  desired="$(desired_capacity)"
  [[ "$(healthy_in_service)" == "${desired}" && "$(running_instances | wc -l | tr -d ' ')" == "${desired}" ]]
}

elb_recovered() {
  # The load balancer's view, which only counts instances that are actually serving
  [[ "$(elb_in_service)" == "$(desired_capacity)" ]] && asg_replaced
}

remaining_grace_period() {
  # Seconds until the youngest ASG instance is past the ASG's health check grace period
  local grace ids
  grace="$(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "${TARGET_ASG_NAME}" \
    --query 'AutoScalingGroups[0].HealthCheckGracePeriod' --output text)"
  ids="$(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "${TARGET_ASG_NAME}" \
    --query 'AutoScalingGroups[0].Instances[].InstanceId' --output text)"
  # shellcheck disable=SC2086
  aws ec2 describe-instances --instance-ids ${ids} --query 'Reservations[].Instances[].LaunchTime' --output json \
    | python3 -c 'import datetime as d, json, sys
now = d.datetime.now(d.timezone.utc)
ages = [(now - d.datetime.fromisoformat(t)).total_seconds() for t in json.load(sys.stdin)]
print(max(0, int(int(sys.argv[1]) - min(ages)) + 1) if ages else 0)' "${grace}"
}

wait_until() {
  # wait_until <description> <timeout seconds> <check command...>: poll every 15s, set WAITED to the seconds taken
  local description="$1" timeout="$2" start
  shift 2
  start="$(date +%s)"
  info "Waiting until ${description} (up to $((timeout / 60)) minutes)..."
  while ! "$@"; do
    if (( $(date +%s) - start >= timeout )); then
      info "Timed out."
      return 1
    fi
    sleep 15
  done
  WAITED=$(( $(date +%s) - start ))
  info "Done after ${WAITED} seconds."
}

wait_until_settled() {
  # Before a test that expects the pre-checks to pass: fully recovered and past the grace period
  wait_until "${TARGET_ASG_NAME} is healthy in the ASG and the load balancer" 1500 elb_recovered \
    || die "${TARGET_ASG_NAME} is not healthy"
  local remaining
  remaining="$(remaining_grace_period)"
  if (( remaining > 0 )); then
    info "The newest instance is still in the health check grace period. Waiting ${remaining} seconds..."
    sleep "${remaining}"
  fi
}

set_config_dry_run() {
  # Switch dry run directly in SSM. Takes seconds, unlike a stack update.
  # The next update_stack writes the parameter from the template again.
  local name="/asg-chaos-fis/${ENGINE_NAME}/config" value
  value="$(aws ssm get-parameter --name "${name}" --query Parameter.Value --output text \
    | python3 -c 'import json, sys; c = json.load(sys.stdin); c["dryRun"] = sys.argv[1] == "true"; print(json.dumps(c))' "$1")"
  aws ssm put-parameter --name "${name}" --type String --overwrite --value "${value}" >/dev/null
  info "Set dryRun=$1 directly in the SSM configuration"
}

check_precheck_reason() {
  # check_precheck_reason <decision file> <expected reason prefix>
  local status reason
  status="$(json_get "$1" status)"
  reason="$(json_get "$1" reason)"
  if [[ "${status}" == SKIPPED && "${reason}" == "$2"* ]]; then
    pass "Blocked by a pre-check (${reason})"
  else
    fail "Expected a ${2}* pre-check, got ${status} / ${reason}"
  fi
}

invoke() {
  # invoke <test name>: invoke the engine and leave the decision in ${WORK_DIR}/<test name>.json
  local name="$1" output="${WORK_DIR}/$1.json" error payload timestamp
  timestamp="$(date +%s)"
  payload="{\"scheduledTime\": \"integration-${name}-${timestamp}\"}"
  error="$(aws lambda invoke --function-name "${FUNCTION_NAME}" --cli-binary-format raw-in-base64-out \
    --payload "${payload}" --query FunctionError --output text "${output}")"
  info "Decision: $(cat "${output}")"
  if [[ "${error}" != "None" && -n "${error}" ]]; then
    fail "Lambda returned an error: ${error}"
    if grep -q 'Runtime.ImportModuleError' "${output}"; then
      info "The function still has the placeholder code. Run the deploy step first."
    fi
  fi
}

wait_for_experiment() {
  # wait_for_experiment <experiment id>: print the final status
  local status
  for _ in $(seq 1 40); do
    status="$(aws fis get-experiment --id "$1" --query experiment.state.status --output text)"
    case "${status}" in
      completed|stopped|failed|cancelled) echo "${status}"; return ;;
    esac
    sleep 5
  done
  echo "timeout"
}

# --- Commands ------------------------------------------------------------------

cmd_preflight() {
  log "Preflight safety checks"
  local tool
  for tool in aws python3 zip openssl base64; do command -v "${tool}" >/dev/null || die "${tool} is required"; done

  local component
  for component in ${PROTECTED_COMPONENTS}; do
    [[ "${TARGET_COMPONENT}" != "${component}" ]] \
      || die "TARGET_COMPONENT '${TARGET_COMPONENT}' is an AEM OpenCloud component. Use a test Auto Scaling Group."
  done

  local identity
  identity="$(aws sts get-caller-identity --query '[Account, Arn]' --output text)" \
    || die "Not logged in to AWS. Run 'aws login' and try again."
  info "AWS account / identity: ${identity}"
  info "Region: ${AWS_REGION}"

  local matching asg_members instance
  matching="$(aws ec2 describe-instances \
    --filters "Name=tag:StackPrefix,Values=${TARGET_STACK_PREFIX}" "Name=tag:Component,Values=${TARGET_COMPONENT}" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text | tr '\t' '\n' | sort)"
  asg_members="$(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "${TARGET_ASG_NAME}" \
    --query 'AutoScalingGroups[0].Instances[].InstanceId' --output text | tr '\t' '\n' | sort)"
  [[ -n "${matching}" ]] || die "No instances are tagged StackPrefix=${TARGET_STACK_PREFIX} and Component=${TARGET_COMPONENT}"
  for instance in ${matching}; do
    grep -qx "${instance}" <<<"${asg_members}" \
      || die "Instance ${instance} matches the target tags but is not in ${TARGET_ASG_NAME}. Aborting."
  done
  info "Instances that FIS may terminate (all in ${TARGET_ASG_NAME}):"
  for instance in ${matching}; do info "  ${instance}"; done
  info "Healthy InService: $(healthy_in_service) of $(desired_capacity) desired"
  pass "Only instances of the test Auto Scaling Group match the target tags"
}

cmd_deploy() {
  cmd_preflight
  log "Packaging the Lambda"
  make -C "${REPO_DIR}" --no-print-directory clean package >/dev/null
  local zip
  zip="$(ls "${REPO_DIR}"/stage/asg-chaos-fis-*.zip)"
  info "Built ${zip}"

  log "Deploying harness stack ${HARNESS_STACK_NAME} (dry run mode)"
  update_stack \
    "EngineName=${ENGINE_NAME}" \
    "TargetStackPrefix=${TARGET_STACK_PREFIX}" "TargetComponent=${TARGET_COMPONENT}" \
    "TargetAsgName=${TARGET_ASG_NAME}" "TargetStackName=${TARGET_STACK_NAME}" \
    "TargetLoadBalancerName=${TARGET_LOAD_BALANCER_NAME}" DryRun=true MinInService=2 RequiredMinInService=1 \
    ScheduleAt=
  pass "Harness stack deployed"

  log "Uploading the Lambda code directly from ${zip}"
  aws lambda update-function-code --function-name "${FUNCTION_NAME}" --zip-file "fileb://${zip}" >/dev/null
  aws lambda wait function-updated-v2 --function-name "${FUNCTION_NAME}"
  local local_sha deployed_sha
  local_sha="$(openssl dgst -sha256 -binary "${zip}" | base64)"
  deployed_sha="$(aws lambda get-function --function-name "${FUNCTION_NAME}" \
    --query Configuration.CodeSha256 --output text)"
  check "Deployed code matches the local zip" "${local_sha}" "${deployed_sha}"
}

cmd_iam() {
  log "IAM scoping (IAM policy simulator, nothing is changed)"
  local fis_role function_role template account result
  fis_role="$(stack_output FisRoleArn)"
  function_role="$(stack_output FunctionRoleArn)"
  template="$(stack_output ExperimentTemplateId)"
  account="$(aws sts get-caller-identity --query Account --output text)"
  local instance_arn="arn:aws:ec2:${AWS_REGION}:${account}:instance/i-00000000000000000"

  simulate() {
    # simulate <role> <action> <resource> [StackPrefix Component]
    local context=()
    if [[ $# -eq 5 ]]; then
      context=(--context-entries
        "ContextKeyName=aws:ResourceTag/StackPrefix,ContextKeyValues=$4,ContextKeyType=string"
        "ContextKeyName=aws:ResourceTag/Component,ContextKeyValues=$5,ContextKeyType=string")
    fi
    # ${context[@]+...} keeps bash 3.2 (macOS) happy with an empty array under set -u
    aws iam simulate-principal-policy --policy-source-arn "$1" --action-names "$2" --resource-arns "$3" \
      ${context[@]+"${context[@]}"} --query 'EvaluationResults[0].EvalDecision' --output text
  }

  result="$(simulate "${fis_role}" ec2:TerminateInstances "${instance_arn}" "${TARGET_STACK_PREFIX}" "${TARGET_COMPONENT}")"
  check "FIS role may terminate an instance with the target tags" allowed "${result}"
  result="$(simulate "${fis_role}" ec2:TerminateInstances "${instance_arn}" "${TARGET_STACK_PREFIX}" publish-dispatcher)"
  check "FIS role may not terminate an instance with another Component tag" implicitDeny "${result}"
  result="$(simulate "${fis_role}" ec2:TerminateInstances "${instance_arn}" other-stack-prefix "${TARGET_COMPONENT}")"
  check "FIS role may not terminate an instance with another StackPrefix tag" implicitDeny "${result}"
  result="$(simulate "${fis_role}" ec2:TerminateInstances "${instance_arn}")"
  check "FIS role may not terminate an untagged instance" implicitDeny "${result}"

  local template_arn="arn:aws:fis:${AWS_REGION}:${account}:experiment-template"
  result="$(simulate "${function_role}" fis:StartExperiment "${template_arn}/${template}")"
  check "Lambda role may start the harness experiment template" allowed "${result}"
  result="$(simulate "${function_role}" fis:StartExperiment "${template_arn}/EXTOTHERTEMPLATE")"
  check "Lambda role may not start any other experiment template" implicitDeny "${result}"
  result="$(simulate "${function_role}" fis:GetExperimentTemplate "${template_arn}/${template}")"
  check "Lambda role may read the harness experiment template" allowed "${result}"
  result="$(simulate "${function_role}" fis:GetExperimentTemplate "${template_arn}/EXTOTHERTEMPLATE")"
  check "Lambda role may not read any other experiment template" implicitDeny "${result}"
  result="$(simulate "${function_role}" ec2:TerminateInstances "${instance_arn}" "${TARGET_STACK_PREFIX}" "${TARGET_COMPONENT}")"
  check "Lambda role may not terminate instances itself" implicitDeny "${result}"
  # DescribeAlarms is authorised against alarm:*, whichever alarm names are requested
  result="$(simulate "${function_role}" cloudwatch:DescribeAlarms "arn:aws:cloudwatch:${AWS_REGION}:${account}:alarm:*")"
  check "Lambda role may read the stop condition alarm" allowed "${result}"
}

cmd_dry_run() {
  log "Dry run tests (nothing is terminated)"
  [[ "$(stack_parameter DryRun)" == "true" ]] || update_stack DryRun=true
  wait_until_settled
  delete_claim
  local before alarm
  before="$(running_instances)"
  alarm="$(stack_output StopConditionAlarmName)"

  info "Test: dry run decision"
  invoke dry-run
  check "Status" DRY_RUN "$(json_get "${WORK_DIR}/dry-run.json" status)"
  check "Reason" WOULD_TERMINATE "$(json_get "${WORK_DIR}/dry-run.json" reason)"
  check "Day not claimed" "" "$(claim_value)"
  check "No instance terminated" "${before}" "$(running_instances)"

  info "Test: pre-check blocks a termination when the Auto Scaling Group has too few instances"
  update_stack MinInService=3
  invoke precheck
  check "Reason" PRECHECK_TARGET_ASG_BELOW_MIN_IN_SERVICE "$(json_get "${WORK_DIR}/precheck.json" reason)"
  update_stack MinInService=2

  info "Test: pre-check blocks a termination when a required Auto Scaling Group has too few instances"
  update_stack RequiredMinInService=3
  invoke required-precheck
  check "Reason" PRECHECK_REQUIRED_ASG_BELOW_MIN_IN_SERVICE "$(json_get "${WORK_DIR}/required-precheck.json" reason)"
  update_stack RequiredMinInService=1

  info "Test: a stop condition alarm that is not OK blocks a termination before the day is claimed"
  aws cloudwatch set-alarm-state --alarm-name "${alarm}" --state-value ALARM --state-reason "Integration test"
  invoke stop-condition-precheck
  aws cloudwatch set-alarm-state --alarm-name "${alarm}" --state-value OK --state-reason "Integration test"
  check "Reason" PRECHECK_STOP_CONDITION_ALARM "$(json_get "${WORK_DIR}/stop-condition-precheck.json" reason)"
  check "Day not claimed" "" "$(claim_value)"
}

cmd_scheduler() {
  log "EventBridge Scheduler test (dry run, nothing is terminated)"
  [[ "$(stack_parameter DryRun)" == "true" ]] || update_stack DryRun=true
  local at start_ms
  at="$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) + d.timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:00"))')"
  start_ms="$(($(date +%s) * 1000))"
  update_stack "ScheduleAt=${at}"
  info "Schedule set for ${at} UTC. Waiting for the invocation (up to 8 minutes)..."

  local found=""
  for _ in $(seq 1 32); do
    found="$(aws logs filter-log-events --log-group-name "/aws/lambda/${FUNCTION_NAME}" --start-time "${start_ms}" \
      --filter-pattern '{ $.Decisions = 1 }' --query 'events[].message' --output json \
      | python3 -c 'import json, sys
for message in json.load(sys.stdin):
    record = json.loads(message)
    if record.get("scheduledTime") and not record["scheduledTime"].startswith("integration-"):
        print(record["Status"], record["Reason"], record["scheduledTime"])')"
    [[ -n "${found}" ]] && break
    sleep 15
  done
  if [[ -n "${found}" ]]; then
    pass "Scheduler invoked the engine with its scheduled time: ${found}"
  else
    fail "No invocation from EventBridge Scheduler found in the logs"
  fi
  update_stack ScheduleAt=
}

cmd_kill() {
  cmd_preflight
  log "Termination tests"
  printf '\n    These tests TERMINATE ONE INSTANCE of %s.\n    Type TERMINATE to continue: ' "${TARGET_ASG_NAME}"
  local answer
  read -r answer
  [[ "${answer}" == "TERMINATE" ]] || die "Aborted."

  wait_until_settled
  update_stack DryRun=false
  delete_claim
  local alarm template before after experiment status reason terminated killed_at
  alarm="$(stack_output StopConditionAlarmName)"
  template="$(stack_output ExperimentTemplateId)"

  info "Test: FIS's own stop condition stops an experiment when the alarm is not OK"
  info "(The engine checks the alarm before claiming the day, see dry-run. This starts FIS directly to test the backstop.)"
  before="$(running_instances)"
  aws cloudwatch set-alarm-state --alarm-name "${alarm}" --state-value ALARM --state-reason "Integration test"
  experiment="$(aws fis start-experiment --experiment-template-id "${template}" \
    --tags Name=asg-chaos-fis-integration-backstop --query experiment.id --output text)"
  status="$(wait_for_experiment "${experiment}")"
  info "Experiment ${experiment}: ${status} ($(aws fis get-experiment --id "${experiment}" --query experiment.state.reason --output text))"
  aws cloudwatch set-alarm-state --alarm-name "${alarm}" --state-value OK --state-reason "Integration test"
  check_one_of "Experiment did not complete" "${status}" stopped failed
  check "No instance terminated" "${before}" "$(running_instances)"
  wait_until_settled

  info "Test: termination"
  before="$(running_instances)"
  killed_at="$(date +%s)"
  invoke kill
  check "Status" EXECUTED "$(json_get "${WORK_DIR}/kill.json" status)"
  experiment="$(json_get "${WORK_DIR}/kill.json" experimentId)"
  if [[ -z "${experiment}" ]]; then
    fail "No experiment was started, so the remaining termination tests cannot run"
    update_stack DryRun=true
    return
  fi
  check "Claim records the experiment" "${experiment}" \
    "$(claim_value | python3 -c 'import json, sys; print(json.load(sys.stdin).get("experimentId"))')"

  info "Test: the daily limit blocks a second termination"
  invoke daily-limit
  check "Reason" DAILY_LIMIT_REACHED "$(json_get "${WORK_DIR}/daily-limit.json" reason)"

  check "Experiment completed" completed "$(wait_for_experiment "${experiment}")"
  after="$(running_instances)"
  terminated="$(comm -23 <(echo "${before}") <(echo "${after}"))"
  check "Exactly one instance terminated" 1 "$(grep -c . <<<"${terminated}")"
  info "Terminated: ${terminated}"

  info "The recovery tests run in dry run mode with the day's claim removed, so that they reach the"
  info "pre-checks. Even if a pre-check failed to block, dry run mode terminates nothing."
  set_config_dry_run true
  delete_claim

  info "Test: right after the termination, before the ASG has noticed"
  invoke just-terminated
  check_precheck_reason "${WORK_DIR}/just-terminated.json" PRECHECK_TARGET_ASG_
  reason="$(json_get "${WORK_DIR}/just-terminated.json" reason)"
  if [[ "${reason}" == PRECHECK_TARGET_ASG_INSTANCE_NOT_RUNNING ]]; then
    info "The ASG still counted the terminated instance; the EC2 check caught it."
  else
    info "The ASG had already noticed the termination."
  fi

  info "Test: while the replacement is in the health check grace period"
  wait_until "the ASG reports the replacement as InService" 900 asg_replaced \
    || fail "The ASG did not launch a replacement within 15 minutes"
  invoke grace-period
  check "Reason" PRECHECK_TARGET_ASG_IN_GRACE_PERIOD "$(json_get "${WORK_DIR}/grace-period.json" reason)"

  info "Test: the replacement serves traffic"
  if wait_until "the load balancer reports all instances InService" 1500 elb_recovered; then
    pass "Recovered about $(( $(date +%s) - killed_at )) seconds after the termination"
  else
    fail "The load balancer did not report all instances InService within 25 minutes"
  fi

  update_stack DryRun=true
}

cmd_teardown() {
  log "Teardown"
  aws cloudformation delete-stack --stack-name "${HARNESS_STACK_NAME}"
  aws cloudformation wait stack-delete-complete --stack-name "${HARNESS_STACK_NAME}"
  info "Deleted stack ${HARNESS_STACK_NAME}"
  local claims
  claims="$(aws ssm get-parameters-by-path --path "${STATE_PATH}" --query 'Parameters[].Name' --output text)"
  if [[ -n "${claims}" ]]; then
    # shellcheck disable=SC2086
    aws ssm delete-parameters --names ${claims} >/dev/null
    info "Deleted claims: ${claims}"
  fi
  info "FIS experiment history cannot be deleted and expires after 120 days."
}

summary() {
  log "Summary: ${PASSED} passed, ${FAILED} failed"
  [[ "${FAILED}" -eq 0 ]]
}

main() {
  local command="${1:-}"
  load_env
  case "${command}" in
    preflight) cmd_preflight ;;
    deploy) cmd_deploy ;;
    iam) cmd_iam ;;
    dry-run) cmd_dry_run ;;
    scheduler) cmd_scheduler ;;
    kill) cmd_kill ;;
    teardown) cmd_teardown ;;
    all) cmd_deploy; cmd_iam; cmd_dry_run; cmd_scheduler; cmd_kill ;;
    *) sed -n '9,18p' "$0"; exit 1 ;;
  esac
  summary
}

main "$@"
