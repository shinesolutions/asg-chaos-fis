# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added
- Initial AEM Chaos Engine Lambda, replacing the Simian Army Chaos Monkey for AEM full-set stacks
- Maximum of one termination per local day across all targets, enforced by an atomic SSM parameter claim
- Weighted target selection across author-dispatcher, publish and publish-dispatcher Auto Scaling Groups, each of which can be disabled
- Pre-checks for stack stability, Auto Scaling Group health, orchestrator health and active experiments
- Pre-checks that Auto Scaling Group instances are running in EC2 and past the health check grace period
- Pre-check that the stop condition alarms are OK before the day is claimed
- Pre-checks that the target Auto Scaling Group has no instance in another lifecycle state, and that FIS can only select its healthy InService instances
- Pre-checks of the paired Auto Scaling Group and the orchestrator for publish and publish-dispatcher, which the orchestrator terminates in pairs
- Dry run mode
- Decision metrics in CloudWatch Embedded Metric Format
