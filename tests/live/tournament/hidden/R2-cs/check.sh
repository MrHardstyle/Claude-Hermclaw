#!/usr/bin/env bash
# Hidden checks of ladder stage R2 (kassenbuch), the same for every language. KB_CMD is the command that runs the
# delivered program (word-split on purpose: "java -cp /tmp/kc kassenbuch.Main"). Output is TAP.
set -u
# a build that failed leaves no program: every check fails instead of the script producing no result
KB_CMD=${KB_CMD:-/nonexistent/kassenbuch}
read -r -a KB <<<"$KB_CMD"
T=$(mktemp -d /tmp/kb-check.XXXXXX)
N=0
FAILED=0
PLAN=16
echo "1..$PLAN"

run() { # run <args...>: sets OUT, ERR, RC
    "${KB[@]}" "$@" >"$T/out" 2>"$T/err" </dev/null
    RC=$?
    OUT=$(cat "$T/out")
    ERR=$(cat "$T/err")
}

result() { # result <ok 0|1> <name> [detail]
    N=$((N + 1))
    if [ "$1" -eq 0 ]; then
        echo "ok $N - $2"
    else
        FAILED=$((FAILED + 1))
        echo "not ok $N - $2"
        [ -n "${3:-}" ] && printf '%s\n' "$3" | sed 's/^/# /'
    fi
}

expect_out() { # expect_out <name> <expected stdout> <args...>
    local name=$1 want=$2
    shift 2
    run "$@"
    if [ "$RC" -eq 0 ] && [ "$OUT" = "$want" ]; then
        result 0 "$name"
    else
        result 1 "$name" "exit $RC; expected:
$want
got:
$OUT
stderr: $ERR"
    fi
}

expect_fail() { # expect_fail <name> <exit code> <stderr pattern or ""> <args...>
    local name=$1 code=$2 pat=$3
    shift 3
    run "$@"
    if [ "$RC" -eq "$code" ] && [ -z "$OUT" ] && { [ -z "$pat" ] || grep -q -- "$pat" "$T/err"; } && [ -n "$ERR" ]; then
        result 0 "$name"
    else
        result 1 "$name" "expected exit $code, no stdout, stderr matching '$pat'; got exit $RC
stdout: $OUT
stderr: $ERR"
    fi
}

printf 'datum;kategorie;betrag\n2024-03-01;Miete;-850,00\n2024-03-02;Lebensmittel;-42,37\n2024-03-05;Gehalt;2500,00\n2024-03-09;Lebensmittel;-17,63\n2024-04-01;Miete;-850,00\n' >"$T/basic.csv"
expect_out "sums per category sorted, total last" "Gehalt: 2500,00
Lebensmittel: -60,00
Miete: -1700,00
GESAMT: 740,00" "$T/basic.csv"

expect_out "month filter" "Gehalt: 2500,00
Lebensmittel: -60,00
Miete: -850,00
GESAMT: 1590,00" "$T/basic.csv" --monat 2024-03

expect_out "month without entries prints only the total" "GESAMT: 0,00" "$T/basic.csv" --monat 2023-12

printf 'datum;kategorie;betrag\n2024-01-01;A;0,10\n2024-01-02;A;0,20\n2024-01-03;B;-0,50\n2024-01-04;C;5,00\n2024-01-05;C;-5,00\n2024-01-06;D;-3\n2024-01-07;D;1234567,89\n' >"$T/cents.csv"
expect_out "exact cents, negative below one, zero without sign, whole amounts" "A: 0,30
B: -0,50
C: 0,00
D: 1234564,89
GESAMT: 1234564,69" "$T/cents.csv"

printf 'datum;kategorie;betrag\n\n2024-05-01;  Reisen ;-10,00\n\n2024-05-02;Reisen;-5,50' >"$T/blank.csv"
expect_out "blank lines ignored, category trimmed, last line without newline" "Reisen: -15,50
GESAMT: -15,50" "$T/blank.csv"

printf 'datum;kategorie;betrag\r\n2024-06-01;Bus;-2,80\r\n2024-06-02;B\xc3\xbccher;-19,90\r\n2024-06-03;Auto;-60,00\r\n2024-06-04;auto;-1,00\r\n' >"$T/crlf.csv"
expect_out "CRLF line ends, UTF-8 category, ordinal sort" "Auto: -60,00
Bus: -2,80
Bücher: -19,90
auto: -1,00
GESAMT: -83,70" "$T/crlf.csv"

printf 'datum;kategorie;betrag\n2024-02-29;Schaltjahr;1,00\n' >"$T/leap.csv"
expect_out "29 February of a leap year is valid" "Schaltjahr: 1,00
GESAMT: 1,00" "$T/leap.csv"

printf 'datum;kategorie;wert\n2024-01-01;A;1,00\n' >"$T/header.csv"
expect_fail "wrong header is a data error in line 1" 2 "Zeile 1" "$T/header.csv"

printf 'datum;kategorie;betrag\n2024-01-01;A;1,00\n2024-01-02;A;12,5\n' >"$T/amount.csv"
expect_fail "amount with one decimal is rejected with its line" 2 "Zeile 3" "$T/amount.csv"

printf 'datum;kategorie;betrag\n2024-01-01;A;1.000,00\n' >"$T/thousands.csv"
expect_fail "thousands separator is rejected" 2 "Zeile 2" "$T/thousands.csv"

printf 'datum;kategorie;betrag\n2024-01-01;A;1,00\n\n2023-02-29;A;1,00\n' >"$T/date.csv"
expect_fail "29 February of a common year is rejected (physical line number)" 2 "Zeile 4" "$T/date.csv"

printf 'datum;kategorie;betrag\n2024-01-01;   ;1,00\n' >"$T/emptycat.csv"
expect_fail "empty category is rejected" 2 "Zeile 2" "$T/emptycat.csv"

printf 'datum;kategorie;betrag\n2024-01-01;A;1,00;extra\n' >"$T/fields.csv"
expect_fail "line with four fields is rejected" 2 "Zeile 2" "$T/fields.csv"

expect_fail "missing file names the path" 1 "does-not-exist.csv" "$T/does-not-exist.csv"

expect_fail "no arguments is a usage error" 3 ""

run "$T/basic.csv" --monat 2024-13
rc1=$RC out1=$OUT
run "$T/basic.csv" --jahr 2024
rc2=$RC out2=$OUT
if [ "$rc1" -eq 3 ] && [ -z "$out1" ] && [ "$rc2" -eq 3 ] && [ -z "$out2" ]; then
    result 0 "invalid month and unknown option are usage errors"
else
    result 1 "invalid month and unknown option are usage errors" "--monat 2024-13: exit $rc1 stdout '$out1'; --jahr: exit $rc2 stdout '$out2'"
fi

rm -rf "$T"
[ "$N" -eq "$PLAN" ] || echo "# planned $PLAN checks, ran $N"
[ "$FAILED" -eq 0 ]
