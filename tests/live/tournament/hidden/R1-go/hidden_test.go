// Hidden checks of ladder task R1-go (roman numerals). Copied next to the package and run with -run '^TestHidden'.
package roemisch

import "testing"

var hiddenPairs = []struct {
	n int
	s string
}{{1, "I"}, {4, "IV"}, {9, "IX"}, {14, "XIV"}, {40, "XL"}, {90, "XC"}, {400, "CD"}, {1994, "MCMXCIV"}, {2026, "MMXXVI"}, {3999, "MMMCMXCIX"}}

func TestHiddenToRomanKnownValues(t *testing.T) {
	for _, p := range hiddenPairs {
		if got, err := ToRoman(p.n); err != nil || got != p.s {
			t.Errorf("ToRoman(%d) = %q, %v; want %q", p.n, got, err, p.s)
		}
	}
}

func TestHiddenFromRomanKnownValues(t *testing.T) {
	for _, p := range hiddenPairs {
		if got, err := FromRoman(p.s); err != nil || got != p.n {
			t.Errorf("FromRoman(%q) = %d, %v; want %d", p.s, got, err, p.n)
		}
	}
}

func TestHiddenToRomanRejectsOutOfRange(t *testing.T) {
	for _, n := range []int{0, -1, 4000} {
		if _, err := ToRoman(n); err == nil {
			t.Errorf("ToRoman(%d) must fail", n)
		}
	}
}

func TestHiddenFromRomanRejectsNonCanonical(t *testing.T) {
	for _, s := range []string{"", "IIII", "IC", "VV", "MMMM", "IM", "XLX", "mcm", "ABC", " X", "X "} {
		if _, err := FromRoman(s); err == nil {
			t.Errorf("FromRoman(%q) must fail", s)
		}
	}
}

func TestHiddenRoundTrip(t *testing.T) {
	for n := 1; n <= 3999; n++ {
		s, err := ToRoman(n)
		if err != nil {
			t.Fatalf("ToRoman(%d): %v", n, err)
		}
		if back, err := FromRoman(s); err != nil || back != n {
			t.Fatalf("round trip %d -> %q -> %d, %v", n, s, back, err)
		}
	}
}
