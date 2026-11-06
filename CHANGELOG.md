# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.10.0 - 2026-11-06
### Added
- Initial ASG Chaos FIS Lambda, which terminates one random instance of one of the configured Auto Scaling Groups at most once a day, using an AWS FIS experiment template
- Maximum of one termination per local day across all targets, enforced by an atomic SSM parameter claim
- Weighted target selection across the configured Auto Scaling Groups, each of which can be disabled
- Configuration `schemaVersion`, so that a configuration for another version fails the invocation
- Pre-checks for stack stability (optional), Auto Scaling Group health and active experiments
- Pre-checks that Auto Scaling Group instances are running in EC2 and past the health check grace period
- Pre-checks that the target Auto Scaling Group has no instance in another lifecycle state, and that the instances FIS can select are exactly its healthy InService instances
- Pre-checks of each target's `requiredHealthy` Auto Scaling Groups
- Target tags and stop condition alarms read from each target's FIS experiment template, and a pre-check that skips templates whose selection the pre-checks cannot fully cover
- Pre-check that the stop condition alarms are OK before the day is claimed
- Configurable experiment tags, plus `ChaosTarget`, `TriggeredBy` and `LocalDate` on every experiment
- Dry run mode
- Decision metrics in CloudWatch Embedded Metric Format, with a configurable namespace and dimensions
- `run()` entry point for deployments that build the configuration in code
- SHA-256 checksum file published next to each release zip
