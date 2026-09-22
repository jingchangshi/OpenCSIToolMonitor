#!/usr/bin/env bash
# Local rehearsal of the CI secret scan. Kept as a file so the quoting is the
# same as in the workflow, rather than being mangled by a PowerShell wrapper.
set -euo pipefail
cd "$(dirname "$0")/.."

fail=0

# JWT: three base64url segments.
if git grep -nIE 'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}' \
     -- . ':!*.md' ':!docs/*'; then
  echo "FAIL: a JWT-shaped string is committed"; fail=1
else
  echo "ok: no JWT-shaped strings"
fi

# A literal cookie header carrying a value.
if git grep -nIE 'Cookie:[[:space:]]*[A-Za-z_]+=[A-Za-z0-9._-]{20,}' \
     -- . ':!*.md' ':!docs/*'; then
  echo "FAIL: a literal Cookie header with a value is committed"; fail=1
else
  echo "ok: no literal Cookie headers with values"
fi

# A token assignment with a long literal.
if git grep -nIE "(access_token|refresh_token|xauth_token)['\"]?[[:space:]]*[=:][[:space:]]*['\"][A-Za-z0-9._-]{24,}['\"]" \
     -- . ':!*.md' ':!docs/*' ':!tests/*'; then
  echo "FAIL: a hard-coded token literal is committed"; fail=1
else
  echo "ok: no hard-coded token literals"
fi

# Repository hygiene: no generated files tracked.
if git ls-files | grep -E '(__pycache__|\.pyc$|\.pyo$|\.egg-info|^dist/|^build/)'; then
  echo "FAIL: compiled or generated files are tracked"; fail=1
else
  echo "ok: no generated files are tracked"
fi

exit $fail
