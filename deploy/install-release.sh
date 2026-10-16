#!/bin/sh
# Run on the target host as root, from a reviewed source staging directory.
# Usage: install-release.sh /absolute/source/root [none|bootstrap|tls]
set -eu
umask 077

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root." >&2
    exit 1
fi
if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "Usage: $0 /absolute/source/root [none|bootstrap|tls]" >&2
    exit 1
fi
SOURCE=$(realpath -e -- "$1")
MODE=${2:-none}
case "$MODE" in none|bootstrap|tls) ;; *) echo "Invalid nginx mode." >&2; exit 1 ;; esac
case "$SOURCE" in /opt/codex-usage|/opt/codex-usage/*) echo "Source must be a separate staging directory." >&2; exit 1 ;; esac
ASSETS="$SOURCE/deploy"
MANIFEST="$ASSETS/source-manifest.txt"
for item in server.py collector.py prices.json web/index.html web/app.js web/style.css; do
    test -f "$SOURCE/$item" || { echo "Missing required source: $item" >&2; exit 1; }
done
for item in source-manifest.txt codex-usage.service nginx-bootstrap.conf nginx.conf nginx-legacy.conf reload-nginx.sh; do
    test -f "$ASSETS/$item" || { echo "Missing deployment asset: $item" >&2; exit 1; }
done
if [ -e /opt/codex-usage/current ] && [ ! -L /opt/codex-usage/current ]; then
    echo "Refusing to replace current: expected a managed symlink." >&2
    exit 1
fi
AVAILABLE=/etc/nginx/sites-available/codex.wenspock.site
ENABLED=/etc/nginx/sites-enabled/codex.wenspock.site
LEGACY_AVAILABLE=/etc/nginx/sites-available/usage.wenspock.site
LEGACY_ENABLED=/etc/nginx/sites-enabled/usage.wenspock.site
if [ "$MODE" = tls ]; then
    # Keep separate lineages, so the existing legacy certificate keeps renewing.
    for hostname in codex.wenspock.site usage.wenspock.site; do
        test -s "/etc/letsencrypt/live/$hostname/fullchain.pem"
        test -s "/etc/letsencrypt/live/$hostname/privkey.pem"
    done
fi
# Only touch our two explicitly managed vhosts. Bootstrap never changes legacy.
if [ "$MODE" != none ]; then
    CHECK_HOSTS=codex.wenspock.site
    if [ "$MODE" = tls ]; then CHECK_HOSTS="$CHECK_HOSTS usage.wenspock.site"; fi
    for hostname in $CHECK_HOSTS; do
        site_available="/etc/nginx/sites-available/$hostname"
        site_enabled="/etc/nginx/sites-enabled/$hostname"
        if [ -e "$site_enabled" ] || [ -L "$site_enabled" ]; then
            if [ ! -L "$site_enabled" ] || [ "$(readlink -f -- "$site_enabled")" != "$site_available" ]; then
                echo "Refusing to replace an unrelated enabled vhost: $hostname" >&2
                exit 1
            fi
        fi
        if [ -e "$site_available" ] || [ -L "$site_available" ]; then
            if [ -L "$site_available" ] || [ ! -f "$site_available" ]; then
                echo "Refusing an unmanaged available vhost: $hostname" >&2
                exit 1
            fi
            if ! grep -Fq "server_name $hostname;" "$site_available" ||
               ! grep -Eq '^# (Dedicated vhost[.]|Install only as /etc/nginx/sites-available/)' "$site_available"; then
                echo "Refusing to overwrite an unrelated vhost configuration: $hostname" >&2
                exit 1
            fi
        fi
    done
fi

getent group codex-usage >/dev/null || groupadd --system codex-usage
if ! id codex-usage >/dev/null 2>&1; then
    useradd --system --gid codex-usage --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin codex-usage
fi
if [ "$(getent passwd codex-usage | cut -d: -f7)" != /usr/sbin/nologin ]; then
    echo "Existing codex-usage user is not the expected non-login service identity." >&2
    exit 1
fi
install -d -m 0755 /opt/codex-usage /opt/codex-usage/releases
install -d -o codex-usage -g codex-usage -m 0750 /var/lib/codex-usage
install -d -o root -g codex-usage -m 0750 /etc/codex-usage
install -d -o root -g root -m 0700 /var/backups/codex-usage
RELEASE=$(date -u +%Y%m%dT%H%M%SZ)-$$
BACKUP="/var/backups/codex-usage/$RELEASE"
install -d -m 0700 "$BACKUP"

# Preserve configuration and the previous immutable release before switching.
if [ -f /etc/codex-usage/server.json ]; then
    cp -a -- /etc/codex-usage/server.json "$BACKUP/server.json"
fi
if [ -L /opt/codex-usage/current ]; then
    PREVIOUS=$(readlink -f -- /opt/codex-usage/current)
    case "$PREVIOUS" in /opt/codex-usage/releases/*) ;; *) echo "Unmanaged previous release; stop for review." >&2; exit 1 ;; esac
    printf '%s\n' "$PREVIOUS" > "$BACKUP/previous-release.txt"
    cp -a -- "$PREVIOUS" "$BACKUP/code"
fi
if [ -f /var/lib/codex-usage/usage.sqlite3 ]; then
    /usr/bin/python3 - "$BACKUP/usage.sqlite3" <<'PY'
import sqlite3
import sys
source = sqlite3.connect('file:/var/lib/codex-usage/usage.sqlite3?mode=ro', uri=True)
target = sqlite3.connect(sys.argv[1])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PY
fi
for existing in /etc/systemd/system/codex-usage.service "$AVAILABLE" "$LEGACY_AVAILABLE" /etc/letsencrypt/renewal-hooks/deploy/codex-usage-reload-nginx; do
    if [ -f "$existing" ]; then
        cp -a -- "$existing" "$BACKUP/$(basename "$existing")"
    fi
done

if [ ! -e /etc/codex-usage/server.json ]; then
    /usr/bin/python3 - <<'PY'
import json
import os
import secrets
path = '/etc/codex-usage/server.json'
with open(path, 'x', encoding='utf-8') as stream:
    json.dump({'admin_key': secrets.token_urlsafe(48)}, stream)
    stream.write('\n')
os.chmod(path, 0o640)
PY
fi
chown root:codex-usage /etc/codex-usage/server.json
chmod 0640 /etc/codex-usage/server.json
# Validate existing credentials without printing them or rotating them.
/usr/bin/python3 - <<'PY'
import json
with open('/etc/codex-usage/server.json', encoding='utf-8') as stream:
    value = json.load(stream).get('admin_key')
if not isinstance(value, str) or not 24 <= len(value) <= 512:
    raise SystemExit('Existing admin key is invalid; restore or repair the private config.')
PY

DEST="/opt/codex-usage/releases/$RELEASE"
install -d -m 0755 "$DEST/web"
# Fixed allowlist: never copy .local, auth files, reports, tests or a database.
for item in server.py collector.py prices.json web/index.html web/app.js web/style.css; do
    install -o root -g root -m 0644 "$SOURCE/$item" "$DEST/$item"
done
install -m 0644 "$MANIFEST" "$DEST/source-manifest.txt"
ln -s -- "$DEST" "/opt/codex-usage/current.$RELEASE"
mv -Tf -- "/opt/codex-usage/current.$RELEASE" /opt/codex-usage/current
install -m 0644 "$ASSETS/codex-usage.service" /etc/systemd/system/codex-usage.service
systemd-analyze verify /etc/systemd/system/codex-usage.service
systemctl daemon-reload
systemctl enable codex-usage.service
systemctl restart codex-usage.service

if [ "$MODE" != none ]; then
    install -d -m 0755 /var/www/letsencrypt/.well-known/acme-challenge
    if [ "$MODE" = bootstrap ]; then VHOST="$ASSETS/nginx-bootstrap.conf"; else VHOST="$ASSETS/nginx.conf"; fi
    install -m 0644 "$VHOST" "$AVAILABLE"
    if [ ! -L "$ENABLED" ]; then ln -s -- "$AVAILABLE" "$ENABLED"; fi
    if [ "$MODE" = tls ]; then
        install -m 0644 "$ASSETS/nginx-legacy.conf" "$LEGACY_AVAILABLE"
        if [ ! -L "$LEGACY_ENABLED" ]; then ln -s -- "$LEGACY_AVAILABLE" "$LEGACY_ENABLED"; fi
    fi
    if ! nginx -t; then
        echo "nginx validation failed: no reload performed. Restore this site's backup before continuing." >&2
        exit 1
    fi
    systemctl reload nginx
fi
if [ "$MODE" = tls ]; then
    install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
    install -m 0755 "$ASSETS/reload-nginx.sh" /etc/letsencrypt/renewal-hooks/deploy/codex-usage-reload-nginx
fi
echo "Installed release $RELEASE; backups: $BACKUP"
echo "Backend: http://127.0.0.1:18765/health; admin credential remains in private /etc/codex-usage/server.json."
