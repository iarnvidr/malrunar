#!/usr/bin/env bash
# Checks a built macOS bundle: identity, architectures, usage descriptions, ad-hoc signature.
#   tests/e2e/verify-macos.sh Name.app Name.dmg IDENTIFIER DISPLAY_NAME "ARCHS"
# Gatekeeper is expected to reject the app (it is not notarised); that is printed, not failed on.
set -euo pipefail

app="$1" dmg="$2" identifier="$3" name="$4" archs="$5"
plist="$app/Contents/Info.plist"
read_key() { /usr/libexec/PlistBuddy -c "Print :$1" "$plist"; }
fail() { echo "verify: $*" >&2; exit 1; }

[ "$(read_key CFBundleIdentifier)" = "$identifier" ] || fail "bundle identifier is $(read_key CFBundleIdentifier), expected $identifier"
[ "$(read_key CFBundleName)" = "$name" ] || fail "bundle name is $(read_key CFBundleName), expected $name"
read_key NSCameraUsageDescription | grep -q . || fail "NSCameraUsageDescription missing"
read_key NSMicrophoneUsageDescription | grep -q . || fail "NSMicrophoneUsageDescription missing"
[ -f "$app/Contents/Resources/icon.icns" ] || fail "icon.icns missing from the bundle"

executable="$app/Contents/MacOS/$(read_key CFBundleExecutable)"
found="$(lipo -archs "$executable" | tr ' ' '\n' | sort | tr '\n' ' ')"
wanted="$(printf '%s\n' $archs | sort | tr '\n' ' ')"
[ "$found" = "$wanted" ] || fail "architectures are '$found', expected '$wanted'"

codesign --verify --deep --strict --verbose=2 "$app"
signature="$(codesign -dv --verbose=2 "$app" 2>&1)"
echo "$signature"
echo "$signature" | grep -q 'Signature=adhoc' || fail "the app is not ad-hoc signed"

[ -s "$dmg" ] || fail "disk image missing"
hdiutil verify "$dmg"

echo "Gatekeeper assessment (a rejection is expected for an unsigned, un-notarised build):"
spctl --assess --type execute --verbose "$app" 2>&1 || true
echo "verify ok: $identifier, $found"
