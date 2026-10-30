# Malrunar Linux checks

This Dagger 0.21.11 module runs the isolated Python/shell unit suites and Node syntax checks. Git-based content-hash checks and public-hygiene checks remain in the hosted workflow.
Run it locally with `dagger call -m ci/dagger/check checks --source .`.
