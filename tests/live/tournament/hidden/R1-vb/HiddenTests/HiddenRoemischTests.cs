// Hidden checks of the roman numeral ladder task (C# and VB.NET deliver the same API: Roemisch.RoemischeZahlen).
using Roemisch;

public class HiddenRoemischTests
{
    public static readonly TheoryData<int, string> Pairs = new()
    {
        { 1, "I" }, { 4, "IV" }, { 9, "IX" }, { 14, "XIV" }, { 40, "XL" }, { 90, "XC" },
        { 400, "CD" }, { 1994, "MCMXCIV" }, { 2026, "MMXXVI" }, { 3999, "MMMCMXCIX" },
    };

    [Fact]
    public void ToRomanKnownValues()
    {
        foreach (var row in Pairs) Assert.Equal((string)row[1], RoemischeZahlen.ToRoman((int)row[0]));
    }

    [Fact]
    public void FromRomanKnownValues()
    {
        foreach (var row in Pairs) Assert.Equal((int)row[0], RoemischeZahlen.FromRoman((string)row[1]));
    }

    [Fact]
    public void ToRomanRejectsOutOfRange()
    {
        foreach (var n in new[] { 0, -1, 4000 }) Assert.ThrowsAny<ArgumentException>(() => RoemischeZahlen.ToRoman(n));
    }

    [Fact]
    public void FromRomanRejectsNonCanonical()
    {
        foreach (var s in new[] { "", "IIII", "IC", "VV", "MMMM", "IM", "XLX", "mcm", "ABC", " X", "X " })
            Assert.ThrowsAny<ArgumentException>(() => RoemischeZahlen.FromRoman(s));
    }

    [Fact]
    public void FromRomanRejectsNull()
    {
        Assert.ThrowsAny<ArgumentException>(() => RoemischeZahlen.FromRoman(null!));
    }

    [Fact]
    public void RoundTrip()
    {
        for (var n = 1; n <= 3999; n++) Assert.Equal(n, RoemischeZahlen.FromRoman(RoemischeZahlen.ToRoman(n)));
    }
}
