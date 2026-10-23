#!/usr/bin/env bash
# Copyright 2026 efthomsen
# SPDX-License-Identifier: Apache-2.0
#
# malrunar-bridge-logfilter.sh [quiet-seconds]   (reads ffmpeg's log on stdin, writes it to stdout)
#
# Joining an MPEG-TS stream in the middle of a GOP makes ffmpeg's H.264 decoder complain until the
# first keyframe arrives ("non-existing PPS 0 referenced", "Error submitting packet to decoder",
# "no frame!", ...). That is expected and harmless, and it buries real problems. For the first
# <quiet-seconds> (default 10; 0 = filter nothing) lines matching those known decoder messages are
# dropped. The quiet period ends early at the first "frame=N" progress line (ffmpeg at -loglevel
# info). Every other line, such as "Connection refused" or "Invalid argument", always passes, and
# after the quiet period everything passes. Carriage-return progress updates count as lines.
set -u
quiet="${1:-10}"
[[ "$quiet" =~ ^[0-9]+$ ]] || { echo "malrunar-bridge-logfilter.sh: quiet seconds must be a whole number, got '$quiet'" >&2; exit 2; }

noise='non-existing (PPS|SPS)|PPS id out of range|SPS unavailable|Error submitting packet to decoder|no frame!|decode_slice_header error|Error while decoding stream|co located POCs unavailable|mmco: unref short failure|(Missing|missing) reference picture|reference picture missing during reorder|Could not find ref with POC|Last message repeated [0-9]+ times'
progress='^frame= *[1-9]'
shopt -s nocasematch
in_quiet=0
(( quiet > 0 )) && in_quiet=1
SECONDS=0

emit() {
    local line=$1
    [[ -n "$line" ]] || return 0
    if (( in_quiet )); then
        if (( SECONDS >= quiet )) || [[ "$line" =~ $progress ]]; then
            in_quiet=0
        elif [[ "$line" =~ $noise ]]; then
            return 0
        fi
    fi
    printf '%s\n' "$line"
}

buf=""
while IFS= read -r -N 1 ch; do
    if [[ "$ch" == $'\n' || "$ch" == $'\r' ]]; then
        emit "$buf"
        buf=""
    else
        buf+="$ch"
    fi
done
emit "$buf"
