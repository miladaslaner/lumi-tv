from collections.abc import Iterable, Mapping

from pydantic import BaseModel, Field, model_validator

# [kids] ages default: Kijkwijzer labels -> minimum age. A household with another scheme sets its own table.
DEFAULT_AGES: dict[str, int] = {"AL": 0, "6": 6, "9": 9, "12": 12, "14": 14, "16": 16, "18": 18}
DEFAULT_MAX_AGE = 15  # a kids_age above this counts as this: 16+ and 18+ titles never appear in kids mode


def check_labels(
    order: Iterable[str], toddler_allowed: Iterable[str], no_age_max: str, ages: Mapping[str, int]
) -> None:
    """Every label the policy uses must have an age; raises ValueError naming the first one that lacks it."""
    for where, labels in (("order", order), ("toddler_allowed", toddler_allowed), ("no_age_max", [no_age_max])):
        for label in labels:
            if label not in ages:
                raise ValueError(f"{where} entry {label!r} has no age in [kids] ages {sorted(ages)}")


class KidsPolicy(BaseModel):
    order: list[str]
    toddler_below_age: int
    toddler_allowed: list[str]
    no_age_max: str
    ages: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_AGES))  # label -> minimum age
    max_age: int = DEFAULT_MAX_AGE

    @model_validator(mode="after")
    def _labels_have_ages(self):
        check_labels(self.order, self.toddler_allowed, self.no_age_max, self.ages)
        return self


def effective_age(kids_age: int | None, policy: KidsPolicy) -> int | None:
    """kids_age as the policy applies it: anything above max_age counts as max_age, so the highest kids cutoff is
    the label with the largest age at most max_age ("14" by default), whatever age was heard ("9" taken as 99)."""
    return None if kids_age is None else min(kids_age, policy.max_age)


def allowed(cert: str | None, kids_age: int | None, kids_mode: bool, policy: KidsPolicy) -> bool:
    """Fixed guarantees: in kids mode the filter always applies and unrated or
    unknown certifications are always excluded. The policy only sets the limits."""
    if not kids_mode:
        return True
    if not cert or cert not in policy.order:
        return False
    kids_age = effective_age(kids_age, policy)
    if kids_age is None:
        return policy.order.index(cert) <= policy.order.index(policy.no_age_max)
    if kids_age < policy.toddler_below_age:
        return cert in policy.toddler_allowed
    return policy.ages[cert] <= kids_age


def cert_cutoff(kids_age: int | None, policy: KidsPolicy) -> str:
    """Highest certification `allowed` accepts for this age; used to narrow the catalogue search."""
    kids_age = effective_age(kids_age, policy)
    if kids_age is None:
        return policy.no_age_max
    if kids_age < policy.toddler_below_age:
        return max(policy.toddler_allowed, key=policy.order.index)
    return max((c for c in policy.order if policy.ages[c] <= kids_age), key=policy.order.index, default=policy.order[0])
