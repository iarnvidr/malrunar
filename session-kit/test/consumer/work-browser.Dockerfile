# The X test consumer plus the optional work-browser component (and nothing else).
ARG CONSUMER
ARG WORK_BROWSER_IMAGE
FROM ${WORK_BROWSER_IMAGE} AS work-browser

FROM ${CONSUMER}
COPY --link --from=work-browser / /
