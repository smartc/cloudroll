#!/bin/sh
# Install CloudRoll as a systemd service behind an Apache HTTPS proxy.
#
#   sudo sh deploy/install.sh <hostname> <cert-dir>
#
#   hostname  public name for the site, e.g. cloudroll.example.com
#   cert-dir  directory holding fullchain.pem / privkey.pem,
#             e.g. /etc/letsencrypt/live/example.com
#
# The service runs as the user who invoked sudo, from this checkout.
set -e
if [ $# -ne 2 ]; then
    sed -n '3,9p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
fi
HOSTNAME_ARG=$1
CERT_DIR=$2
cd "$(dirname "$0")"
APP_DIR=$(cd .. && pwd)
RUN_USER=${SUDO_USER:-$(id -un)}

# Stop any manually started instance so the service can bind port 8090.
pkill -f "python -m cloudroll run" || true
sleep 1

sed -e "s#@USER@#$RUN_USER#g" -e "s#@DIR@#$APP_DIR#g" cloudroll.service \
    > /etc/systemd/system/cloudroll.service
systemctl daemon-reload
systemctl enable --now cloudroll
systemctl restart cloudroll

sed -e "s#@HOSTNAME@#$HOSTNAME_ARG#g" -e "s#@CERT_DIR@#$CERT_DIR#g" apache-cloudroll.conf \
    > /etc/apache2/sites-available/cloudroll.conf
a2ensite -q cloudroll
apache2ctl configtest
systemctl reload apache2

sleep 3
systemctl --no-pager --lines=5 status cloudroll
echo
curl -s -o /dev/null -w "local  http://127.0.0.1:8090/  -> %{http_code}\n" http://127.0.0.1:8090/
curl -s -o /dev/null -w "proxy  https://$HOSTNAME_ARG/ -> %{http_code}\n" "https://$HOSTNAME_ARG/"
