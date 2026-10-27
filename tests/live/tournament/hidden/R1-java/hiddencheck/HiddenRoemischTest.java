// Hidden checks of ladder task R1-java (roman numerals); run with --select-package hiddencheck.
package hiddencheck;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;
import roemisch.Roemisch;

class HiddenRoemischTest {
    static final Object[][] PAIRS = {
        {1, "I"}, {4, "IV"}, {9, "IX"}, {14, "XIV"}, {40, "XL"}, {90, "XC"},
        {400, "CD"}, {1994, "MCMXCIV"}, {2026, "MMXXVI"}, {3999, "MMMCMXCIX"}};

    @Test void toRomanKnownValues() {
        for (Object[] p : PAIRS) assertEquals(p[1], Roemisch.toRoman((Integer) p[0]), "toRoman(" + p[0] + ")");
    }

    @Test void fromRomanKnownValues() {
        for (Object[] p : PAIRS) assertEquals(p[0], Roemisch.fromRoman((String) p[1]), "fromRoman(" + p[1] + ")");
    }

    @Test void toRomanRejectsOutOfRange() {
        for (int n : new int[] {0, -1, 4000}) assertThrows(IllegalArgumentException.class, () -> Roemisch.toRoman(n));
    }

    @Test void fromRomanRejectsNonCanonical() {
        for (String s : new String[] {"", "IIII", "IC", "VV", "MMMM", "IM", "XLX", "mcm", "ABC", " X", "X "})
            assertThrows(IllegalArgumentException.class, () -> Roemisch.fromRoman(s), "fromRoman(\"" + s + "\")");
    }

    @Test void fromRomanRejectsNull() {
        assertThrows(IllegalArgumentException.class, () -> Roemisch.fromRoman(null));
    }

    @Test void roundTrip() {
        for (int n = 1; n <= 3999; n++) assertEquals(n, Roemisch.fromRoman(Roemisch.toRoman(n)));
    }
}
