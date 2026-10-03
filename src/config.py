"""Named configuration flags for decisions that are not obviously settled.

Anything in here is a choice the pipeline makes that a reasonable engineer
could make differently, and that Phase 11 will sweep against the solved
samples. Keeping them named and central means a sweep edits one file and
every stage picks the change up, instead of hunting inline constants.

Every flag is read at call time, not import time, so a test can monkeypatch
a value and the change takes effect without reloading modules. Values may
also be overridden from the environment, which keeps the sweep runnable
from a shell without editing source.
"""

from __future__ import annotations

import os
from typing import Final, Literal

# ---------------------------------------------------------------------------
# Sweepable: foreign-currency conversion
# ---------------------------------------------------------------------------

FxDateKey = Literal["cash_date", "event_date"]

#: **MEASURED INERT (P11-S5).** All four combinations of this flag and
#: ``QUANTIZE_AT`` produce identical MARE, identical per-column exactness and
#: an identical 250-row distribution: every foreign-currency event in this
#: dataset settles on a date the rate table covers. Retained because a dataset
#: whose events settle away from a rate date would separate them.
#:
#: Which date keys the exchange-rate lookup.
#:
#: ``cash_date``  -- ``settlement_date`` falling back to ``event_date``. This
#:                   is what the dataset contract states ("use the row for its
#:                   settlement date") and is the default.
#: ``event_date`` -- the date the transaction was initiated.
#:
#: Phase 1 audit (``audit/audit_report.txt``): 178 events settle later than
#: they occur, so the two keys genuinely differ for any of those that are also
#: foreign-currency.
FX_DATE_KEY: Final[FxDateKey] = "cash_date"

QuantizePoint = Literal["conversion", "render"]

#: **MEASURED INERT (P11-S5)** -- see ``FX_DATE_KEY``. Sub-cent differences
#: never survive the two-decimal output format.
#:
#: Where money gets rounded to two decimal places.
#:
#: ``conversion`` -- round each converted amount as it is produced, so every
#:                   stored amount is already at cent precision. Matches how a
#:                   bank ledger behaves and is the default.
#: ``render``     -- keep the full-precision product and round only when
#:                   writing output. Avoids accumulating per-row rounding
#:                   error across a long series, at the cost of carrying
#:                   amounts that never existed in any statement.
#:
#: The ground truth engineers margins of 1.9-3.45 currency units, so a
#: systematic sub-cent drift across ~100 forecast rows is inside the noise
#: floor but a systematic half-cent bias in the same direction may not be.
QUANTIZE_AT: Final[QuantizePoint] = "conversion"


# ---------------------------------------------------------------------------
# Recurrence detection
# ---------------------------------------------------------------------------

#: Cadences a recurring series is allowed to snap to, in days. Monthly is
#: handled separately via day-of-month anchoring because calendar months are
#: 28-31 days long.
ALLOWED_CADENCE_DAYS: Final[tuple[int, ...]] = (5, 7, 10, 14, 21)

#: Day-gap window treated as "monthly".
MONTHLY_GAP_MIN: Final[int] = 28
MONTHLY_GAP_MAX: Final[int] = 31

#: Nominal length of a monthly cadence, used when a series is monthly but its
#: day-of-month anchor cannot be established.
MONTHLY_NOMINAL_DAYS: Final[int] = 30

#: **1 day.** Tolerance when matching an observed gap to a candidate
#: cadence. Swept 0/1/2/3 at P11-S6 against the 25 labels:
#:
#: ===== ====== ============ ========= ===========
#: value MARE   ratio        safe/stat recurring
#: ===== ====== ============ ========= ===========
#: 0     1.225  0.99         2 / 18    2806
#: **1** 1.248  **1.00**     2 / 18    2866
#: 2     1.842  0.95         2 / 17    2885
#: 3     1.974  1.10         1 / 13    3138
#: ===== ====== ============ ========= ===========
#:
#: 0 and 1 are indistinguishable: identical on **every** exact count, and
#: the 1.9% MARE edge for 0 is **one row** -- ``request_09`` 0.92 -> 0.13,
#: partly offset by ``request_03`` 0.14 -> 0.34. Two rows move, one each
#: way. That is not evidence.
#:
#: Kept at 1 on the domain argument, since the data does not separate them:
#: a real monthly debit lands a day early or late when a payday or a
#: month boundary moves it, and tolerance 0 would require exact day-gaps
#: forever. Beyond 1 it degrades sharply -- at 2, ``request_10`` alone goes
#: from 1.00 to a 17.45x overstatement.
CADENCE_TOLERANCE_DAYS: Final[int] = 1

#: **3.** A gap may be a small multiple of the true cadence when an
#: occurrence is missing from history; multiples beyond this are not
#: treated as skips. Swept 1/2/3/4/6 at P11-S6:
#:
#: ===== ====== ========== ========= ===== ==========
#: value MARE   ratio      safe/stat o/u   recurring
#: ===== ====== ========== ========= ===== ==========
#: 1     1.466  0.87       3 / 19    18/4  2563
#: 2     1.213  0.94       3 / 19    17/5  2704
#: **3** 1.248  **1.00**   2 / 18    16/7  2866
#: 4     1.260  1.01       1 / 16    15/9  3003
#: 6     1.301  1.08       1 / 13    12/12 3239
#: ===== ====== ========== ========= ===== ==========
#:
#: **This is the one sweep where a rival setting genuinely wins on the
#: label metrics, and it is still rejected.** 2 beats 3 on MARE (-2.8%) and
#: on every exact column. But the entire gain is **one row**: ``request_09``
#: 0.923 -> 0.000, contributing -0.0369 of the -0.0353 total MARE move (the
#: only other row, ``request_24``, moves +0.0017 the wrong way).
#:
#: The mechanism is real, not luck. ``user_09``'s 'Bulk pantry shop' has
#: gaps [90, 30] and 'Local taxi' [63, 21]; 90 is 3x monthly and 63 is 3x21,
#: so at 3 both snap to consistency 0.75 and project, while at 2 neither
#: matches and both drop. ``request_09`` is one of the 7 **under**-stating
#: rows, so removing that outflow moves it to exact.
#:
#: That is precisely why 2 is rejected. The same mechanism removes outflow
#: from *everyone*: the run-rate ratio falls 1.00 -> 0.94 across all 25
#: users, and the over/under split moves 16/7 -> 17/5. It buys one fixed
#: under-statement by pushing the whole dataset further into
#: **over**-statement -- which is both the unsafe direction and the
#: documented residual defect (`affordable_now` +14 pts). Trading a
#: systematic 6% outflow loss for one row is the exact move P11-S2
#: established should not be made.
MAX_SKIPPED_OCCURRENCES: Final[int] = 3

#: Occurrences required before a series is called recurring. "Detect
#: recurrence only when history supports it" -- two points define one gap,
#: which is not yet evidence of a pattern.
RECURRENCE_MIN_OCCURRENCES: Final[int] = 3

#: Exception to the above for outflows only. Under-detecting a recurring
#: expense makes the forecast optimistic and the recommendation unsafe;
#: under-detecting income makes it conservative and safe. The asymmetry is
#: deliberate, and matches "forecast essential variable spending
#: conservatively" plus "do not invent unsupported future income".
RECURRENCE_ALLOW_TWO_OCCURRENCE_DEBITS: Final[bool] = True

#: **0.6.** Fraction of consecutive gaps that must match the inferred
#: cadence before the series is accepted as regular. This is the most
#: load-bearing constant in the file -- it gates recurrence detection,
#: which drives the entire forecast. Swept 0.0 through 1.0 at P11-S6:
#:
#: ======= ====== ========== ========= ========== ==========
#: value   MARE   ratio      safe/stat recurring  affo_now
#: ======= ====== ========== ========= ========== ==========
#: 0.0     2.277  1.70       1 / 9     5273       40
#: 0.3     2.290  1.61       1 / 9     4967       44
#: 0.4     1.487  1.43       1 / 9     4529       47
#: 0.5     1.461  1.40       1 / 9     4422       47
#: **0.6** 1.248  **1.00**   2 / 18    2866       65
#: 0.7     1.211  0.84       2 / 18    2616       66
#: 0.8     1.498  0.79       3 / 18    2404       62
#: 1.0     1.526  0.79       3 / 18    2372       64
#: ======= ====== ========== ========= ========== ==========
#:
#: 0.6 sits on a **cliff, not a plateau**. Crossing from 0.5 to 0.6 moves
#: status exactness 9 -> 18 and method 10 -> 20 in a single step, because
#: below 0.6 irregular series are admitted wholesale (4,422 recurring
#: series against 2,866) and the forecast over-projects outflow by 40%.
#:
#: 0.7 is the only rival: MARE 1.211 vs 1.248, a 3.0% edge. Rejected
#: because the run-rate ratio collapses 1.00 -> 0.84 -- a 16% systematic
#: loss of projected outflow, in the unsafe direction -- and the over/under
#: split moves 16/7 -> 17/6. For scale, the P11-S2 sweep that *did* move a
#: default showed a 43% MARE spread; 3.0% is inside this harness's noise,
#: spread across 6 rows moving in both directions (``request_01`` -0.63 and
#: ``request_14`` -0.62 against ``request_24`` +0.18 and ``request_23``
#: +0.17). One metric moving 3% cannot outvote the other moving 16%.
#:
#: Note what the low end shows: tightening to 0.0-0.3 does pull
#: `affordable_now` from 65 toward the labelled 30, which looks like a fix
#: for the residual defect. It is not -- `not_affordable` explodes to 165-174
#: against a target of 70, and MARE nearly doubles. The distribution is a
#: weak proxy and this is the clearest demonstration of it in the project.
CADENCE_CONSISTENCY_THRESHOLD: Final[float] = 0.6

#: **0.5.** Weight given to a gap that matches only a *multiple* of the
#: candidate cadence (a skipped occurrence) relative to one that lands on
#: it directly. Extracted from an inline literal in ``state._snap_cadence``
#: at P11-S6 so it could be swept and defended like every other constant
#: here; the value is unchanged. Swept 0.0/0.25/0.5/0.75/1.0:
#:
#: ======= ====== ========== ========= ===== ==========
#: value   MARE   ratio      safe/stat o/u   recurring
#: ======= ====== ========== ========= ===== ==========
#: 0.0     1.466  0.87       3 / 19    18/4  2563
#: 0.25    1.245  0.95       2 / 18    17/6  2742
#: **0.5** 1.248  **1.00**   2 / 18    16/7  2866
#: 0.75    1.251  0.94*      2 / 16    15/8  3411
#: 1.0     1.286  1.18       1 / 13    13/11 3743
#: ======= ====== ========== ========= ===== ==========
#:
#: (*0.75 reads 1.13 in the raw sweep; the table above is the median.)
#:
#: MARE is **flat** across 0.25/0.5/0.75 -- 1.245 / 1.248 / 1.251, a 0.5%
#: spread, which is no signal at all. The run-rate ratio is the only metric
#: that separates them, and it moves monotonically 0.87 -> 1.18, crossing
#: 1.00 at exactly 0.5. On a flat continuous metric the tie-break is the
#: other continuous metric, not exact counts.
#:
#: Exact counts peak at 0.0 (safe 3/25, status 19/25, method 21/25) --
#: the P11-S2 pattern repeating, where the discrete metric peaks where the
#: continuous ones are worst. At 0.0, MARE is the worst in the sweep (1.466)
#: and essentially all of that degradation is one row: ``request_10`` goes
#: 1.00 -> 6.86, a 6.9x overstatement of the same row P11-S1 diagnosed.
#: 0.0 also means a skipped occurrence counts for nothing, which is the
#: original synthetic failure this weight was introduced to fix: gaps of
#: 16/67/6 days read as a 5-day cadence at 67% consistency.
CADENCE_SKIP_WEIGHT: Final[float] = 0.5


# ---------------------------------------------------------------------------
# Forecast horizon
# ---------------------------------------------------------------------------

#: The safety check window, in days from ``request_date``.
FORECAST_HORIZON_DAYS: Final[int] = 90


# ---------------------------------------------------------------------------
# Sweepable: stale-series projection
# ---------------------------------------------------------------------------

#: **12.0 cadence periods.** Chosen in P11-S2 by sweeping None/12/8/6/4/3/1.5
#: against the 25 labels: MARE degrades monotonically as the gate tightens
#: (1.259 -> 1.825 **as swept at P11-S2**; the same sweep after the P11-S4
#: income fix gives 1.248 -> 1.786, a 43% degradation rather than 45% -- the
#: shape is unchanged), and the outflow run-rate ratio falls 1.02 -> 0.70. At
#: the previous 4.0, ``request_10`` lost four live grocery/transport series
#: sitting at 4.4-5.1 periods and overstated capacity **13.5x**. Twelve periods means a
#: monthly series must be silent a full year before it is dropped.
#:
#: How many cadence periods a series may go unseen before the forecast stops
#: projecting it, or ``None`` to project every detected series regardless of
#: age.
#:
#: The asymmetry matters: dropping a live series *overstates* capacity, which
#: is the unsafe direction, while projecting a dead one understates it. The
#: default is therefore permissive -- only series that are unambiguously dead
#: are dropped.
#:
#: Measured over all 275 requests (2,866 detected series), the share unseen for
#: more than N periods is:
#:
#:     N > 1.5 -> 762 series, 392 of them with 3+ occurrences
#:     N > 3.0 -> 548 series, 246 of them with 3+ occurrences
#:     N > 6.0 -> 266 series
#:
#: So gating on occurrence count alone does **not** isolate dead series; the
#: two axes are largely independent above 3 occurrences. See C3 in
#: ``evaluation/calibration_log.md``.
#:
#: **Phase 11 revision: 4.0 -> 12.0.** Swept against the 25 labels on both
#: ``amount_safe_to_pay`` error and the outflow run-rate ratio. At 4.0 the
#: forecast projected only 0.78 of each user's own observed run rate and the
#: gate was cutting live short-cadence spending -- ``request_10`` lost four
#: grocery/dining/transport series sitting at 4.4-5.1 periods and overstated
#: capacity by 13x. At 12.0 the median ratio is 1.01 and that overstatement is
#: gone. Twelve periods means a monthly series must be silent a full year
#: before it is dropped, which is the "affirmative evidence of death" bar the
#: original reasoning asked for -- 4.0 was simply set far too tight. See
#: P11-S2.
SERIES_STALENESS_PERIODS: Final[float | None] = 12.0

#: Minimum occurrences before a series is projected forward. ``state.py``
#: already decides *whether a series is recurring at all*; this is the stricter
#: bar for *extrapolating* it into the future.
#:
#: Measured in Phase 4 (C3b). Two-occurrence series are strongly associated
#: with staleness -- only 84 of 454 are fresh, and 302 are more than 3 periods old -- so requiring 3 removes
#: mostly-dead series without touching the monthly backbone (rent, utilities,
#: subscriptions, salary), 84% of which are fresh.
MIN_SERIES_OCCURRENCES: Final[int] = 2

#: Day of month a monthly salary lands on when the history gives no anchor.
#: Measured in P11-S4: the anchor is derived per user in 214 of 275 cases,
#: and 205 of those genuinely fall on the 15th, so the default agrees with
#: the data rather than imposing on it.
DEFAULT_SALARY_DAY_OF_MONTH: Final[int] = 15


# ---------------------------------------------------------------------------
# Sweepable: installment eligibility
# ---------------------------------------------------------------------------

#: Reject an installment option whose payment count exceeds the user's
#: ``max_installment_months``.
#:
#: **[HYP]** -- this is the one eligibility rule inferred rather than stated.
#: The contract says an option "may still be rejected because it conflicts
#: with ... max_installment_months" without defining the comparison, and the
#: units do not match: the cap is in months while options are in payment
#: counts at 28/30/31-day intervals.
#:
#: Measured in Phase 6 (C6a), the gate is a **no-op on this dataset**:
#: across all 275 requests
#: there is not one installment option that exceeds the cap and does *not*
#: already miss its deadline (0 of 469 in the evaluation set, 0 of 40 in the
#: samples). Turning it off changes no outcome, which is why it is safe to
#: leave on. See C6 in ``evaluation/calibration_log.md``.
ENFORCE_INSTALLMENT_MONTH_CAP: Final[bool] = True


# ---------------------------------------------------------------------------
# Sweepable: spending-change eligibility
# ---------------------------------------------------------------------------

#: Restrict spending changes to series detected as recurring.
#:
#: The rulebook states "only recurring expenses marked as flexible may be
#: changed", so the default is on. It is also functionally necessary: a
#: non-recurring series has no future occurrences, so changing it frees
#: nothing inside the horizon.
#:
#: **This conflicts with the data.** `request_11`'s ground truth changes
#: `event_989` ("Weekend food delivery"), a series with two occurrences 126
#: days apart. Turning this off makes that event eligible but still does not
#: reproduce the row, because the change frees nothing. See C7.
CHANGES_REQUIRE_RECURRING: Final[bool] = True


# ---------------------------------------------------------------------------
# Environment overrides
# ---------------------------------------------------------------------------

_ENV_PREFIX = "BUYORWAIT_"


def _env(name: str, default: object) -> object:
    raw = os.environ.get(f"{_ENV_PREFIX}{name}")
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw.strip()


def fx_date_key() -> FxDateKey:
    """Read the FX date key, honouring ``BUYORWAIT_FX_DATE_KEY``."""
    value = _env("FX_DATE_KEY", FX_DATE_KEY)
    if value not in ("cash_date", "event_date"):
        raise ValueError(f"invalid FX_DATE_KEY: {value!r}")
    return value  # type: ignore[return-value]


def quantize_at() -> QuantizePoint:
    """Read the quantization point, honouring ``BUYORWAIT_QUANTIZE_AT``."""
    value = _env("QUANTIZE_AT", QUANTIZE_AT)
    if value not in ("conversion", "render"):
        raise ValueError(f"invalid QUANTIZE_AT: {value!r}")
    return value  # type: ignore[return-value]


def forecast_horizon_days() -> int:
    return int(_env("FORECAST_HORIZON_DAYS", FORECAST_HORIZON_DAYS))


def recurrence_min_occurrences() -> int:
    return int(_env("RECURRENCE_MIN_OCCURRENCES", RECURRENCE_MIN_OCCURRENCES))


def allow_two_occurrence_debits() -> bool:
    return bool(
        _env(
            "RECURRENCE_ALLOW_TWO_OCCURRENCE_DEBITS",
            RECURRENCE_ALLOW_TWO_OCCURRENCE_DEBITS,
        )
    )


def cadence_consistency_threshold() -> float:
    return float(_env("CADENCE_CONSISTENCY_THRESHOLD", CADENCE_CONSISTENCY_THRESHOLD))


def cadence_skip_weight() -> float:
    return float(_env("CADENCE_SKIP_WEIGHT", CADENCE_SKIP_WEIGHT))


def cadence_tolerance_days() -> int:
    return int(_env("CADENCE_TOLERANCE_DAYS", CADENCE_TOLERANCE_DAYS))


def max_skipped_occurrences() -> int:
    return int(_env("MAX_SKIPPED_OCCURRENCES", MAX_SKIPPED_OCCURRENCES))


def series_staleness_periods() -> float | None:
    """``None`` disables the staleness gate entirely."""
    raw = os.environ.get(f"{_ENV_PREFIX}SERIES_STALENESS_PERIODS")
    if raw is None:
        return SERIES_STALENESS_PERIODS
    stripped = raw.strip().lower()
    if stripped in {"", "none", "null", "off"}:
        return None
    return float(stripped)


def min_series_occurrences() -> int:
    return int(_env("MIN_SERIES_OCCURRENCES", MIN_SERIES_OCCURRENCES))


def default_salary_day_of_month() -> int:
    return int(_env("DEFAULT_SALARY_DAY_OF_MONTH", DEFAULT_SALARY_DAY_OF_MONTH))


def enforce_installment_month_cap() -> bool:
    return bool(
        _env("ENFORCE_INSTALLMENT_MONTH_CAP", ENFORCE_INSTALLMENT_MONTH_CAP)
    )


def changes_require_recurring() -> bool:
    return bool(_env("CHANGES_REQUIRE_RECURRING", CHANGES_REQUIRE_RECURRING))


def describe() -> dict[str, object]:
    """Current effective configuration, for the run log and usage report."""
    return {
        "fx_date_key": fx_date_key(),
        "quantize_at": quantize_at(),
        "forecast_horizon_days": forecast_horizon_days(),
        "recurrence_min_occurrences": recurrence_min_occurrences(),
        "allow_two_occurrence_debits": allow_two_occurrence_debits(),
        "cadence_consistency_threshold": cadence_consistency_threshold(),
        "cadence_skip_weight": cadence_skip_weight(),
        "cadence_tolerance_days": cadence_tolerance_days(),
        "max_skipped_occurrences": max_skipped_occurrences(),
        "allowed_cadence_days": ALLOWED_CADENCE_DAYS,
        "series_staleness_periods": series_staleness_periods(),
        "min_series_occurrences": min_series_occurrences(),
        "default_salary_day_of_month": default_salary_day_of_month(),
        "enforce_installment_month_cap": enforce_installment_month_cap(),
        "models": describe_models(),
    }


# ---------------------------------------------------------------------------
# Sweepable: income-stream aggregation (Phase 11, Step 4)
# ---------------------------------------------------------------------------

#: Aggregate every salary credit into one income stream when no
#: description-keyed salary series was detected.
#:
#: ``state.py`` keys series by description, which is right for expenses. But a
#: single income stream is often recorded under several descriptions, and the
#: keying then shatters it into irregular fragments so no income projects at
#: all. Measured in P11-S4: 57 of 275 users projected zero salary before this
#: fallback existed, 40 after.
AGGREGATE_INCOME_STREAM: Final[bool] = True

#: **Monthly only.** Swept in P11-S4 against the alternative of aggregating
#: every cadence: that variant wins one row on five exact-match columns but
#: swings ``request_10`` from ``safe=0`` to the full requested 266,700 against
#: a label of 12,700 -- a **21x** overstatement that is the entire MARE gap
#: (1.249 vs 1.972). Rejected on principle and on error magnitude, not score.
#:
#: Restrict that aggregation to *monthly* streams.
#:
#: A monthly payroll recorded as "Payroll before leave" / "Payroll after
#: returning from leave" is plainly one salary. Sub-monthly platform earnings
#: under rotating payer names ("Delivery platform payout", "Weekly app
#: earnings", ...) are not: extrapolating them forward invents unsupported
#: future income, which the rulebook forbids, and under-forecasting income is
#: the safe error.
AGGREGATE_MONTHLY_INCOME_ONLY: Final[bool] = True


def aggregate_income_stream() -> bool:
    return bool(_env("AGGREGATE_INCOME_STREAM", AGGREGATE_INCOME_STREAM))


def aggregate_monthly_income_only() -> bool:
    return bool(
        _env("AGGREGATE_MONTHLY_INCOME_ONLY", AGGREGATE_MONTHLY_INCOME_ONLY)
    )


# ---------------------------------------------------------------------------
# Sweepable: orphaned variable spending (Phase 11, Step 3)
# ---------------------------------------------------------------------------

#: Project variable spending that description-keying left in non-recurring
#: fragments.
#:
#: Measured in P11-S3-SECOND: all 275 users fragment grocery/dining/transport
#: spend across description variants -- one user's transport appears as nine
#: separate series, none individually recurring.
#:
#: **Default OFF: hypothesis measured and rejected (P11-S3).** The expense-side
#: mirror of the P11-S4 income defect does *not* hold, because outflow was
#: already complete: after Steps 2 and 4 the median run-rate ratio is 1.01, so
#: the projected recurring series already reproduce each user's observed
#: spending. Adding the fragments on top double-counts -- naive
#: (category, cadence) projection takes the median ratio to 1.41, and the
#: rate-based version here to 1.37, collapsing status accuracy from 18/25 to
#: 9/25. Kept behind the flag because the fragmentation is real and a future
#: dataset with genuinely missing outflow would need it.
AGGREGATE_ORPHAN_EXPENSES: Final[bool] = False

#: Trailing window, in days before ``request_date``, used to measure the rate.
ORPHAN_EXPENSE_WINDOW_DAYS: Final[int] = 90

#: Events required in a category before its rate is projected.
ORPHAN_EXPENSE_MIN_EVENTS: Final[int] = 3


def aggregate_orphan_expenses() -> bool:
    return bool(_env("AGGREGATE_ORPHAN_EXPENSES", AGGREGATE_ORPHAN_EXPENSES))


def orphan_expense_window_days() -> int:
    return int(_env("ORPHAN_EXPENSE_WINDOW_DAYS", ORPHAN_EXPENSE_WINDOW_DAYS))


def orphan_expense_min_events() -> int:
    return int(_env("ORPHAN_EXPENSE_MIN_EVENTS", ORPHAN_EXPENSE_MIN_EVENTS))


# ---------------------------------------------------------------------------
# Sweepable: series amount estimator (Phase 11, Step 3)
# ---------------------------------------------------------------------------

SeriesEstimator = Literal["median", "mean", "max"]

#: **median.** Swept in P11-S3: median and mean are indistinguishable
#: (MARE 1.249 vs 1.248); ``max`` improves MARE by 2% but costs a row on three
#: exact-match columns. For scale, the staleness sweep moved MARE by 45%, so
#: this lever is negligible and the default stands.
#:
#: Which statistic represents a recurring series' future amount.
#:
#: ``median`` resists a one-off spike; ``mean`` tracks drift; ``max`` is the
#: conservative reading, since over-forecasting an expense is the safe error.
SERIES_AMOUNT_ESTIMATOR: Final[SeriesEstimator] = "median"


def series_amount_estimator() -> SeriesEstimator:
    value = _env("SERIES_AMOUNT_ESTIMATOR", SERIES_AMOUNT_ESTIMATOR)
    if value not in ("median", "mean", "max"):
        raise ValueError(f"invalid SERIES_AMOUNT_ESTIMATOR: {value!r}")
    return value  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Model generation settings
# ---------------------------------------------------------------------------
#
# Every generation parameter lives here, never inline in ``src/extract.py``.
# The per-request cost of this pipeline is zero -- template coverage is 100%
# (Phase 3, ``audit/extraction_coverage.md``) so the text fallback never fires, and explanations are deterministic string
# fills -- but the settings are still pinned so the two optional model paths
# are exactly reproducible, and so they appear in ``config.describe()`` and
# therefore in ``evaluation/usage_report.md``.

#: Provider for both optional model paths.
MODEL_PROVIDER: Final[str] = "anthropic"

#: Model for the text fallback in ``extract.llm_fallback``. Never reached on
#: the supplied dataset.
TEXT_MODEL: Final[str] = "claude-opus-5"

#: Model for the one-time image prepass in ``extract.run_image_prepass``.
VISION_MODEL: Final[str] = "claude-opus-5"

#: **0.0.** The extractor must return the same JSON for the same message.
#: A sampled extraction would make the whole pipeline non-reproducible, and
#: the output is scored against exact numeric ground truth.
MODEL_TEMPERATURE: Final[float] = 0.0

#: Output caps. Both paths return one small JSON object.
TEXT_MAX_TOKENS: Final[int] = 512
VISION_MAX_TOKENS: Final[int] = 512

#: **Unset, deliberately.** At temperature 0 it is redundant, and sending both
#: sampling controls together is a documented source of surprising behaviour.
#: Recorded as a decision rather than omitted, so its absence is legible.
MODEL_TOP_P: Final[float | None] = None

#: **Unset -- not available.** The Anthropic Messages API exposes no seed
#: parameter, so determinism rests on temperature 0 plus the committed image
#: cache. Stated explicitly so a reader does not read this as an oversight.
MODEL_SEED: Final[int | None] = None


def model_provider() -> str:
    return str(_env("MODEL_PROVIDER", MODEL_PROVIDER))


def text_model() -> str:
    return str(_env("TEXT_MODEL", TEXT_MODEL))


def vision_model() -> str:
    return str(_env("VISION_MODEL", VISION_MODEL))


def model_temperature() -> float:
    return float(_env("MODEL_TEMPERATURE", MODEL_TEMPERATURE))


def text_max_tokens() -> int:
    return int(_env("TEXT_MAX_TOKENS", TEXT_MAX_TOKENS))


def vision_max_tokens() -> int:
    return int(_env("VISION_MAX_TOKENS", VISION_MAX_TOKENS))


def model_top_p() -> float | None:
    raw = os.environ.get(f"{_ENV_PREFIX}MODEL_TOP_P")
    if raw is None:
        return MODEL_TOP_P
    stripped = raw.strip().lower()
    return None if stripped in {"", "none", "null"} else float(stripped)


def model_seed() -> int | None:
    raw = os.environ.get(f"{_ENV_PREFIX}MODEL_SEED")
    if raw is None:
        return MODEL_SEED
    stripped = raw.strip().lower()
    return None if stripped in {"", "none", "null"} else int(stripped)


def describe_models() -> dict[str, object]:
    """Generation settings, for the usage report."""
    return {
        "provider": model_provider(),
        "text_model": text_model(),
        "vision_model": vision_model(),
        "temperature": model_temperature(),
        "text_max_tokens": text_max_tokens(),
        "vision_max_tokens": vision_max_tokens(),
        "top_p": model_top_p(),
        "seed": model_seed(),
    }
