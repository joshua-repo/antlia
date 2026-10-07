#!/usr/bin/env bash
# Is antlia's vendored IBC template still a 3-line patch of the image's own?
#
# The one real cost of mounting our own config.ini.tmpl: the image ships its
# template, and when the image updates, ours silently keeps overriding it --
# including any new settings the update added. This is the same class of bug
# as editing web/src without rebuilding, and it gets the same treatment: a
# cheap check that says so out loud.
#
# Run it after pulling a new ghcr.io/gnzsnz/ib-gateway image.
set -euo pipefail
cd "$(dirname "$0")"
container=${1:-ib-gateway-ib-gateway-1}

# The image's own template, read from inside the container. IBC_INI_TMPL is
# pointed at ours by the override, so ask for the original path explicitly.
docker exec "$container" cat /home/ibgateway/ibc/config.ini.tmpl > /tmp/ibc-image.tmpl

if diff -q /tmp/ibc-image.tmpl ibc-config.ini.tmpl >/dev/null; then
  echo "identical -- the patch is MISSING from antlia's copy."
  exit 1
fi

changed=$(diff /tmp/ibc-image.tmpl ibc-config.ini.tmpl | grep -c '^[<>]' || true)
echo "--- diff against the image's template ---"
diff /tmp/ibc-image.tmpl ibc-config.ini.tmpl || true
echo "-----------------------------------------"
if [ "$changed" -eq 6 ]; then
  echo "OK: exactly the 3 expected lines differ (3 removed + 3 added)."
else
  echo "DRIFT: $changed changed lines, expected 6."
  echo "The image's template has moved. Re-vendor it and re-apply the 3 lines:"
  echo "  docker exec $container cat /home/ibgateway/ibc/config.ini.tmpl > ops/ib-gateway/ibc-config.ini.tmpl"
  echo "  # then set CommandServerPort / ControlFrom / BindAddress to their \${...} placeholders"
  echo "  # NOTE: the file is CRLF -- patch it as bytes, or the whole file 'changes'."
  exit 1
fi
