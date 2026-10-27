-- Hidden checks of ladder task R1-lua (roman numerals); run from the repository root with `busted _hidden`.
local roemisch = require("roemisch")

local pairs_ = {
  {1, "I"}, {4, "IV"}, {9, "IX"}, {14, "XIV"}, {40, "XL"}, {90, "XC"},
  {400, "CD"}, {1994, "MCMXCIV"}, {2026, "MMXXVI"}, {3999, "MMMCMXCIX"},
}

describe("hidden roemisch", function()
  it("to_roman known values", function()
    for _, p in ipairs(pairs_) do assert.are.equal(p[2], (roemisch.to_roman(p[1]))) end
  end)

  it("from_roman known values", function()
    for _, p in ipairs(pairs_) do assert.are.equal(p[1], (roemisch.from_roman(p[2]))) end
  end)

  it("to_roman rejects out of range with nil and a message", function()
    for _, n in ipairs({0, -1, 4000, 2.5}) do
      local value, err = roemisch.to_roman(n)
      assert.is_nil(value)
      assert.is_string(err)
    end
  end)

  it("from_roman rejects non-canonical input with nil and a message", function()
    for _, s in ipairs({"", "IIII", "IC", "VV", "MMMM", "IM", "XLX", "mcm", "ABC", " X", "X "}) do
      local value, err = roemisch.from_roman(s)
      assert.is_nil(value, s)
      assert.is_string(err)
    end
  end)

  it("round trip", function()
    for n = 1, 3999 do assert.are.equal(n, (roemisch.from_roman((roemisch.to_roman(n))))) end
  end)
end)
