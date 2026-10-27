#!/usr/bin/env bats
# Hidden checks of ladder task R1-bash (roman numerals): the delivered ./roemisch.sh, run as a program.

bats_require_minimum_version 1.5.0

setup() { cd "$BATS_TEST_DIRNAME/.." || exit 1; }

@test "hidden to known values" {
  for pair in 1:I 4:IV 9:IX 14:XIV 40:XL 90:XC 400:CD 1994:MCMXCIV 2026:MMXXVI 3999:MMMCMXCIX; do
    run bash ./roemisch.sh to "${pair%%:*}"
    [ "$status" -eq 0 ] && [ "$output" = "${pair#*:}" ] || { echo "to ${pair%%:*} -> $status '$output'"; return 1; }
  done
}

@test "hidden from known values" {
  for pair in 1:I 4:IV 9:IX 14:XIV 40:XL 90:XC 400:CD 1994:MCMXCIV 2026:MMXXVI 3999:MMMCMXCIX; do
    run bash ./roemisch.sh from "${pair#*:}"
    [ "$status" -eq 0 ] && [ "$output" = "${pair%%:*}" ] || { echo "from ${pair#*:} -> $status '$output'"; return 1; }
  done
}

@test "hidden to rejects out of range on stderr with exit code 1" {
  for n in 0 -1 4000 abc; do
    run --separate-stderr bash ./roemisch.sh to "$n"
    [ "$status" -eq 1 ] && [ -z "$output" ] && [ -n "$stderr" ] || { echo "to $n -> $status '$output'"; return 1; }
  done
}

@test "hidden from rejects non-canonical input on stderr with exit code 1" {
  for s in "" IIII IC VV MMMM IM XLX mcm ABC " X"; do
    run --separate-stderr bash ./roemisch.sh from "$s"
    [ "$status" -eq 1 ] && [ -z "$output" ] && [ -n "$stderr" ] || { echo "from '$s' -> $status '$output'"; return 1; }
  done
}

@test "hidden wrong usage exits 2" {
  run bash ./roemisch.sh
  [ "$status" -eq 2 ]
  run bash ./roemisch.sh sideways 5
  [ "$status" -eq 2 ]
}

@test "hidden functions can be sourced without running main" {
  run bash -c 'source ./roemisch.sh && to_roman 2026 && from_roman XIV'
  [ "$status" -eq 0 ] && [ "$output" = $'MMXXVI\n14' ]
}

@test "hidden round trip 1..3999" {
  run bash -c 'source ./roemisch.sh; for ((n=1; n<=3999; n++)); do [ "$(from_roman "$(to_roman "$n")")" = "$n" ] || { echo "$n"; exit 1; }; done'
  [ "$status" -eq 0 ]
}
