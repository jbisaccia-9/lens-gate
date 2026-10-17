from lensgate import numbers


def test_exact_after_formatting_only():
    avail = numbers.sources([412330.5, 4.2, -3.1, "2026-09-28"])
    assert numbers.unsourced("412,330.50 and 4.2% and -3.1% in 2026-09", avail) == []
    assert numbers.unsourced("about 412,331", avail) == ["412,331"]
    assert numbers.unsourced("roughly 4%", avail) == ["4%"]


def test_sign_is_part_of_the_value():
    avail = numbers.sources([-3.1])
    assert numbers.unsourced("down 3.1%", avail) == ["3.1%"]


def test_labels_are_not_numbers():
    assert numbers.extract("Q3 results for v2 of the H1 plan") == []


def test_abbreviated_figures_are_unsourced():
    avail = numbers.sources([1794919.64, 3, 5])
    assert numbers.unsourced("about $1.79M, 3x the 5th store", avail) == ["$1.79M", "3x", "5th"]
