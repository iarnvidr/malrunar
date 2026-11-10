# Malrunar Linux checks

This Dagger 1.0 module runs the isolated Python/shell unit suites and Node syntax checks. Git-based content-hash checks and public-hygiene checks remain in the hosted workflow.

Run it from the workspace root with `dagger check dag://malrunar-ci/checks`.
`dagger generate -y` refreshes the committed Go SDK code and `dagger.lock` after module changes.
