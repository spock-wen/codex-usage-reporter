#!/bin/sh
# Independent Certbot deploy hook; never replaces existing renewal hooks.
set -eu
if [ -n "${RENEWED_LINEAGE:-}" ] && [ "$RENEWED_LINEAGE" != /etc/letsencrypt/live/usage.wenspock.site ]; then
    exit 0
fi
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx
