#!/usr/bin/env bash
# Stops the credential relay and deletes the resolved credentials. with-auth.sh
# calls it after its command; the action also runs it once at the end, so a step
# that kept credentials for the next one never leaves them behind.
AUTH_DIR="${RUNNER_TEMP}/codeboarding-auth"
if [ -s "$AUTH_DIR/relay.pid" ]; then
  kill "$(cat "$AUTH_DIR/relay.pid")" 2>/dev/null || true
fi
rm -rf "$AUTH_DIR"
