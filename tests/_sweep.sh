#!/usr/bin/env bash
# Hygiene lint. One command, run before a commit (and by the pre-push hook).
# Flags things that must not ship in source:
#   - dash-like unicode (em/en/figure/bar/box) and stray unicode arrows
#     (keep source ASCII so diffs and terminals stay clean)
#   - invisible / zero-width / bidi / variation-selector chars (watermark vector)
#   - likely secrets / PII: non-example IPv4, real-looking emails, private keys,
#     MAC addresses
#   - tracked files that name a private provider or private infra word
# Exits 0 when clean, 1 when anything is found. Add patterns here, not in your head.
set -u
cd "$(dirname "$0")/.." || exit 2   # repo root (this script lives in tests/)

# Lines marked "sweep-ok" are excluded from all checks - for a line that
# legitimately needs a flagged token (a load-bearing glyph, a protocol constant,
# an example address). Works from a code comment (`# sweep-ok`) or a markdown/HTML
# comment (`<!-- sweep-ok -->`).
nosweep() { grep -Fv 'sweep-ok'; }

# Files to scan: git-tracked source/docs (includes anything staged for the commit).
# Falls back to a full working-tree scan outside a git repo. The sweep excludes
# itself (it holds the patterns).
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  mapfile -t FILES < <(git ls-files -- '*.py' '*.md' '*.sh' '*.toml' | grep -vx 'tests/_sweep.sh' | sort)
else
  mapfile -t FILES < <(find . \
    \( -name '*.py' -o -name '*.md' -o -name '*.sh' -o -name '*.toml' \) \
    -not -name '_sweep.sh' \
    -not -path '*/__pycache__/*' \
    -not -path '*/local/*' -not -path '*/dev/*' | sort)
fi

fails=0
report() {  # name, hits  (hits passed as an arg, not piped: keeps the counter
            # in this shell - a piped last stage would run in a subshell)
  local name="$1" hits="$2"
  if [ -n "$hits" ]; then
    echo "FAIL: $name"
    printf '%s\n' "$hits" | sed 's/^/    /'
    fails=$((fails + 1))
  else
    echo "ok:   $name"
  fi
}

# --- ASCII hygiene ---------------------------------------------------------
# No -o: keep the whole line so a `sweep-ok` marker on it survives to nosweep.
report "dash-like unicode (use ASCII -)" \
  "$(grep -rnP "[\x{2012}-\x{2015}\x{2212}\x{2500}]" "${FILES[@]}" | nosweep)"

report "unicode arrows (use ASCII -> )" \
  "$(grep -rnP "[\x{2190}-\x{21FF}\x{2794}\x{27A1}]" "${FILES[@]}" | nosweep)"

# Invisible / zero-width / format-control / bidi / variation-selector / unicode-tag
# chars: zero legit use in source, and the exact vector for a fingerprint/watermark
# smuggled into tracked source.
report "invisible / zero-width / control unicode (watermark/smuggle vector)" \
  "$(grep -rnP "[\x{200b}-\x{200f}\x{2060}-\x{2064}\x{feff}\x{00ad}\x{061c}\x{180e}\x{202a}-\x{202e}\x{2066}-\x{2069}\x{fe00}-\x{fe0f}\x{E0000}-\x{E007f}]" "${FILES[@]}" | nosweep)"

# --- likely secrets / PII --------------------------------------------------
# Any dotted-quad that is NOT localhost, 0.0.0.0, or an RFC 5737 doc range
# (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24). Use the doc ranges in examples.
# Whole lines are kept (not grep -o) so a `sweep-ok` marker on the line works; the
# per-IP allow-list is then applied to the IPs found on each surviving line.
_ALLOW_IP='^(127\.0\.0\.1|0\.0\.0\.0|192\.0\.2\.[0-9]{1,3}|198\.51\.100\.[0-9]{1,3}|203\.0\.113\.[0-9]{1,3})$'
ip_hits() {
  grep -rnE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' "${FILES[@]}" | nosweep | while IFS= read -r ln; do
    if printf '%s\n' "$ln" | grep -oE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' \
         | grep -vqE "$_ALLOW_IP"; then printf '%s\n' "$ln"; fi
  done
}
report "non-example IPv4 (use 192.0.2.x / 198.51.100.x / 203.0.113.x)" "$(ip_hits)"

# Real-looking email addresses (allow example.com and *.example placeholders).
email_hits() {
  grep -rnE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' "${FILES[@]}" | nosweep | while IFS= read -r ln; do
    if printf '%s\n' "$ln" | grep -oE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' \
         | grep -viqE '@example\.|\.example$|@host|@functools\.'; then printf '%s\n' "$ln"; fi
  done
}
report "email addresses (use user@host or *.example)" "$(email_hits)"

report "private keys / public-key material" \
  "$(grep -rniE 'BEGIN [A-Z0-9 ]*PRIVATE KEY|ssh-rsa AAAA|ssh-ed25519 AAAA' "${FILES[@]}" | nosweep)"

report "MAC addresses" \
  "$(grep -rniE '\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b' "${FILES[@]}" | nosweep)"

# --- private-name / private-word guard --------------------------------------
# Names of private providers / infra live ONLY in the gitignored tests/.private_names
# and tests/.private_words (one per line). Any TRACKED file that mentions one fails.
# A public checkout has no list, so this is skipped there.
if [ -f tests/.private_names ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  report "private name in a tracked file" \
    "$(git ls-files -z | xargs -0 grep -nIiF -f <(grep -v '^[[:space:]]*$' tests/.private_names) 2>/dev/null | nosweep)"
fi
if [ -f tests/.private_words ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  report "private infra/employer word in a tracked file" \
    "$(git ls-files -z | xargs -0 grep -nIiwF -f <(grep -v '^[[:space:]]*$' tests/.private_words) 2>/dev/null | nosweep)"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "SWEEP CLEAN"
  exit 0
fi
echo "SWEEP FOUND $fails categor(y/ies) of issues"
exit 1
