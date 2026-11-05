import pytest

from lumitv.kids import DEFAULT_AGES, KidsPolicy, allowed, cert_cutoff, effective_age

P = KidsPolicy(
    order=["AL", "6", "9", "12", "14", "16", "18"], toddler_below_age=6, toddler_allowed=["AL"], no_age_max="6"
)
# a US-style scheme: labels are free, the ages table says what they mean
US = KidsPolicy(order=["G", "PG", "PG-13", "R"], toddler_below_age=6, toddler_allowed=["G"], no_age_max="PG",
                ages={"G": 0, "PG": 6, "PG-13": 12, "R": 16})  # fmt: skip


def test_outside_kids_mode_everything_allowed():
    assert allowed("18", None, kids_mode=False, policy=P) and allowed(None, None, kids_mode=False, policy=P)


def test_toddler_only_AL():
    assert allowed("AL", 3, True, policy=P) and not allowed("6", 3, True, policy=P)


def test_toddler_boundary_is_below_not_at_the_age():
    assert not allowed("6", P.toddler_below_age - 1, True, policy=P)  # 5: still a toddler, AL only
    assert allowed("6", P.toddler_below_age, True, policy=P)  # 6: the age rule applies
    assert cert_cutoff(P.toddler_below_age - 1, P) == "AL" and cert_cutoff(P.toddler_below_age, P) == "6"


def test_age_cutoff():
    assert allowed("9", 10, True, policy=P) and not allowed("12", 10, True, policy=P)


def test_kids_mode_without_age_is_AL_and_6():
    assert allowed("6", None, True, policy=P) and not allowed("9", None, True, policy=P)


def test_unrated_excluded_in_kids_mode():
    assert not allowed(None, 8, True, policy=P) and not allowed("", None, True, policy=P)


def test_unknown_cert_excluded_in_kids_mode():
    assert not allowed("PG", 8, True, policy=P)


@pytest.mark.parametrize("age", [None, 0, 3, 6, 10, 14, 15, 16, 17, 18, 30, 99])
def test_sixteen_and_eighteen_never_pass_in_kids_mode(age):
    assert not allowed("16", age, True, policy=P) and not allowed("18", age, True, policy=P)


@pytest.mark.parametrize("age", [16, 17, 18, 99])
def test_ages_above_max_age_count_as_max_age(age):
    assert effective_age(age, P) == 15 and cert_cutoff(age, P) == "14"
    assert allowed("14", age, True, policy=P)
    assert effective_age(15, P) == 15 and effective_age(None, P) is None


def test_non_numeric_labels_work_through_the_ages_table():
    assert allowed("PG", 8, True, policy=US) and not allowed("PG-13", 8, True, policy=US)
    assert allowed("PG-13", 12, True, policy=US) and not allowed("R", 99, True, policy=US)
    assert allowed("G", 3, True, policy=US) and not allowed("PG", 3, True, policy=US)  # toddler
    assert allowed("PG", None, True, policy=US) and not allowed("PG-13", None, True, policy=US)
    assert cert_cutoff(8, US) == "PG" and cert_cutoff(99, US) == "PG-13" and cert_cutoff(None, US) == "PG"


def test_policy_rejects_a_label_without_an_age():
    with pytest.raises(ValueError, match="'PG-13'"):
        KidsPolicy(order=["G", "PG", "PG-13"], toddler_below_age=6, toddler_allowed=["G"], no_age_max="PG",
                   ages={"G": 0, "PG": 6})  # fmt: skip
    with pytest.raises(ValueError, match="toddler_allowed entry 'TV-Y'"):
        KidsPolicy(order=["AL", "6"], toddler_below_age=6, toddler_allowed=["TV-Y"], no_age_max="6")


def test_default_ages_cover_the_default_order():
    assert set(P.order) <= set(DEFAULT_AGES) and P.ages == DEFAULT_AGES and P.max_age == 15
