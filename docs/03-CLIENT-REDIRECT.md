# Connecting the real client (emulator + redirect)

Goal: get the unmodified game (`com.legacy.titans.diacord.mu.eternal.origin.blade`,
v1.0.16, target SDK 28 / Android 9) to talk to our local server instead of the
dead `*.noyagame.com` / `*.onelinkmobi.com` backends.

## UPDATE 2026-06-14 — emulator choice changed (APK is 32-bit ARM only)

The APK ships **only `armeabi-v7a`** native libs (libmono/libslua/libunity). It
will **not** install on Google x86/x86_64 AVDs (`INSTALL_FAILED_NO_MATCHING_ABIS`
— no 32-bit ARM translation). The Google-APIs-AVD section below is therefore
**superseded**: use a **rooted ARM-capable gaming emulator (LDPlayer / MEmu,
Android 9)**. The hosts + system-CA approach below still applies once rooted; the
only change is the host address.

Concrete facts for this machine:
- **Host PC LAN IP = `192.168.1.5`** (from inside LDPlayer the host is reached
  here, NOT `10.0.2.2` — that alias is only for the AOSP/Google AVD). So use
  `192.168.1.5` for both the hosts redirect and the server-list `domain`.
- adb + emulator live at `~/AppData/Local/Android/Sdk`. Connect to LDPlayer with
  `adb connect 127.0.0.1:5555` (enable Root + ADB in LDPlayer settings).
- **TLS certs generated** (`server/gencert.py`): `ca.pem` (system cert filename
  `acfd89f6.0`), leaf `cert.pem`/`key.pem` with SANs for all the dead domains.
  `bootstrap.py` auto-serves HTTPS now that those exist.
- **Redirect is scripted**: `server/redirect.sh` (adb root → remount → push hosts
  → install CA). Run after `adb connect`. If `remount` fails: `adb disable-verity
  && adb reboot`, then re-run.

LDPlayer setup steps:
1. Install LDPlayer, create an **Android 9** instance.
2. Settings → enable **Root mode** and **ADB** (local connection), then reboot it.
3. `adb connect 127.0.0.1:5555` (port may differ; LDPlayer shows it).
4. Install game: `adb install client/base.apk` then push the OBB (same as below).
5. `HOST_IP=192.168.1.5 ADB=<adb> bash server/redirect.sh` → `adb reboot`.
6. Start servers: `LOD_GATEWAY_HOST=192.168.1.5 python bootstrap.py` + `python gateway.py`.
7. Launch the game; watch `adb logcat -s Unity` + bootstrap console for the real
   request paths, and iterate the stub.

---
## Emulator choice (ORIGINAL PLAN — superseded for the run env, see update above)

**Android Studio AVD — API 28 (Android 9), x86_64, "Google APIs" image (NOT
"Google Play").** Reasons:
- "Google APIs" is **rootable** → `adb root` + `adb remount` → we can edit
  `/system/etc/hosts` and add a **system CA cert**. That lets the unmodified app
  trust our HTTPS and resolve the dead domains to us — no APK patching.
- `adb`/`logcat` give us client-side visibility (the app `DB.Log`s the login
  flow, server ip/port, decode errors).
- From inside the AVD, the host PC is reachable at **`10.0.2.2`**.

(Gaming emulators — LDPlayer/BlueStacks/MEmu — run the game fine and are often
pre-rooted, but are less convenient for `logcat`/system-cert work. Acceptable
fallback if AVD performance is poor.)

## Install steps (once AVD is running)

```
# from the lod/ folder; extract the apk + obb out of the xapk first
adb install com.legacy.titans.diacord.mu.eternal.origin.blade.apk
adb shell mkdir -p /sdcard/Android/obb/com.legacy.titans.diacord.mu.eternal.origin.blade
adb push main.16.com.legacy.titans.diacord.mu.eternal.origin.blade.obb \
    /sdcard/Android/obb/com.legacy.titans.diacord.mu.eternal.origin.blade/
```

## Redirect strategy (unmodified app)

1. **DNS**: map the API domains to the host PC.
   ```
   adb root && adb remount
   # append to /system/etc/hosts (pull, edit, push):
   10.0.2.2  api.ttus.noyagame.com
   10.0.2.2  data.ttus.noyagame.com
   10.0.2.2  ttapi.onelinkmobi.com
   10.0.2.2  ttdata.tten.onelinkmobi.com
   ```
   (Exact domains per `Resources/luascript/area_info.txt`; the app picks one
   "area" — we only need to redirect the area we log into.)
2. **HTTPS trust**: the API base URLs are `https://`. On target-SDK 28 the app
   trusts only **system** CAs by default. Install our self-signed CA into
   `/system/etc/security/cacerts/` (hash-named) on the writable AVD system. Then
   our HTTPS bootstrap server is trusted.
   - *Check first*: pull the APK's `AndroidManifest.xml` / `res/xml/network_security_config`
     — if it allows user certs or cleartext, we can skip the system-cert step.
3. **Gateway is raw TCP** (not HTTP), so it isn't subject to the cleartext-HTTP
   policy. Our HTTP server-list response just needs to return the gateway as
   `ip=10.0.2.2, port=7001` and the client will connect there.

## The HTTP bootstrap contract (M1) — what we must serve

Flow (from `common/servermgr.txt`, `game/login/*`):
- App fetches *package info* + *server list* from the area's API base.
- Responses are **obfuscated, not plain JSON**. `DecodeSerInfo` (servermgr.txt:322)
  reverses: `<index-prefix> + ZZBase64( ... ZZBase64(json) ... )` with `|`↔`=`
  substitution, applied **twice**. Our stub must produce the matching encoding.
  → Plan: reuse the client's own `ZZBase64` + inverse of `DecodeSerInfo` via the
    lupa runtime; unit-test by `encode(x)` → `DecodeSerInfo` == `x`.
- Decoded server entry fields referenced in code: `domain`, `ip`, `port`,
  `servertypeid`, `name`, `status`, plus top-level `area`, `recom`, `avenger`,
  `star`, `fb`, `status` (see `SetGlobalValue`, `OnHttpPackageInfo`). Final field
  list to be confirmed empirically from logcat + `biz_selectServer` /
  `biz_createRole_mgr`.

## Empirical loop (why the emulator unblocks M1)

The original servers are dead, so we can't capture a real response to copy.
Instead: point the app at our stub, watch `logcat` for the app's requests and
its `DB.LogError` decode failures, and iterate the stub until the server list
renders and "connect" fires at `10.0.2.2:7001` (where `gateway.py` is already
working). This observe-and-match loop is far cheaper than reversing every field
blind.

## Status / next actions

- [ ] **User**: install Android Studio, create the API 28 x86_64 *Google APIs*
      AVD, boot it, confirm `adb devices` lists it.
- [ ] **User**: extract `*.apk` + `*.obb` from the `.xapk` (it's a zip) — or ask
      me to script it.
- [ ] **Me (next)**: build `server/bootstrap.py` (HTTPS server-list stub) incl.
      the `DecodeSerInfo` inverse encoder (reuse client `ZZBase64`); generate the
      CA cert; write the hosts + cert install helper script.
- [ ] **Together**: boot the game against the stub, read logcat, iterate until it
      connects to our gateway and shows character-select.
