// Hidden checks of ladder task R1-cpp (roman numerals).
#include <gtest/gtest.h>

#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "roemisch.h"

static const std::vector<std::pair<int, std::string>> kPairs = {
    {1, "I"}, {4, "IV"}, {9, "IX"}, {14, "XIV"}, {40, "XL"}, {90, "XC"},
    {400, "CD"}, {1994, "MCMXCIV"}, {2026, "MMXXVI"}, {3999, "MMMCMXCIX"}};

TEST(Hidden, ToRomanKnownValues) {
    for (const auto& [n, s] : kPairs) EXPECT_EQ(roemisch::to_roman(n), s) << n;
}

TEST(Hidden, FromRomanKnownValues) {
    for (const auto& [n, s] : kPairs) EXPECT_EQ(roemisch::from_roman(s), n) << s;
}

TEST(Hidden, ToRomanRejectsOutOfRange) {
    for (int n : {0, -1, 4000}) EXPECT_THROW(roemisch::to_roman(n), std::invalid_argument) << n;
}

TEST(Hidden, FromRomanRejectsNonCanonical) {
    for (const char* s : {"", "IIII", "IC", "VV", "MMMM", "IM", "XLX", "mcm", "ABC", " X", "X "})
        EXPECT_THROW(roemisch::from_roman(s), std::invalid_argument) << '"' << s << '"';
}

TEST(Hidden, RoundTrip) {
    for (int n = 1; n <= 3999; ++n) ASSERT_EQ(roemisch::from_roman(roemisch::to_roman(n)), n) << n;
}
