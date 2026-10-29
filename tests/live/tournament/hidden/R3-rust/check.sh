#!/usr/bin/env bash
# Hidden checks of ladder stage R3 (rechner), the same for every language. RC_CMD is the command that runs the
# delivered program (word-split on purpose: "java -cp /tmp/rc rechner.Main"). Output is TAP.
set -u
# a build that failed leaves no program: every check fails instead of the script producing no result
RC_CMD=${RC_CMD:-/nonexistent/rechner}
read -r -a PROG <<<"$RC_CMD"
LIMIT=()
command -v timeout >/dev/null && LIMIT=(timeout 10) # (-1)^9223372036854775807 must not loop
T=$(mktemp -d /tmp/rc-check.XXXXXX)
N=0
FAILED=0
PLAN=27
echo "1..$PLAN"

run() { # run <args...>: sets OUT, ERR, RC
    "${LIMIT[@]}" "${PROG[@]}" "$@" >"$T/out" 2>"$T/err" </dev/null
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

prog() { # prog <file name> <printf format>: writes a program file
    # shellcheck disable=SC2059
    printf "$2" >"$T/$1"
}

expect_out() { # expect_out <name> <expected stdout> <file>
    run "$T/$3"
    if [ "$RC" -eq 0 ] && [ "$OUT" = "$2" ]; then
        result 0 "$1"
    else
        result 1 "$1" "exit $RC; expected:
$2
got:
$OUT
stderr: $ERR"
    fi
}

expect_fail() { # expect_fail <name> <exit code> <expected stdout> <stderr pattern or ""> <args...>
    local name=$1 code=$2 want=$3 pat=$4
    shift 4
    run "$@"
    if [ "$RC" -eq "$code" ] && [ "$OUT" = "$want" ] && { [ -z "$pat" ] || grep -q -- "$pat" "$T/err"; } && [ -n "$ERR" ]; then
        result 0 "$name"
    else
        result 1 "$name" "expected exit $code, stdout '$want', stderr matching '$pat'; got exit $RC
stdout: $OUT
stderr: $ERR"
    fi
}

prog prec.rc 'print 2+3*4\nprint (2+3)*4\nprint 10-4-3\nprint 7-2*3\n'
expect_out "precedence and left associativity" "14
20
3
1" prec.rc

prog pow.rc 'print 2^3^2\nprint -2^2\nprint (-2)^2\nprint 2*-3\nprint --3\nprint 0^0\n'
expect_out "power is right associative and binds tighter than unary minus" "512
-4
4
-6
3
1" pow.rc

prog div.rc 'print -7/2\nprint 7/-2\nprint -7%%2\nprint 7%%-2\nprint -8/2\nprint 7/2\n'
expect_out "division truncates toward zero, remainder takes the sign of the dividend" "-3
-3
-1
1
-4
3" div.rc

prog vars.rc 'let a = 2\nlet b = a*10\nlet a = a+b\nprint a\nprint b\nlet _x1 = 4\nlet y_2 = _x1^2\nprint y_2\n'
expect_out "variables, reassignment, names with digits and underscores" "22
20
16" vars.rc

prog layout.rc '# kommentar\n\n   \n\tprint\t1 +  2\n  # eingerueckt\nprint(4)\n'
expect_out "comments, blank lines, tabs and optional spaces" "3
4" layout.rc

prog crlf.rc 'let x = 1\r\nprint x\r\nprint x+1'
expect_out "CRLF line ends and a last line without newline" "1
2" crlf.rc

prog limits.rc 'print 9223372036854775807\nprint -9223372036854775807-1\nprint (-2)^63\nprint 2^62\nprint 3037000499*3037000499\n'
expect_out "exact 64-bit limits" "9223372036854775807
-9223372036854775808
-9223372036854775808
4611686018427387904
9223372030926249001" limits.rc

prog ones.rc 'print (-1)^9223372036854775807\nprint 1^9223372036854775807\nprint 0^9223372036854775807\n'
expect_out "huge exponents of -1, 1 and 0 answer at once" "-1
1
0" ones.rc

prog minrem.rc 'let m = -9223372036854775807-1\nprint m%%-1\nprint m/1\n'
expect_out "minimum remainder -1 is 0" "0
-9223372036854775808" minrem.rc

prog addov.rc 'print 1\nprint 9223372036854775807+1\nprint 2\n'
expect_fail "addition overflow stops with its line, earlier output stays" 3 "1" "Zeile 2" "$T/addov.rc"

prog mulov.rc 'print 4294967296*4294967296\n'
expect_fail "multiplication overflow" 3 "" "Zeile 1" "$T/mulov.rc"

prog subov.rc 'let m = -9223372036854775807-1\nprint m-1\n'
expect_fail "subtraction overflow" 3 "" "Zeile 2" "$T/subov.rc"

prog divov.rc 'let m = -9223372036854775807-1\nprint m/-1\n'
expect_fail "minimum divided by -1 overflows" 3 "" "Zeile 2" "$T/divov.rc"

prog negov.rc 'let m = -9223372036854775807-1\nprint 0\nprint -m\n'
expect_fail "negating the minimum overflows" 3 "0" "Zeile 3" "$T/negov.rc"

prog powov.rc 'print 2^62\nprint 2^63\n'
expect_fail "power overflow" 3 "4611686018427387904" "Zeile 2" "$T/powov.rc"

prog zero.rc 'print 1\nlet z = 0\nprint 5%%z\nprint 3\n'
expect_fail "remainder by zero is a runtime error" 3 "1" "Zeile 3" "$T/zero.rc"

prog unknown.rc 'print 1\nprint y+1\n'
expect_fail "unknown variable is a runtime error" 3 "1" "Zeile 2" "$T/unknown.rc"

prog negexp.rc 'print 2^-1\n'
expect_fail "negative exponent is a runtime error" 3 "" "Zeile 1" "$T/negexp.rc"

prog incomplete.rc 'print 1\nprint 1 +\n'
expect_fail "incomplete expression: column after the line end, nothing runs" 2 "" "Zeile 2, Spalte 10" "$T/incomplete.rc"

prog char.rc 'print 1 $ 2\n'
expect_fail "unexpected character names its column" 2 "" "Zeile 1, Spalte 9" "$T/char.rc"

prog paren1.rc 'print (1+2\n'
prog paren2.rc 'print 1)\n'
run "$T/paren1.rc"
rc1=$RC out1=$OUT err1=$ERR
run "$T/paren2.rc"
if [ "$rc1" -eq 2 ] && [ -z "$out1" ] && [[ $err1 == *"Zeile 1, Spalte 11"* ]] && [ "$RC" -eq 2 ] && [ -z "$OUT" ] &&
    [[ $ERR == *"Zeile 1, Spalte 8"* ]]; then
    result 0 "missing and surplus parenthesis"
else
    result 1 "missing and surplus parenthesis" "print (1+2: exit $rc1 stdout '$out1' stderr '$err1'; print 1): exit $RC stdout '$OUT' stderr '$ERR'"
fi

prog name1.rc 'let print = 1\n'
prog name2.rc 'let 1x = 2\n'
prog name3.rc 'let x 3\n'
ok=0 detail=""
for pair in "name1.rc:Zeile 1, Spalte 5" "name2.rc:Zeile 1, Spalte 5" "name3.rc:Zeile 1, Spalte 7"; do
    run "$T/${pair%%:*}"
    if [ "$RC" -ne 2 ] || [ -n "$OUT" ] || [[ $ERR != *"${pair#*:}"* ]]; then
        ok=1 detail="$detail${pair%%:*}: exit $RC stderr '$ERR' (want '${pair#*:}'); "
    fi
done
result $ok "keyword, number or missing '=' where let needs a name and '='" "$detail"

prog big1.rc 'print 9223372036854775808\n'
prog big2.rc 'print -9223372036854775808\n'
run "$T/big1.rc"
rc1=$RC err1=$ERR
run "$T/big2.rc"
if [ "$rc1" -eq 2 ] && [[ $err1 == *"Zeile 1, Spalte 7"* ]] && [ "$RC" -eq 2 ] && [[ $ERR == *"Zeile 1, Spalte 8"* ]]; then
    result 0 "number literal beyond the 64-bit range is a syntax error"
else
    result 1 "number literal beyond the 64-bit range is a syntax error" "big1: exit $rc1 stderr '$err1'; big2: exit $RC stderr '$ERR'"
fi

prog stmt.rc 'print 1\n  zeige 1\n'
expect_fail "unknown statement names the column of its first word" 2 "" "Zeile 2, Spalte 3" "$T/stmt.rc"

prog later.rc 'print 1/0\nprint (\n'
expect_fail "a syntax error anywhere wins over runtime errors" 2 "" "Zeile 2, Spalte 8" "$T/later.rc"

expect_fail "missing file names the path" 1 "" "does-not-exist.rc" "$T/does-not-exist.rc"

run
rc1=$RC out1=$OUT
run "$T/prec.rc" "$T/pow.rc"
if [ "$rc1" -eq 4 ] && [ -z "$out1" ] && [ "$RC" -eq 4 ] && [ -z "$OUT" ]; then
    result 0 "no file or two files are usage errors"
else
    result 1 "no file or two files are usage errors" "no file: exit $rc1 stdout '$out1'; two files: exit $RC stdout '$OUT'"
fi

rm -rf "$T"
[ "$N" -eq "$PLAN" ] || echo "# planned $PLAN checks, ran $N"
[ "$FAILED" -eq 0 ]
