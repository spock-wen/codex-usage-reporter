#!/bin/sh
# Independent Certbot deploy hook; never replaces existing renewal hooks.
set -eu
case "${RENEWED_LINEAGE:-}" in
    ""|/etc/letsencrypt/live/codex.wenspock.site|/etc/letsencrypt/live/usage.wenspock.site) ;;
    *) exit 0 ;;
esac
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx
