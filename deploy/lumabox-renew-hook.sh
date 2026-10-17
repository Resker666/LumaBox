#!/bin/sh
set -eu
if [ "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/lumabox.resker666.cloud ]; then
    /usr/sbin/nginx -t -c /etc/nginx/ghost-chat.conf
    /bin/systemctl reload nginx
fi
