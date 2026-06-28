#!/usr/bin/env bash
# Generate URL-safe JWT_SECRET / MCP_AUTH_TOKEN values for a non-local deploy
# whose Terraform/Helm path doesn't auto-generate them. URL-safe so they drop
# cleanly into env files + HTTP bearer headers without escaping.
#
# Usage:
#   infra/scripts/gen-secrets.sh            # prints both as KEY=VALUE lines
#   infra/scripts/gen-secrets.sh > prod.env # capture into an env file
set -euo pipefail

gen() {
  # 48 url-safe chars from /dev/urandom (letters/digits + - _).
  LC_ALL=C tr -dc 'A-Za-z0-9_-' </dev/urandom | head -c 48
  echo
}

printf 'JWT_SECRET=%s\n' "$(gen)"
printf 'MCP_AUTH_TOKEN=%s\n' "$(gen)"
