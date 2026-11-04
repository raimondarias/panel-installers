# Implementation status and next validation

Implemented areas include CI-published releases with a self-updating bootstrap, guided Panel update rollback, Panel ↔ Wings compatibility checks, local backup retention, disposable integration tooling, automatic first-node creation, encrypted scheduled Panel/Wings-configuration backups, guided Panel restore, persisted resume phases, expanded health checks, isolated native game SQL and verified SSH provisioning of remote nodes/SQL. Hosted game file/database backups and restores are outside this installer’s scope. See [advanced operations](ADVANCED.md) for their contracts and limits.

Remaining validation and expansion:

1. Add ARM64 runners to the completed 18-case amd64 Ubuntu/Debian × standalone/Docker × MariaDB/MySQL matrix. Keep failures visible and attach only redacted artifacts.
2. Test S3 and SFTP against independent off-server infrastructure; current encrypted repository roundtrips use isolated local storage.
3. Test remote routing and firewall behavior between independent VPSes. Local SSH/VM tests do not prove provider networking.
4. Validate guided Panel update rollback on fresh Docker and standalone systems across a real release pair.

[Verification report](../test-results/REPORT.md) · [Testing procedure](TESTING.md).
