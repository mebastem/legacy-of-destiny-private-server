#!/usr/bin/env bash
# Redirect the game client to our local server on a ROOTED emulator (LDPlayer/MEmu/Genymotion).
# Maps the dead API domains -> our PC, and installs our CA into the system trust store
# so the unmodified app trusts our HTTPS bootstrap.
#
# Usage:
#   HOST_IP=192.168.1.5 ADB="/c/Users/<you>/AppData/Local/Android/Sdk/platform-tools/adb.exe" \
#     bash redirect.sh
#
# Notes:
#  - run from the server/ dir (needs the generated <hash>.0 CA file -> run gencert.py first)
#  - LDPlayer: enable Root + ADB in settings; connect with `adb connect 127.0.0.1:5555`
#  - if `adb remount` fails, run `adb disable-verity` then `adb reboot`, then re-run this.
set -e
export MSYS_NO_PATHCONV=1                       # stop Git Bash mangling /system/... paths

ADB=${ADB:-adb}
HOST_IP=${HOST_IP:-192.168.1.5}
DOMAINS="api.ttus.noyagame.com data.ttus.noyagame.com api.tten.noyagame.com data.tten.noyagame.com ttapi.onelinkmobi.com ttdata.tten.onelinkmobi.com"
CERT_HASH=$(ls *.0 2>/dev/null | head -1)
[ -z "$CERT_HASH" ] && { echo "no <hash>.0 CA file; run: python gencert.py"; exit 1; }

echo "== gaining root + writable /system =="
"$ADB" root || true
sleep 2
"$ADB" remount || { echo "remount failed -> try: $ADB disable-verity && $ADB reboot, then re-run"; exit 1; }

echo "== redirecting domains -> $HOST_IP =="
TMP=$(mktemp)
"$ADB" shell cat /system/etc/hosts > "$TMP" 2>/dev/null || true
for d in $DOMAINS; do
  grep -q "[[:space:]]$d\$" "$TMP" || printf '%s %s\n' "$HOST_IP" "$d" >> "$TMP"
done
"$ADB" push "$TMP" /system/etc/hosts
rm -f "$TMP"

echo "== installing system CA ($CERT_HASH) =="
"$ADB" push "$CERT_HASH" /system/etc/security/cacerts/
"$ADB" shell chmod 644 /system/etc/security/cacerts/"$CERT_HASH"

echo "== verifying =="
echo "-- hosts --"; "$ADB" shell cat /system/etc/hosts | grep noyagame || true
echo "-- cacert present --"; "$ADB" shell ls -l /system/etc/security/cacerts/"$CERT_HASH"
echo
echo "DONE. Reboot for the CA to take effect:  $ADB reboot"
echo "Then start servers on the PC:"
echo "  LOD_GATEWAY_HOST=$HOST_IP python bootstrap.py        # https on :443"
echo "  python gateway.py                                    # tcp :7001"
