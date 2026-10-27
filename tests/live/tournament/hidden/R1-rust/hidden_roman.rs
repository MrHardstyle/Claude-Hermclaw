// Hidden checks of ladder task R1-rust (roman numerals). Copied to tests/ and run with `cargo test --test hidden_roman`.
use roemisch::{from_roman, to_roman};

const PAIRS: &[(u32, &str)] = &[
    (1, "I"), (4, "IV"), (9, "IX"), (14, "XIV"), (40, "XL"), (90, "XC"),
    (400, "CD"), (1994, "MCMXCIV"), (2026, "MMXXVI"), (3999, "MMMCMXCIX"),
];

#[test]
fn hidden_to_roman_known_values() {
    for (n, s) in PAIRS {
        assert_eq!(to_roman(*n).as_deref(), Ok(*s), "to_roman({n})");
    }
}

#[test]
fn hidden_from_roman_known_values() {
    for (n, s) in PAIRS {
        assert_eq!(from_roman(s), Ok(*n), "from_roman({s})");
    }
}

#[test]
fn hidden_to_roman_rejects_out_of_range() {
    assert!(to_roman(0).is_err());
    assert!(to_roman(4000).is_err());
}

#[test]
fn hidden_from_roman_rejects_non_canonical() {
    for s in ["", "IIII", "IC", "VV", "MMMM", "IM", "XLX", "mcm", "ABC", " X", "X "] {
        assert!(from_roman(s).is_err(), "from_roman({s:?}) must fail");
    }
}

#[test]
fn hidden_round_trip() {
    for n in 1..=3999 {
        assert_eq!(from_roman(&to_roman(n).unwrap()), Ok(n), "round trip {n}");
    }
}
