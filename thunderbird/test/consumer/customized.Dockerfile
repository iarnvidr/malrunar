# What a consumer does to its OWN copy of Thunderbird, exactly as thunderbird/README.md shows it. This image
# is a modified Thunderbird: it exists only in the test run and is never published. The node component
# is added for the test only (it runs the OAuth module test); the customization itself needs python3.
ARG CONSUMER
ARG NODE_IMAGE
FROM ${NODE_IMAGE} AS node

FROM ${CONSUMER}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
COPY --link --from=node / /
ARG THUNDERBIRD_CUSTOMIZE=/opt/malrunar/thunderbird/consumer
RUN python3 "$THUNDERBIRD_CUSTOMIZE/customize.py" oauth-shared-mailbox; \
    python3 "$THUNDERBIRD_CUSTOMIZE/customize.py" addon-pinned tbsync; \
    python3 "$THUNDERBIRD_CUSTOMIZE/customize.py" addon-pinned eas4tbsync; \
    python3 "$THUNDERBIRD_CUSTOMIZE/customize.py" policies \
        "$THUNDERBIRD_CUSTOMIZE/policies/maildir-and-oauth.json" "$THUNDERBIRD_CUSTOMIZE/policies/tbsync.json"; \
    python3 "$THUNDERBIRD_CUSTOMIZE/customize.py" verify
