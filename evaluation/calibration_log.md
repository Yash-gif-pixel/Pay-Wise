# Calibration log

> **Reading this file.** Entries are dated records of the state at the time
> they were written, kept as history rather than edited. Sections whose figures
> were superseded by a later calibration step carry an **as-of marker** naming
> that step. **Current values live in one place only: the Final scorecard at the
> foot of this file.** No figure in the body should be read as current unless
> its section says so.


---

# PHASE 11 — CALIBRATION

Harness: `evaluation/calibrate.py`. Baseline before any change:

| column | exact |
| --- | ---: |
| `amount_safe_to_pay` | **2 / 25** |
| `affordability_status` | 18 / 25 |
| `recommended_payment_method` | 20 / 25 |
| `payment_plan` | 18 / 25 |
| `earliest_date_for_full_payment` | 12 / 25 |
| `spending_changes_needed` | 22 / 25 |
| `decision_explanation` | 11 / 25 |
| **all columns** | **2 / 25** |

Mean absolute relative error on `amount_safe_to_pay`: **1.7280**.
Overstating 19/25, understating 4/25.

## P11-S1. Run-rate ratio — **branch: outflow (go to Step 2)**

Projected 90-day outflow against observed settled outflow in the 90 days
*before* `request_date`, per sample user:

| statistic | outflow ratio | inflow ratio |
| --- | ---: | ---: |
| min | **0.50** | 0.00 |
| median | **0.78** | 1.00 |
| mean | **0.80** | 0.94 |
| max | 1.07 | 1.74 |

**15 of 25 users project below 0.80 of their own observed run rate.** The
forecast is losing roughly a fifth of outflow. Some shortfall is expected — the
observed window contains one-off spending that should not recur — but 0.50–0.78
is far beyond what one-offs explain, and the exclusion log confirms whole
recurring series are being dropped.

Decisive evidence from `request_10`, the worst overstatement (13×):

```
opening balance       : 750,155      minimum : 225,400
observed 90d outflow  : 543,156  (48 debits)
projected 90d outflow : 353,563  (24 debits)   <- half the events
my amount_safe_to_pay : 171,192      ground truth : 12,700

excluded series (all four, all staleness, all short-cadence):
  [stale] 'Bulk pantry shop'      n=3 cad=14  periods=5.1
  [stale] 'Fuel refill'           n=2 cad=7   periods=5.0
  [stale] 'Household groceries'   n=2 cad=21  periods=5.0
  [stale] 'Lunch with colleagues' n=3 cad=14  periods=4.4
```

Every excluded series sits at **4.4–5.1 periods**, just past the
`SERIES_STALENESS_PERIODS = 4.0` cutoff, and every one is short-cadence
grocery/dining/transport spending — precisely the category that dominates a
90-day outflow total. Raising the cutoff to 6 restores all four.

**Branch taken: Step 2 (staleness re-sweep).** The Phase 4 sweep called this
"within noise" using a weak proxy; with `amount_safe_to_pay` against 25 labels
the signal is direct.

## P11-S2. Staleness re-sweep — **4.0 → 12.0**, on metric agreement

Every value tried, including rejected ones. `chg` is rows of 250 producing a
spending-change candidate.

| stale | safe exact | **MARE** | earliest | method | status | m\|gen | **med ratio** | min ratio | excl | of which n≥3 | chg |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **None** | 1/25 | **1.259** | 12/25 | 19/25 | 17/25 | 19/19 | **1.02** | 0.50 | 0 | 0 | 4 |
| **12** ← chosen | 1/25 | **1.260** | 12/25 | 19/25 | 17/25 | 19/19 | **1.01** | 0.50 | 3 | 1 | 3 |
| 8 | 1/25 | 1.276 | 12/25 | 19/25 | 17/25 | 19/19 | 0.85 | 0.50 | 18 | 7 | 1 |
| 6 | 2/25 | 1.297 | 12/25 | 20/25 | 18/25 | 20/20 | 0.80 | 0.50 | 29 | 11 | 1 |
| 4 *(old)* | 2/25 | 1.728 | 12/25 | 20/25 | 18/25 | 20/20 | 0.78 | 0.50 | 42 | 17 | 1 |
| 3 | **3/25** | 1.798 | 10/25 | 20/25 | 18/25 | 20/20 | 0.74 | 0.50 | 58 | 23 | 0 |
| 1.5 | **3/25** | 1.825 | 9/25 | 19/25 | 17/25 | 19/19 | 0.70 | 0.50 | 73 | 29 | 1 |

### The metrics disagree, and the exactness peak is the chance artifact

`amount_safe_to_pay` **exact count** peaks at the tightest settings (3/25 at
stale 3 and 1.5). The **run-rate ratio** and **MARE** both peak at the loosest
(1.02 / 1.259 at None). They point in opposite directions.

Exact-count is the untrustworthy metric here, exactly as the brief warned —
seven settings against 25 labels, and the whole spread is 1–3 rows. MARE is the
same quantity measured continuously, and it moves **monotonically and
substantially** the other way: 1.259 → 1.825 as the gate tightens, a 45%
degradation. Two independent continuous metrics agreeing against one noisy
discrete one is not a close call.

`earliest_date` corroborates: 12/25 down to 9/25 at the tight end.

### `request_10` — the clearest single readout

| stale | my safe | err | proj out | obs out | ratio | debits | excluded |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| None / 12 / 8 / 6 | **0** | 100% *(under)* | 589,465 | 543,156 | **1.09** | 52 | 0 |
| 4 / 3 / 1.5 | **171,192** | **1248%** *(over)* | 353,563 | 543,156 | **0.65** | 24 | 4 |

The 13× overstatement **vanishes entirely** at stale ≥ 6. The forecast goes from
24 projected debits to 52 against 48 observed. All four excluded series sat at
4.4–5.1 periods — just past the old 4.0 cutoff — and all four were live
short-cadence grocery/dining/transport spending.

The row now *understates* (0 against a labelled 12,700), which is the safe
direction and a separate, smaller problem.

### Chosen: 12.0, not None

None and 12 are statistically indistinguishable on every metric (MARE 1.259 vs
1.260, ratio 1.02 vs 1.01). 12 is preferred because it retains a guard against
genuinely dead series — a monthly series must be silent a **full year** before
it is dropped — at zero measured cost. That is the "affirmative evidence of
death" bar the Phase 4 reasoning asked for; **4.0 was simply set far too tight**
and the original argument for it does not survive the direct metric.

### Effect on the full 250

| status | before (4.0) | after (12.0) | target | delta vs target |
| --- | ---: | ---: | ---: | ---: |
| `affordable_now` | 27.2% | **24.4%** | 12% | +15.2 → **+12.4** |
| `affordable_with_plan` | 28.4% | 26.0% | 36% | −7.6 → −10.0 |
| `affordable_later` | 19.2% | 18.0% | 24% | −4.8 → −6.0 |
| `not_affordable` | 25.2% | **31.6%** | 28% | −2.8 → **+3.6** |

`affordable_now` — the headline defect — falls 2.8 points. It is still 12 points
high, so staleness was one contributor, not the whole story. `not_affordable`
has crossed from under to over.

250 rows rendered, **0 validator violations**.

### Downstream re-measurements

* **Ranking: 19/19** (was 20/20 of 20 available). Still never picks a wrong
  single candidate, but the tighter forecast removed `request_04`'s second
  candidate, so **zero labelled rows now have a real choice** — criteria 2–5
  fire on no sample row at all. Ranking is *less* exercised than before.
* **`unsafe_against_forecast` rejections: 10 → 18** on the evaluation set, and
  **0 → 1** on the samples. The Phase 6 call that 10 was a floor is confirmed.
  The branch now has label coverage for the first time; synthetic tests for it
  remain green.
* **Spending-change rows: 1 → 3 of 250**, against ~30 expected. Barely moved —
  this is not primarily a staleness problem.

### Not fixed by this step

`min ratio` is **0.50 at every setting** — `request_18` projects half its
observed outflow regardless of the gate, so its defect is elsewhere. Four users
still project zero inflow. Both are Step 3/4 material.

## P11-S3b. Threshold rows 06 / 11 / 21 — **no shared cause, nothing changed**

The last concrete lead: three rows falsely `affordable_now` by 2-5%, and
exactly the three labelled spending-change rows. A small systematic
under-projection would have flipped all three statuses and recovered all three
change rows at once. It is not one cause.

Exact extra 90-day outflow needed to flip each, and what the user's own history
could supply:

| request | extra outflow needed | as % of projected | candidate cause | would supply |
| --- | ---: | ---: | --- | ---: |
| `request_06` | **346.29** | 17.3% | `Takeaway order` dropped by staleness (n=2, 7-day, **22.9 periods** stale) | 514.68 (**149%**) |
| `request_11` | **2,562,583** | 5.4% | `Supermarket basket` dropped by staleness (n=2, 10-day, **14.5 periods** stale) | 15,115,264 (**590%**) |
| `request_21` | **139.17** | 3.4% | **none found** | — |

Checked in order for each user: series dropped by staleness at 12; cadence
snapped to the wrong period; series whose occurrences span less than the
window; event classes excluded by classification.

**`request_21` has nothing.** Zero excluded series, zero projected-occurrence
shortfall, zero cadence mismatches, zero excluded event classes in the window.
Its 139.17 gap is not attributable to any of the four mechanisms.

**The other two share a mechanism but not a fix.** Both would be closed by a
stale two-occurrence series, but restoring them needs
`SERIES_STALENESS_PERIODS` raised from 12 to **beyond 23** — a global default
moved to close a 2-5% gap on two rows, which would re-break everything P11-S2
fixed. Both series are also genuinely dead: 160 and 145 days since their last
occurrence. And it would still leave `request_21` untouched.

**Decision: causes are per-user and unrelated. Nothing changed.**

### Incidental: a partial check on the recurrence classification

This is the one `CODE-DEPENDENT` claim with no independent grounding, and these
three users are a natural place to probe it. Across **29 recurring debit series**
the snapped cadence matches the raw modal gap everywhere except one:

* `request_06` / `Local market purchase` — raw modal gap **60 days**, snapped to
  **30**, so it projects 3 occurrences where 1-2 is right.

That mis-snap **over**-projects outflow, making that row more conservative, not
less — so it is not a contributor to the overstatement, and correcting it would
widen `request_06`'s gap rather than close it. Recorded as a known imprecision
in cadence snapping at long periods: 28 of 29 correct on this sample.

## P11-S3. Amount estimator and outflow completeness — **all three levers rejected**

Net result: **no default changed.** Two new flags added, both defaulting to the
existing behaviour, because every candidate lever measured worse or negligible.

### S3-FIRST. The gap is concentrated, not spread

Only **5 of 25** sample rows say `affordable_now` where the label does not, and
the relative deficit is overwhelmingly one row:

| request | label | deficit vs label | outflow ratio |
| --- | --- | ---: | ---: |
| `request_21` | `affordable_with_plan` | **2%** | 0.95 |
| `request_06` | `affordable_with_plan` | **3%** | 0.81 |
| `request_11` | `affordable_with_plan` | **5%** | 0.69 |
| `request_13` | `affordable_later` | 117% | 0.83 |
| `request_05` | `not_affordable` | **2001%** | 0.75 |

`request_05` alone is **94%** of the total relative deficit; the top two are
99.5%. By the brief's own criterion — *"even spread points at the estimator,
concentration points at whole missing flows"* — **this is concentration**, and
the estimator was predicted to be the wrong lever before it was swept.

Note the three 2–5% rows are exactly the C7/C8c spending-change rows. They are a
**threshold** effect, not a magnitude one: a few per cent more outflow flips
them from `affordable_now` to `affordable_with_plan` and would fix three status
rows *and* three spending-change rows together.

### S3-SECOND. Expense fragmentation is universal but is **not** costing outflow

The mirror of the S4 income defect exists and is total: **275 of 275 users**
have two or more non-recurring debit series inside one category, covering
**11,857 orphaned events**. The worst users fragment transport across nine
series and dining across eight.

But aggregating it **overshoots badly**, because outflow was already complete
after Steps 2 and 4:

| variant | median outflow ratio | status exact | method exact |
| --- | ---: | ---: | ---: |
| current (no aggregation) | **1.01** | **18/25** | **20/25** |
| naive `(category, cadence)` | 1.41 | — | — |
| rate-based, per category | 1.37 | 9/25 | 12/25 |

**Hypothesis rejected.** The median run-rate ratio was already 1.01 — the
projected recurring series reproduce each user's observed spending in
aggregate, so the fragments are already accounted for and adding them
double-counts. The rate-based variant collapses status accuracy from 18/25 to
9/25.

Kept behind `AGGREGATE_ORPHAN_EXPENSES` (default **False**) because the
fragmentation is real and a dataset with genuinely missing outflow would need
it.

### S3-THIRD. The estimator sweep — negligible

| estimator | safe | status | method | plan | earliest | **MARE** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **median** ← kept | 2/25 | **18** | **20** | **18** | **13** | 1.249 |
| mean | 2/25 | **18** | **20** | **18** | **13** | 1.248 |
| max | 2/25 | 17 | 19 | 17 | 12 | **1.226** |

Median and mean are indistinguishable. `max` improves MARE by **2%**
(1.249 → 1.226) while costing a row on three exact columns. For contrast, S2's
staleness sweep moved MARE by **45%**. There is no case for changing;
`SERIES_AMOUNT_ESTIMATOR` is added as a flag and left at `median`.

### S3 attribution: my trough balance, against what the label implies

Comparing my binding-trough balance to `floor + label_safe`:

| verdict | rows |
| --- | ---: |
| balance **HIGH** (too little outflow / too much inflow) | 18 |
| balance **LOW** | 6 |
| within 2% | 1 |

Still biased high, but no longer one-directional — six rows now err the other
way, and 8 of 25 are within 5%. That is consistent with the remaining error
being per-user rather than systematic, which is why a global estimator cannot
address it.

### The three open rows

**`request_11` — closed, not a defect.** Suppressing the "Performance
commission" series would remove **47,968,260** across three occurrences against
a deficit of **599,355** — an 80× overshoot that would swing the row far under.
The label therefore *does* count that commission. The `PENDING_COMMISSION` fact
refers to future commission on open deals, not to the established historical
stream, so projecting the stream is correct. **Hypothesis rejected; the open
item was a false alarm.**

**`request_18` — explained, not fixed.** Its 0.50 outflow ratio is pure
fragmentation: 22 non-recurring debit series across groceries, dining and
transport. Per-user aggregation would take it to 1.01 exactly. But the global
lever overshoots every other user (S3-SECOND), so applying it to fix one row
would cost far more than it gains.

**`request_05` — unexplained.** No messages, no extracted facts, no
scheduled or pending events; 9 recurring and 14 non-recurring debit series.
Projected outflow 27,660 against 37,118 observed (0.75) and projected income
44,220 against ~44,220 observed (1.00), so the balance barely dips and the
forecast minimum is 43,428. The label implies a trough near **13,837** — a fall
of 32,638 from an opening 46,475 — which is not reachable from this user's own
observed cash flows under any estimator tried. Something in the reference
model's treatment of this user is not recoverable from the data available.
Remains the single largest contributor to MARE.

---

## P11-S4. Inflow — root cause is series keying, not amendments or anchoring

### Findings, before any change

**`request_10` (the 13× case, then the 0 case).** Binding trough 2025-03-03 at
160,690 against a 225,400 minimum — headroom **negative**, hence
`amount_safe_to_pay = 0`. The user has **21 salary credits** on a textbook
weekly rhythm (4th, 11th, 18th, 25th of every month for five months), every one
`category=salary`, `direction=credit`, `status=settled`.

None of it projected, because the 21 events carry **four rotating payer
descriptions** — "Delivery platform payout", "Weekly app earnings", "Task
marketplace payout", "Driver platform payout". `state.py` keys series by
description, so one clean 7-day stream is shattered into four irregular ones
(gaps 14, 17, 7, 38, …), none passes the cadence test, `_salary_series` returns
`None`, and **zero income is projected**.

This is the **same defect as C7b**, where `user_11`'s dining spend was split
across six descriptions. Confirmed as a general keying limitation, now found on
the income side where it is far more damaging.

| request | descriptions | true rhythm | detected? |
| --- | ---: | --- | --- |
| `request_09` | 8 | 7th and 20th monthly | no |
| `request_10` | 4 | weekly, 4/11/18/25 | no |
| `request_12` | 2 | monthly, 15th | no |
| `request_14` | 2 | monthly, 15th | no |

**57 of 275 users projected no salary at all.**

### Hypotheses checked and rejected

| hypothesis | verdict |
| --- | --- |
| Anchor day falling back to the 15th default | **Rejected.** Derived per-user in 214/275; 205 genuinely land on the 15th, 7 on the 8th, one each on the 20th and 22nd. Only 4 series-found cases fall back, plus the 57 that project nothing anyway. |
| Income-ending amendments misfiring | **Rejected.** `ends_on` fires 4 times across 275, `next_payday_amount` 20, `resumes_on` 8, `next_payday_date` 7. No spurious firing found. |
| Month-end anchors mishandled | **Rejected.** 29 → Feb 28, 31 → Feb 28 and Apr 30. Clamping is correct. |
| Overshoot = one-off credit treated as recurring | **Rejected for `request_01`** — its 23,320 is a genuine *scheduled* event (confirmed next salary), correctly counted; the 1.74 ratio reflects a real prorated→full pay rise, not a defect. **Rejected for `request_11`** — the overshoot is a recurring "Performance commission" series that the message declares pending; that is an amendment-suppression gap, recorded below, not a recurrence error. |

### The 15th-clustering check the brief asked for

Labelled `earliest_date` day-of-month: `{15: 13, 3: 1, 4: 1, 5: 1, 12: 1, 23: 1}`.
Observed **salary** day-of-month sets across the same users: `{(15,): 18, (15, 23): 1}`.

**The clustering survives.** 19 of 25 sample users genuinely receive salary on
the 15th in their own history, so the labels agree with per-user derived
anchors, not with the default. The default was not confirming itself.

### The fix, and the variant chosen

`_aggregate_income_stream` pools **all** salary credits into one stream and
infers cadence from the union of dates. Used **only as a fallback** when no
description-keyed salary series exists, so users with consistent payroll keep
their validated behaviour. `SalaryPlan` gained `cadence_days`/`cadence_kind`/
`anchor_date` and `_salary_flows` learned to step a non-monthly cadence — a
7-day stream projected as monthly would lose three quarters of itself.

| variant | safe | status | method | plan | earliest | expl | **MARE** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| off (revert) | 1/25 | 17 | 19 | 17 | 12 | 11 | 1.260 |
| **monthly-only** ← chosen | 2/25 | 18 | 20 | 18 | 13 | **12** | **1.249** |
| all cadences | **3/25** | **19** | **21** | **19** | **14** | 11 | 1.972 |

**The metrics disagree again, and this time in the opposite direction from
P11-S2** — so the reasoning has to be stated rather than reused.

"All cadences" wins by one row on five columns. Its MARE penalty is **entirely
one row**: `request_10` swings from `safe = 0` (100% under) to `safe = 266,700`,
the full requested amount, against a label of 12,700 — a **20× overstatement**.
That single row moves the mean relative error by 0.76, which is the whole
1.249 → 1.972 gap. In P11-S2 MARE moved monotonically across every row; here it
is a single outlier. Neither metric is decisive on its own.

**Chosen on principle: monthly-only.** The rulebook says *"do not invent
unsupported future income"* and *"count confirmed salary on its settlement
date"*. A monthly payroll recorded as "Payroll before leave" / "Payroll after
returning from leave" is plainly one salary. Platform earnings ranging 40,977 to
82,667 under rotating payer names are not confirmed salary, and extrapolating
them forward is exactly the invention the rulebook forbids. It also has the best
MARE of the three, and it keeps `request_10` erring in the **safe** direction.

Recorded honestly: **"all cadences" scores better on five exact-match columns
and was rejected on principle and on error magnitude, not on score.** Both flags
are sweepable (`AGGREGATE_INCOME_STREAM`, `AGGREGATE_MONTHLY_INCOME_ONLY`).

### Re-measurement after S2 + S4

| metric | Phase 10 baseline | after S2 | after S4 |
| --- | ---: | ---: | ---: |
| `amount_safe_to_pay` exact | 2/25 | 1/25 | **2/25** |
| MARE | 1.728 | 1.260 | **1.249** |
| `earliest_date` exact | 12/25 | 12/25 | **13/25** |
| method exact | 20/25 | 19/25 | **20/25** |
| status exact | 18/25 | 17/25 | **18/25** |
| **outflow ratio median** | 0.78 | **1.01** | 1.01 |
| outflow ratio min | 0.50 | 0.50 | 0.50 |
| users below 0.80 outflow | 15/25 | 5/25 | **5/25** |
| users projecting **no** salary | 57/275 | 57/275 | **40/275** |
| users rescued by aggregation | — | — | **17/275** |

Full 250, **0 validator violations**:

| status | Phase 10 | after S2 | **after S4** | target |
| --- | ---: | ---: | ---: | ---: |
| `affordable_now` | 27.2% | 24.4% | **26.0%** | 12% (+14.0) |
| `affordable_with_plan` | 28.4% | 26.0% | 27.6% | 36% |
| `affordable_later` | 19.2% | 18.0% | 18.0% | 24% |
| `not_affordable` | 25.2% | 31.6% | **28.4%** | 28% (**+0.4**) |

`not_affordable` has landed almost exactly on target. `affordable_now` rose
1.6 points from S2 — more projected income makes more requests payable today —
and remains the outstanding defect at +14.0.

* **Ranking: 20/20** (was 19/19 after S2). Still never picks a wrong single
  candidate. Sample multi-candidate rows: **0** — criteria 2–5 still fire on no
  labelled row. On the evaluation set, `minimum_total_amount_paid` decides 4.
* **`unsafe_against_forecast`: 18 → 14** on the evaluation set. More projected
  income makes more installment schedules survive.
* **Spending-change rows: 3/250**, unchanged, against ~30 expected. Neither S2
  nor S4 moved this.

### Still open after S4

* `request_18` projects **0.50** of its observed outflow at every setting tried.
* `request_05` is unchanged at 2001% error — `amount_safe_to_pay` pinned to the
  requested-amount cap against a label of 737.
* `request_11`'s pending "Performance commission" is still projected despite the
  message declaring it unearned; the `PENDING_COMMISSION` fact does not suppress
  the corresponding historical series.

### Secondary finding, deferred to Step 4

Four users project **zero inflow** despite observed prior-period income:

| request | observed 90d inflow | projected |
| --- | ---: | ---: |
| `request_09` | 2,795 | **0** |
| `request_10` | 749,474 | **0** |
| `request_12` | 61,315 | **0** |
| `request_14` | 2,717 | **0** |

Zero inflow makes a forecast *more* conservative, so this is not what drives
the overstatement — `request_10` overstates 13× even with no projected income
at all. But it is a real defect and is the Step 4 starting point. Two users at
the other extreme (`request_01` at 1.74, `request_11` at 1.44) project *more*
income than observed, which is the same salary-extrapolation machinery failing
in the opposite direction.

Open questions whose answers are decided by evidence the pipeline cannot
settle on its own, recorded at the point the choice was made. Phase 11 sweeps
these against the 25 labelled rows in `dataset/sample_requests.csv`.

---

## C1. Image-path validation (Phase 3)

The image path resolves exactly four blank-amount events. **Two of them belong
to labelled sample rows**, and those two are the only direct validation the
image extraction ever gets — the other two are evaluation rows with no ground
truth.

| event | user | request | set | extracted | status |
| --- | --- | --- | --- | --- | --- |
| `event_1442` | `user_16` | `request_16` | **labelled** | INR 100,000.00 | corroborated, see below |
| `event_1786` | `user_20` | `request_20` | **labelled** | INR 704.05 | **unresolved, sweep required** |
| `event_6033` | `user_64` | `request_64` | eval | INR 79,679.26 | no label available |
| `event_6859` | `user_73` | `request_73` | eval | INR 3,650.00 | no label available |

### C1a. `event_1442` — corroborated by the label

`image_02` is a rent receipt with three candidate figures:

- Total Amount to be Received: 2,00,000.00
- Amount Received: 1,00,000.00
- **Balance Due: 1,00,000.00** ← chosen
- Rent & Maintenance line only: 1,80,000.00

The ledger row is *"Outstanding rent balance"*, which points at Balance Due.

The label agrees. Ground truth for `request_16` is `affordable_now`,
`amount_safe_to_pay = 122500`, with the explanation *"leaves at least INR
122,400 available over the next 90 days"* — exactly `minimum_balance_to_keep`.
Opening balance is 362,370. Paying 122,500 leaves 239,870; subtracting a
100,000 rent balance leaves 139,870, which can still land on the 122,400 floor
after ordinary recurring spend. Subtracting 200,000 instead leaves 39,870,
which is below the floor and could not be `affordable_now`.

**Action for Phase 11:** assert `request_16` reproduces the ground-truth row
exactly. If it does not, re-open the 200,000 / 180,000 alternates.

### C1b. `event_1786` — genuinely ambiguous, must be swept

`image_05` is a telecom bill with two defensible figures:

| candidate | source | argument |
| --- | --- | --- |
| **704.05** (chosen) | "Total (₹) 704.05", "Amount due till 06-Feb-2026" | Confirmed by the amount-in-words line, *"Seven Hundred Four Rupees and Five Paise Only"*, which makes it the document's own canonical total |
| 822.05 | "Amount due after 06-Feb-2026" | `event_1786` settles **2026-02-09**, three days *after* the due date, so the late-payment figure is what would actually be charged; also the financially safer reading, which the rulebook prefers when a conflict cannot be resolved |

The difference is 118.00 INR against a headroom of 38,109.05, so it is very
unlikely to flip the label — ground truth for `request_20` is `not_affordable`
/ `not_recommended` with `amount_safe_to_pay = 5400`, and 118 INR of extra
outflow does not plausibly move a 5,400 figure. But `amount_safe_to_pay` is
scored numerically, so the wrong choice may cost exact-match credit on that row.

**Action for Phase 11:** run `request_20` under both values and keep whichever
reproduces `amount_safe_to_pay = 5400`. If both do, prefer 822.05 on the
rulebook's "financially safer interpretation" tie-break. Alternates are
recorded in `audit/image_cache.json` under `event_1786.alternates`.

### C1c. Correction to the phase brief

The brief describes `event_1786` as *"the pending-refund trap … its image
supplies an amount for a PENDING credit"*. It is a pending **debit** —
*"Outstanding telecom bill"*, `direction=debit`, INR, settling 2026-02-09.

user_20's pending **credit** is a different row: `event_1785`, *"Pending
merchant refund"*, INR 8,640.00, which already carries an amount and needs no
image at all. It is excluded by its `pending_credit` classification.

The required invariant still holds, and for a stronger reason than the brief
assumed: resolving `event_1786` adds an *outflow*, so it can only ever
**reduce** available capacity. Asserted in
`tests/test_extract.py::test_user_20_extraction_never_raises_available_capacity`.

---

## C2. Config flags awaiting a sweep (Phase 2)

Declared in `src/config.py`, both read at call time and overridable via
`BUYORWAIT_*` environment variables.

| flag | default | alternative | why it is open |
| --- | --- | --- | --- |
| `FX_DATE_KEY` | `cash_date` | `event_date` | The contract says "use the row for its settlement date", but 178 events settle later than they occur; any of those that are also foreign-currency convert at a different rate under the two readings |
| `QUANTIZE_AT` | `conversion` | `render` | Rounding each converted amount matches a bank ledger; deferring avoids accumulating per-row rounding across a long forecast. Ground-truth margins are 1.9–3.45 currency units, so a systematic one-direction bias could matter |

---

## C3. Recurrence thresholds and stale-series gating (Phases 2 and 4)

### C3a. Recurrence detection (`state.py`)

| flag | default | note |
| --- | --- | --- |
| `RECURRENCE_MIN_OCCURRENCES` | 3 | "Detect recurrence only when history supports it"; two points define one gap |
| `RECURRENCE_ALLOW_TWO_OCCURRENCE_DEBITS` | `True` | Deliberately asymmetric — under-forecasting an outflow is the unsafe error, under-forecasting income is the safe one |
| `CADENCE_CONSISTENCY_THRESHOLD` | 0.6 | Skipped-occurrence multiples count half weight; see `src/state.py::_snap_cadence` |

### C3b. Stale-series gating (`forecast.py`) — hypothesis refuted

The Phase 4 brief proposed: *"First measure: how many series are both stale
AND have >= 3 occurrences. If that set is near-empty, gate on occurrence count
alone and leave staleness at None."*

**That set is not near-empty.** Measured across all 275 requests (2,866
detected recurring series):

| staleness threshold | stale series | of which n≥3 | of which n=2 |
| ---: | ---: | ---: | ---: |
| > 1.5 periods | 762 | **392** | 370 |
| > 2.0 periods | 666 | **314** | 352 |
| > 3.0 periods | 548 | **246** | 302 |
| > 4.0 periods | 428 | **187** | 241 |

So occurrence count alone does **not** isolate dead series, and staleness
cannot be left at `None` on the grounds the brief suggested. The two axes are
largely independent above three occurrences.

The axes are, however, strongly dependent *below* it:

| | fresh (≤1.5 periods) | 1.5–3 | > 3 periods |
| --- | ---: | ---: | ---: |
| n = 2 series (454) | 84 (18%) | 68 | **302 (67%)** |
| n ≥ 3 series (2,412) | **2,020 (84%)** | 146 | 246 (10%) |

Two-occurrence series are mostly dead; three-plus-occurrence series are mostly
live. That is the monthly backbone — rent, utilities, subscriptions, salary.

The most stale well-attested series are almost all **short-cadence**, which
inflates their period count: `request_267`'s *"Fuel refill"* (n=3, 5-day
cadence) was last seen **28.8 periods** before the request date. Projecting
that forward would invent spending that demonstrably stopped five months ago,
which the rulebook forbids under "do not invent unsupported future expenses".

### C3c. Chosen defaults, and why

> **AS OF PHASE 4.** `SERIES_STALENESS_PERIODS` was revised **4.0 -> 12.0**
> in P11-S2 after the direct metric contradicted this proxy sweep. The
> table below records the Phase 4 decision, not the current default.

| flag | default | reasoning |
| --- | --- | --- |
| `SERIES_STALENESS_PERIODS` | `4.0` | Only drops series unseen for four **full cadence periods** — affirmative evidence of death, not mere absence. A monthly series must be silent four months; a weekly one, a month. |
| `MIN_SERIES_OCCURRENCES` | `2` | i.e. **no gate beyond** what `state.py` already applies. Raising it to 3 is measurably worse — see below. |

Swept against the 25 labelled samples, using "is full payment on `request_date`
safe?" versus ground-truth `affordable_now` as a proxy:

| staleness | min occ | agree /25 | false positive | false negative | series excluded |
| ---: | ---: | ---: | ---: | ---: | ---: |
| `None` | 2 | 17 | 6 | 2 | 0 |
| `None` | 3 | 16 | **8** | 1 | 56 |
| 6.0 | 2 | 16 | 7 | 2 | 29 |
| **4.0** | **2** | 16 | 7 | 2 | 42 |
| 4.0 | 3 | 16 | **8** | 1 | 73 |
| 3.0 | 2 | 17 | 7 | 1 | 58 |
| 2.0 | 3 | 15 | **9** | 1 | 82 |
| 1.5 | 3 | 15 | **9** | 1 | 85 |

`MIN_SERIES_OCCURRENCES = 3` raises false positives from 6–7 to 8–9 at every
staleness setting. A false positive here means the pipeline calls a payment
safe when the label says it is not — it removed 56 series' worth of expenses
and **overstated capacity**, exactly the unsafe direction the brief warns
about. This is direct empirical support for the asymmetry principle, so the
count gate stays off.

Staleness itself is within noise on 25 samples (`None`, 3.0 and 4.0 all land
at 16–17 agreement). `4.0` is chosen on the principled ground above rather
than on this metric.

**Action for Phase 11:** re-sweep `SERIES_STALENESS_PERIODS` over
`{None, 6.0, 4.0, 3.0, 2.0}` once ranking exists and the metric is the real
score rather than this proxy. Keep `MIN_SERIES_OCCURRENCES = 2` unless the real
score contradicts the false-positive result above.

### C3d. Exclusion logging

Every excluded series is logged at INFO by `forecast.build_forecast` and
carried on `Forecast.excluded_series` as a `SeriesExclusion` with `reason`,
`occurrences`, `cadence_days`, `cadence_kind`, `last_occurrence` and
`periods_since_last`, so a swept run shows exactly what each setting killed.

`user_26`'s *"Neighbourhood grocer"* (n=2, 21-day cadence, last 2025-04-09,
**5.52 periods** before the 2025-08-03 request) is dropped by the 4.0 default.
Its sibling *"Supermarket basket"* (n=2, 10-day, 6.60 periods) is dropped too,
while all five of that user's monthly series (0.70–1.03 periods) are kept — so
the cutoff separates the two groups cleanly for this user without touching the
backbone.

---

## C3e. Capacity accuracy — the forecast is the bottleneck (Phase 5)

> **AS OF PHASE 5, pre-calibration.** Every figure in this section predates
> P11-S2 and P11-S4. `earliest_date` exact was 11/25 and is now 13/25;
> overstating was 19/25 and is now 16/25; the within-1%/5%/20% bands were
> 3/6/11 and are now 2/5/13. See the Final scorecard.

`src/capacity.py` is **exact given the forecast it is handed**. It returns the
headroom at the binding trough, and `test_a_late_binding_trough_sets_the_amount`
proves the answer is tight: paying it is safe, paying one cent more is not. So
every error below is an error in `forecast.py`, not in the capacity solve.

Scored against the 25 labelled samples (`audit/capacity_calibration.md`,
regenerate with `py audit/capacity_trace.py`):

| metric | result |
| --- | --- |
| `amount_safe_to_pay` exact | **2 / 25** |
| within 1% | 3 / 25 |
| within 5% | 6 / 25 |
| within 20% | 11 / 25 |
| `earliest_date_for_full_payment` exact | **11 / 25** |
| overstating capacity | **19 / 25** |
| understating capacity | 4 / 25 |

### The dominant signal: systematic over-statement

19 of 25 rows report **more** capacity than the label, several by a wide
margin — `request_05` (737 → 15,488), `request_10` (12,700 → 171,192),
`request_20` (5,400 → 17,626). The forecast is not projecting enough outflow.

`earliest_date_for_full_payment` corroborates this from the other side: where
it is wrong it is almost always **too early** (`request_06` computed
2026-01-03 against a labelled 2026-01-15; `request_13` computed 2024-03-07
against 2024-05-15). A forecast that under-projects spending reaches any given
threshold sooner than it should.

Ground-truth `earliest` dates cluster hard on **the 15th** — the salary anchor
— which suggests the reference model treats the next payday as the natural
unlock point. Computed dates that land mid-month are a symptom of the same
under-projection.

### The counter-signal: 4 rows understate

`request_01` (25,256 → 18,409), `request_09` (166.61 → 117.75), and
`request_14` / `request_15`, which both compute **0** against labels of 597.74
and 83.05 because the forecast dips *negative*. So the error is not a uniform
scale factor — some users are over-projected and most are under-projected,
which points at the **series amount estimator** (currently
`RecurringSeries.median_amount`) and at short-cadence variable spending rather
than at a single global bias.

### Where the brief's expected gaps landed

The brief anticipated "users 02/03 (~1%) and 06/21 (~7-17%)". Measured:

| row | brief expected | actual |
| --- | --- | --- |
| `request_02` | ~1% | **16.6%** |
| `request_03` | ~1% | **28.7%** |
| `request_06` | 7–17% | **2.8%** |
| `request_21` | 7–17% | **2.0%** |

06 and 21 are now well inside tolerance; 02 and 03 are much worse than
expected. The pairs have effectively swapped, so the brief's calibration
predates the current forecast.

### Action for Phase 11

Attribute each miss using `audit/capacity_trace.jsonl`:

- **`binding_trough_date` wrong** → the *shape* of the forecast is wrong: a
  series projected that should not be, or dropped that should not be. Re-sweep
  `SERIES_STALENESS_PERIODS` (C3c) first.
- **date right, `binding_trough_balance` wrong** → the *amounts* are wrong:
  the series amount estimator, or a missing/double-counted flow. Try
  `mean_amount` and `max_amount` in place of `median_amount`, and consider
  forecasting bursty variable categories (groceries, transport, dining) as a
  conservative daily rate rather than as discrete dated occurrences.

Do not tune `capacity.py` to close these gaps — it is provably tight, and
bending it would hide the forecast defect rather than fix it.

---

## C6. Candidate eligibility (Phase 6)

### C6a. The `[HYP]` installment month-cap gate is a no-op

`ENFORCE_INSTALLMENT_MONTH_CAP` (default **on**) is the only eligibility rule
inferred rather than stated. The contract says an option "may still be rejected
because it conflicts with … `max_installment_months`" without defining the
comparison, and the units do not match — the cap is in *months*, options are in
*payment counts* at 28/30/31-day intervals.

Measured across every request: **zero** installment options exceed the cap
without *also* missing their deadline.

| set | installment options | late | over-cap but not late |
| --- | ---: | ---: | ---: |
| evaluation | 469 | 394 | **0** |
| samples | 40 | 20 | **0** |

So the gate never changes an outcome, and Phase 11's sweep of it is free.
Asserted by `test_the_installment_cap_gate_is_a_no_op_on_this_dataset`, which
fails loudly if a future dataset makes it load-bearing. A synthetic test covers
the branch itself, since real data never reaches it.

### C6b. The rulebook's rejection claim holds for samples, breaks on evaluation

> **AS OF PHASE 6.** `unsafe_against_forecast` was 10 on the evaluation set
> and 0 on the samples; after P11-S2/S4 it is **14** and **0-1**. The claim
> that 10 was a floor was confirmed by that rise.

The claim that deadline compliance explains option rejection is **true for the
25 labelled samples**: every rejected payment option there is rejected for the
user's method preference or for the deadline, nothing else.

| reason (payment options only) | samples | evaluation |
| --- | ---: | ---: |
| `method_not_considered` | 20 | 208 |
| `past_deadline` | 20 | 187 |
| `unsafe_against_forecast` | **0** | **10** |
| `exceeds_installment_cap` | 0 | 0 |

**It does not hold on the evaluation set**, where 10 options are rejected as
unsafe against the forecast. Because the forecast currently *overstates*
capacity (C3e), 10 is a **floor** — a better-calibrated forecast rejects at
least that many.

Consequence: the samples do not exercise the safety-rejection path at all. A
ranking implementation validated only against them would leave that branch
untested. Both facts are pinned as tests so a change to either is visible.

### C6c. Ceiling on method accuracy from the current forecast

> **AS OF PHASE 6.** The 20/25 availability ceiling still holds, but the
> per-method breakdown below predates P11-S4.

For how many labelled samples does generation even *produce* the ground-truth
method as a candidate?

| ground-truth method | available | missing |
| --- | ---: | ---: |
| `installments` | 5 | 0 |
| `wait` | 5 | 1 |
| `full_payment` | 4 | 2 |
| `not_recommended` | 6 | 1 |
| `partial_payment` | **0** | **1** |

**20 / 25.** Ranking cannot beat that without a forecast fix, so this is the
current ceiling, not a ranking problem.

The single `partial_payment` sample (`request_19`) is instructive: ground truth
splits 28,820 today plus the remainder later, but the over-stated forecast
computes `amount_safe_to_pay` as the *full* 39,660, so partial is rejected as
`full_amount_is_already_safe_today`. Fixing C3e should recover this row
automatically. Do **not** relax the partial-payment gate to force it.

---

## C7. Spending changes (Phase 7) — two data/rulebook conflicts

The rulebook marks this section `[VAL]`, so it is implemented literally. Two
instructions conflict with the data. Both are reported here rather than
resolved silently.

### C7a. Reproduction against the labels

3 of the 25 samples carry spending changes in ground truth.

| row | ground truth | selected | outcome |
| --- | --- | --- | --- |
| `request_06` | `stop:event_476` | *nothing* | looked already-safe |
| `request_11` | `reduce_to:event_989:665950` | *nothing* | looked already-safe |
| `request_21` | `stop:event_1815\|reduce_to:event_1816:23.50` | *nothing* | looked already-safe |

**Exact reproductions: 0 / 3. Wrong sets selected: 0 / 3.**

The separation the brief asked for is unambiguous: **no miss is a `changes.py`
defect.** All three rows compute `amount_safe_to_pay == requested_amount`
under the current forecast, so the plan is already safe and no change is
needed. This is C3e's over-statement showing up one stage later.

Evidence that the selection itself is right, obtained by sizing a payment to
create the deficit the label implies:

* **`request_21`** — eligible changes in event order are
  `stop:event_1815`, `reduce_to:event_1816:23.50`,
  `reduce_to:event_1817:49.60`, `reduce_to:event_1854:41`. Applying the
  label's own deficit (1,574.40 − 1,543.35 = **31.05**) selects exactly
  `stop:event_1815|reduce_to:event_1816:23.50` — the label, verbatim. A
  deficit of 5–10 selects one change, 15–30 selects two, 35+ selects three, so
  the label's 31.05 lands squarely in the two-change band.
* `event_1817` (shopping) frees 71.14 per occurrence, more than `event_1815`
  and `event_1816` combined, and alone would suffice. Greedy-in-event-order
  skips it, which is exactly the rulebook's cited behaviour and rules out both
  fewest-changes and largest-freed-first.
* **`request_06`** — the only eligible change is `stop:event_476`, matching
  the label.

So the rule reproduces 2 of the 3 labelled sets once the forecast is corrected.
Fixing C3e should recover these rows. **Do not tune `changes.py` for them.**

### C7b. CONFLICT: `request_11` changes a non-recurring series

Ground truth changes `event_989` ("Weekend food delivery"), whose series has
**two occurrences 126 days apart** (2024-12-18 and 2025-04-23). That is not
recurring under any reasonable test, yet the rulebook states *"only recurring
expenses marked as flexible may be changed"*.

The conflict has a second consequence: a non-recurring series has **no future
occurrences**, so changing it frees nothing inside a 90-day window. Even with
`CHANGES_REQUIRE_RECURRING=False` the row cannot be reproduced, because the
change has no effect on any trough.

`user_11`'s eligible set under the literal rule is
`reduce_to:event_948:755250` (entertainment, monthly) and `stop:event_949`
(cloud storage, monthly). Ground truth uses neither.

This also means **no single ordering reproduces all three labelled rows**:
event-id ascending gives `request_06` and `request_21` but not `request_11`;
date-descending gives `request_11` but not `request_21`. Event order is kept,
per the brief.

A plausible reading is that the reference model treats *dining* as one
category-level spend rather than six description-keyed series — every dining
event for `user_11` carries the same `minimum_allowed_amount` of 665,950, which
looks like a per-category floor. Not adopted, because it contradicts
`request_21`, where the chosen events are category representatives ordered by
event id and the dining representative is skipped.

### C7c. The second gate layer is redundant

Eligibility is implemented as the instructed two layers — the event's
`flexibility` **and** its category being in the user's willing list. Layer two
never excludes anything on this dataset: all 2,907 reduce-capable events sit in
a permitted reduce category, all 1,522 stop-capable events in a permitted stop
category, and no flexible event sits in a protected category. Both layers are
applied anyway so a future dataset that breaks the alignment is handled;
`test_the_category_layer_is_redundant_on_this_dataset` pins the fact.

### C7d. Under-generation is severe

> **AS OF PHASE 7.** Spending-change rows were 1 of 250 and are now **3 of
> 250** after P11-S2/S4 — still far below the ~30 expected, so the finding
> stands and only the number has moved.

Only **1 of 250** evaluation rows currently produces a changes-based candidate.
Given that 3 of 25 samples (12%) carry changes, the expected figure is nearer
30. This is a direct consequence of C3e and is the single clearest measure of
how much the forecast over-statement costs.

**Action for Phase 11:** re-measure this after fixing C3e. If changes-based
candidates do not rise to roughly 10% of rows, the selection rule needs
re-examination — but not before.

---

## C8. Ranking (Phase 8) — no ranking defect, but barely exercised

> **AS OF PHASE 8.** Method/status exactness and the criterion counts below
> predate P11-S2/S4. Ranking's own figure (method exact given the label was
> generated) is unchanged at 20/20.

### C8a. The three numbers

| metric | result | what it diagnoses |
| --- | ---: | --- |
| method exact | 20 / 25 | dominated by C3e, not ranking |
| status exact | 18 / 25 | same; status follows the winning method |
| **method exact given the label was generated** | **20 / 20** | **the only ranking measure** |

**Ranking picks the labelled method every time candidate generation offered it
one.** There is no visible ranking defect. Both other numbers are capped by the
C6c ceiling — the label's method simply is not among the candidates for 5 rows.

### C8b. The five method misses are all availability, not ranking

`request_01`, `request_05`, `request_09`, `request_13`, `request_19` — every
one has `available = no`. Generation never produced the labelled method, so
ranking had nothing to pick. All trace back to C3e.

### C8c. The three status-only misses are exactly the C7 rows

`request_06`, `request_11`, `request_21` get the **method right and the status
wrong** — and they are precisely the three labelled rows that carry spending
changes. The overstated forecast reports no change is needed, so the winner is
a plain full payment and the status becomes `affordable_now` instead of
`affordable_with_plan`.

A status-assignment defect would scatter across unrelated rows. This one lands
on exactly the C7 set, so it is C3e again.
`test_the_status_only_misses_are_exactly_the_spending_change_rows` pins the
identity so a real status bug would break it.

### C8d. Ranking almost never has a choice

> **AS OF PHASE 8, and counted over all 275 requests.** Current counts over
> the same 275: `only_eligible_candidate` **192**, `no_eligible_candidate`
> **79**, `minimum_total_amount_paid` **4**,
> `requires_no_spending_changes` **0**. Seven requests lost their single
> candidate and `request_04` lost its changes-based one. Criteria 3, 4 and 5
> still fire on **zero** requests.

Across all 275 requests, which criterion decided the winner:

| criterion | requests |
| --- | ---: |
| `only_eligible_candidate` | 199 |
| `no_eligible_candidate` | 71 |
| `minimum_total_amount_paid` | 4 |
| `requires_no_spending_changes` | 1 |

**Ranking made a real choice in 5 of 275 requests.** 270 were settled by
candidate generation alone. So the 20/20 figure, while clean, is weak evidence:
only one labelled row (`request_04`) had two candidates to choose between.

Criterion-3, -4 and -5 have **never fired on real data**. They are covered by
synthetic tests only. Consequence: if C3e's fix makes more candidates eligible,
these paths become live for the first time and should be re-measured.

### C8e. Ranking is total

No candidate pair survived all five criteria on any of the 275 requests, so
`RankingTieError` never fired. A synthetic test confirms it raises rather than
picking arbitrarily when the criteria genuinely do not determine a winner.

### C8f. One documented divergence: wait vs fee-bearing installments

The brief expected *"a wait candidate never wins over any safe immediate
candidate"*. That holds against `full_payment` and `partial_payment`, both of
which total exactly `requested_amount` and therefore lose to nothing on
criterion 2 and win on criterion 3.

It does **not** hold against installments, because criterion 2 (minimum total
paid) outranks criterion 3 (start earlier) and an installment plan carries a
financing fee. Waiting genuinely is the cheaper plan, and the rulebook's stated
order prefers it. Implemented as stated;
`test_wait_does_beat_a_fee_bearing_installment_plan` documents the case.

No labelled row exercises it — every `installments` label belongs to a user who
does not accept `full_payment`, so `wait` is never eligible alongside it.

---

## C9. Formatting and explanations (Phase 9)

> **AS OF PHASE 9, and still current.** Formatting is independent of the
> calibration steps; all figures in this section remain accurate.

### C9a. Formatting is verified, 100/100

Rendered from **ground-truth values** so the measurement is unaffected by C3e.
Every numeric and date field of all 25 labelled rows reproduces
character-for-character: **100 / 100 field checks exact**
(`audit/render_validation.md`, regenerate with `py audit/render_validation.py`).

The two decimal conventions are confirmed distinct and are pinned by
`test_the_same_value_renders_differently_in_the_two_columns`: one
`Decimal("603.30")` renders `603.3` in `amount_safe_to_pay` and `603.30` inside
a plan.

### C9b. One rule no sample exercises

**`amount_safe_to_pay` with a trailing zero to strip.** Every labelled value is
already minimal (`603.3`, never `603.30`), so no label can prove the stripping
direction. It matters because `capacity.py` produces two-decimal Decimals, so
the strip fires on nearly every real row.

Covered by unit test instead — `format_minimal(Decimal("603.30")) == "603.3"`
and `format_minimal(Decimal("25256.00")) == "25256"`, including an explicit
guard that a whole number never emerges as `2.5256E+4`, which bare
`Decimal.normalize()` would produce.

### C9c. The template inventory is complete; two rows use variant phrasings

Every labelled explanation fits one of the six templates. **No row falls
outside the inventory.** Exact fills:

| template | rows | exact |
| --- | ---: | ---: |
| `installments` | 5 | **5/5** |
| `full+changes` | 3 | **3/3** |
| `partial` | 1 | **1/1** |
| `not_affordable_A` | 5 | **5/5** |
| `not_affordable_B` | 2 | **2/2** |
| `affordable_now` | 3 | 2/3 |
| `wait` | 6 | 5/6 |

**23 / 25 exact.** The two misses are alternate wordings of the same semantic
template, not new templates:

* `request_09` — *"This **keeps the** EUR 600 **minimum available** over the
  next 90 days"* against the majority *"This **leaves at least** EUR 600
  **available** over the next 90 days"* (2 of 3 rows).
* `request_04` — *"**Wait until** 15 June 2024, **then pay** … **Paying
  sooner would put** the … **minimum at risk**"* against the majority
  *"**Pay** … **in full on** 15 June 2024. **Paying earlier would take the
  balance below** the … minimum"* (5 of 6 rows).

The majority wording is used in both cases. Whether the reference model varies
phrasing by some unobserved signal is unknown; with 3 and 6 rows there is not
enough evidence to model it, and `decision_explanation` is scored on usefulness
and consistency rather than exact match.

### C9d. CONFLICT: the A/B threshold direction is inverted in the brief

The brief states *"choose B over A when `amount_safe_to_pay` /
`requested_amount` is below roughly 10%"*. The labels say the opposite:

| template | rows | ratio range |
| --- | --- | --- |
| **A** ("None of the available options keeps the … minimum protected") | 5 | 1.8% – 4.8% |
| **B** ("Although EUR 597.74 **is available today** …") | 2 | **11.0% – 12.2%** |

B is used at **high** ratios. Its wording only makes sense when a meaningful
amount *is* available, so the labels are self-consistent. Implemented as
`ratio >= 0.10 -> B`, with `test_the_a_b_threshold_direction_follows_the_labels`
asserting the highest A ratio sits below the threshold and the lowest B ratio at
or above it. The ~10% magnitude in the brief is right; only the direction was
inverted.

### C9e. No model call in the render path

`decision_explanation` is a deterministic string fill.
`test_rendering_makes_no_model_call` parses `src/render.py`'s AST and asserts it
imports neither `anthropic` nor `src.extract` and calls neither
`messages.create` nor `TokenMeter`. Per-request model cost therefore stays at
**zero** (C4).

---

## C4. Extraction coverage (Phase 3)

Deterministic template coverage is **100% of 215 messages**
(`audit/extraction_coverage.md`), so the LLM fallback never fires on this
dataset and `evaluation/usage_raw.jsonl` contains **zero**
`phase=message_fallback` records. The fallback is retained for robustness, not
because the data needs it.

Consequence for the usage report: per-request model cost for the full
evaluation run is **zero**. The only model spend is the one-time four-call
image prepass, which is why every record carries a `phase` tag.

---

## C5. Image-cache provenance

No `ANTHROPIC_API_KEY` was configured in the build environment, so
`audit/image_cache.json` was seeded by direct vision reading inside the Claude
Code session that authored the pipeline, recorded as
`provenance: "claude-code-session:claude-opus-5"` on every entry and as
`provider: "claude-code-session"` in `evaluation/usage_raw.jsonl`.

`py -m src.extract --prepass` reproduces the same four values through the
metered API path when a key is present. The distinction is kept explicit so
the usage report never reports session reading as API spend.

---

## P11-S6. The cadence constants — **all four confirmed, nothing moved**

Harness: `evaluation/sweep_cadence.py`. Each knob swept alone, the other three
held at their current values, on the same metric set every prior step used.

These four were the gap in the project's own standard. Every other constant in
`src/config.py` carried a sweep, a measurement, or an explicit statement of
principle; these were set to make a single synthetic test pass and never
revisited — while gating recurrence detection, which drives the whole forecast.
`CADENCE_SKIP_WEIGHT` was not even a named constant: it was the literal `0.5`
inside `state._snap_cadence`. It was extracted here, value unchanged, so it
could be swept at all (`output.csv` SHA verified identical across that change).

Judging rule, unchanged: continuous metrics beat discrete exact-counts when
they disagree; name any single row driving a swing; do not move a default on
exact-count alone.

### `CADENCE_CONSISTENCY_THRESHOLD` — confirmed at 0.6

| value | MARE | ratio | safe | status | method | recurring | `affordable_now` |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.0 | 2.277 | 1.70 | 1/25 | 9/25 | 10/25 | 5273 | 40 |
| 0.3 | 2.290 | 1.61 | 1/25 | 9/25 | 10/25 | 4967 | 44 |
| 0.4 | 1.487 | 1.43 | 1/25 | 9/25 | 10/25 | 4529 | 47 |
| 0.5 | 1.461 | 1.40 | 1/25 | 9/25 | 10/25 | 4422 | 47 |
| **0.6** ← kept | **1.248** | **1.00** | 2/25 | **18/25** | **20/25** | 2866 | 65 |
| 0.7 | **1.211** | 0.84 | 2/25 | 18/25 | 20/25 | 2616 | 66 |
| 0.8 | 1.498 | 0.79 | **3/25** | 18/25 | 20/25 | 2404 | 62 |
| 1.0 | 1.526 | 0.79 | **3/25** | 18/25 | 20/25 | 2372 | 64 |

0.6 sits on a **cliff, not a plateau**: 0.5 → 0.6 moves status 9 → 18 and
method 10 → 20 in one step, as the recurring-series count halves from 4,422 to
2,866 and the run-rate ratio falls from 1.40 to 1.00.

0.7 is the only rival — MARE 1.211 against 1.248, a 3.0% edge. **Rejected:** the
run-rate ratio collapses 1.00 → 0.84, a 16% systematic loss of projected
outflow in the unsafe direction, and over/under moves 16/7 → 17/6. The 3.0% is
spread over six rows moving both ways (`request_01` −0.63, `request_14` −0.62
against `request_24` +0.18, `request_23` +0.17). For scale, the P11-S2 sweep
that *did* move a default showed a 43% MARE spread. A 3% move cannot outvote a
16% one.

**The most useful thing this sweep shows** is at the low end. Tightening to
0.0–0.3 pulls `affordable_now` from 65 toward the labelled 30 — which looks
like a fix for the residual defect, and is not: `not_affordable` explodes to
165–174 against a target of 70, and MARE nearly doubles. Total absolute
distribution deviation goes from 72 rows at 0.6 to 228 at 0.0. This is the
clearest demonstration in the project that the status distribution is a weak
proxy and must not be optimised directly.

### `CADENCE_SKIP_WEIGHT` — confirmed at 0.5

| value | MARE | ratio | safe | status | method | over/under | recurring |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.0 | 1.466 | 0.87 | **3/25** | **19/25** | **21/25** | 18/4 | 2563 |
| 0.25 | **1.245** | 0.95 | 2/25 | 18/25 | 20/25 | 17/6 | 2742 |
| **0.5** ← kept | 1.248 | **1.00** | 2/25 | 18/25 | 20/25 | 16/7 | 2866 |
| 0.75 | 1.251 | 1.13 | 2/25 | 16/25 | 17/25 | 15/8 | 3411 |
| 1.0 | 1.286 | 1.18 | 1/25 | 13/25 | 14/25 | 13/11 | 3743 |

MARE is **flat** across 0.25/0.5/0.75 — 1.245 / 1.248 / 1.251, a 0.5% spread,
which is no signal. The ratio is the only metric that separates them and moves
monotonically 0.87 → 1.18, crossing 1.00 at exactly 0.5. On a flat continuous
metric the tie-break is the other continuous metric, not exact counts.

Exact counts peak at 0.0 — the P11-S2 pattern repeating. At 0.0 MARE is the
worst in the sweep, and **the whole degradation is one row**: `request_10`
1.00 → 6.86, the same row P11-S1 diagnosed. 0.0 also means a skipped occurrence
counts for nothing, which is exactly the synthetic failure this weight was
introduced to fix (gaps of 16/67/6 days reading as a 5-day cadence at 67%).

### `CADENCE_TOLERANCE_DAYS` — confirmed at 1

| value | MARE | ratio | safe | status | method | recurring |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | **1.225** | 0.99 | 2/25 | 18/25 | 20/25 | 2806 |
| **1** ← kept | 1.248 | **1.00** | 2/25 | 18/25 | 20/25 | 2866 |
| 2 | 1.842 | 0.95 | 2/25 | 17/25 | 19/25 | 2885 |
| 3 | 1.974 | 1.10 | 1/25 | 13/25 | 15/25 | 3138 |

0 and 1 are **indistinguishable**: identical on every exact count, ratio 0.99
against 1.00. The 1.9% MARE edge for 0 is two rows moving in opposite
directions — `request_09` 0.92 → 0.13 and `request_03` 0.14 → 0.34.

Kept at 1 on the domain argument, since the data does not separate them: a real
monthly debit lands a day early or late when a payday or month boundary moves
it, and tolerance 0 demands exact day-gaps forever. Beyond 1 it degrades hard —
at 2, `request_10` alone goes from 1.00 to a 17.45× overstatement.

### `MAX_SKIPPED_OCCURRENCES` — confirmed at 3

| value | MARE | ratio | safe | status | method | over/under | recurring |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 1.466 | 0.87 | **3/25** | **19/25** | **21/25** | 18/4 | 2563 |
| 2 | **1.213** | 0.94 | **3/25** | **19/25** | **21/25** | 17/5 | 2704 |
| **3** ← kept | 1.248 | **1.00** | 2/25 | 18/25 | 20/25 | 16/7 | 2866 |
| 4 | 1.260 | 1.01 | 1/25 | 16/25 | 18/25 | 15/9 | 3003 |
| 6 | 1.301 | 1.08 | 1/25 | 13/25 | 16/25 | 12/12 | 3239 |

**This is the one sweep where a rival genuinely wins on the label metrics, and
it is still rejected.** 2 beats 3 on MARE (−2.8%) *and* on every exact column —
safe 3/25, status 19/25, method 21/25, plan 19/25, earliest 14/25.

But the entire gain is **one row**. Per-row attribution:

| row | at 3 | at 2 | MARE contribution |
| --- | ---: | ---: | ---: |
| `request_09` | 0.9230 | 0.0000 | **−0.0369** |
| `request_24` | 0.0564 | 0.0978 | +0.0017 |
| *(all other 23 rows)* | — | unchanged | 0.0000 |

Total −0.0353, which is the whole observed MARE move.

The mechanism is real, not luck. `user_09`'s `Bulk pantry shop` has gaps
[90, 30] and `Local taxi` has [63, 21]; 90 is 3× monthly and 63 is 3×21, so at
3 both snap to consistency 0.75 and project, while at 2 neither matches and
both drop. `request_09` is one of the 7 **under**-stating rows, so removing
that outflow lands it exactly on its label of 166.61.

**That is precisely why 2 is rejected.** The same mechanism removes outflow
from *everyone*: the run-rate ratio falls 1.00 → 0.94 across all 25 users, and
over/under moves 16/7 → 17/5. It buys one fixed under-statement by pushing the
whole dataset further into **over**-statement — the unsafe direction, and the
documented residual defect. Trading a systematic 6% outflow loss for one row is
the move P11-S2 established should not be made.

### What the four sweeps show together

**The current configuration is a local optimum of the run-rate ratio in all
four coordinates.** Moving any one knob in either direction moves the median
ratio away from 1.00: threshold 0.5 → 1.40 and 0.7 → 0.84; skip weight
0.25 → 0.95 and 0.75 → 1.13; tolerance 0 → 0.99 and 2 → 0.95; max skips
2 → 0.94 and 4 → 1.01.

State that carefully: this is **one measured point seen from four directions**,
not four independent confirmations. Each sweep holds the other three at
current, so the 1.00 in every table is the same run. What the four sweeps
independently establish is only that the point is a local optimum in each
coordinate — no interaction between the knobs was swept, and a joint sweep
could still find something better.

**None of the four is a lever on the residual defect.** Measure this on total
absolute distribution deviation — the sum of |produced − reference| across all
four statuses, where the reference counts are later 60, now 30, with_plan 90,
not_affordable 70. The current configuration scores **72**. Every setting in
all four sweeps that pulls `affordable_now` below 62 scores **worse**, without
exception:

| setting | `affordable_now` | `not_affordable` | MARE | total deviation |
| --- | ---: | ---: | ---: | ---: |
| **current** | 65 | 71 | 1.248 | **72** |
| max skips 6 | 57 | 100 | 1.301 | 114 |
| skip weight 1.0 | 54 | 119 | 1.286 | 146 |
| threshold 0.5 | 47 | 147 | 1.461 | 188 |
| threshold 0.4 | 47 | 150 | 1.487 | 194 |
| threshold 0.3 | 44 | 165 | 2.290 | 218 |
| threshold 0.0 | 40 | 174 | 2.277 | 228 |

Every one trades the `affordable_now` excess for a larger `not_affordable`
excess. The +14-point gap is **not** reachable through recurrence detection,
which narrows where it can live.

> **CORRECTION, same session.** The first version of this paragraph made the
> same claim on a different and **wrong** basis: *"Across every setting whose
> MARE stays under 1.5, `affordable_now` stays in the band 62–66... The only
> settings that reduce it (threshold ≤ 0.5, giving 40–47)..."* Both halves are
> false. Four settings have MARE under 1.5 with `affordable_now` outside 62–66
> — threshold 0.4 (1.487, 47), threshold 0.5 (1.461, 47), skip weight 1.0
> (1.286, 54), max skips 6 (1.301, 57) — and the last two are not
> `threshold ≤ 0.5`, so "the only settings" was wrong too. The error was
> choosing a MARE cutoff to define the comparison set instead of reporting the
> deviation figure that had already been computed. It is recorded rather than
> quietly replaced because the point of this log is what was tried and what was
> mistaken; the replacement above is both correct and stronger, since it admits
> no exceptions rather than holding only inside an arbitrary MARE band.

**Outcome: no default changed.** Four sweeps, 22 settings, all confirming.
Recorded as a complete result — the cluster is no longer the one place the
project's own standard was not applied.

---

# FINAL SCORECARD

**The only current figures in this file.** Everything above is dated history.

Regenerate with `py evaluation/calibrate.py --full` and `py -m src.run`.

## Labelled sample set (25 rows)

| column | exact |
| --- | ---: |
| `amount_safe_to_pay` | 2 / 25 |
| `affordability_status` | 18 / 25 |
| `recommended_payment_method` | 20 / 25 |
| `payment_plan` | 18 / 25 |
| `earliest_date_for_full_payment` | 13 / 25 |
| `spending_changes_needed` | 22 / 25 |
| `decision_explanation` | 12 / 25 |
| **all eight columns** | **2 / 25** |

`amount_safe_to_pay` mean absolute relative error: **1.248**
(16 over-state, 7 under-state, 2 exact).

Method exact **given the labelled method was generated at all**: **20 / 20** —
ranking never picks a wrong candidate when offered a choice.

## Failure buckets, ranked

| count | class |
| ---: | --- |
| 16 | `capacity_over` |
| 13 | `explanation_mismatch` |
| 12 | `wrong_earliest_date` |
| 7 | `capacity_under` |
| 7 | `wrong_plan` |
| 7 | `wrong_status` |
| 5 | `method_not_generated` |
| 5 | `wrong_method` |
| 3 | `wrong_changes` |

## Full evaluation set (250 rows)

| status | count | share | reference | delta |
| --- | ---: | ---: | ---: | ---: |
| `affordable_now` | 65 | 26.0% | 12% | +14.0 |
| `affordable_with_plan` | 69 | 27.6% | 36% | −8.4 |
| `affordable_later` | 45 | 18.0% | 24% | −6.0 |
| `not_affordable` | 71 | 28.4% | 28% | **+0.4** |

Rows carrying spending changes: **3 / 250**.
Installment options rejected as unsafe against the forecast: **14**.

## Health

| check | result |
| --- | --- |
| tests | **345 passed** |
| output rows | **250 / 250** |
| validator violations | **0** |
| model calls per request | **0** |
| total model spend | 4 calls, $0.046 (one-time prepass) |
| `code.zip` | 54 files, ~284 KiB, secret scan clean |
| `chat_transcript.md` | ~650 KiB, 29 prompts, generated from the session log |

## Ranking exercise (all 275 requests)

| criterion | requests decided |
| --- | ---: |
| `only_eligible_candidate` | 192 |
| `no_eligible_candidate` | 79 |
| `minimum_total_amount_paid` | 4 |
| `requires_no_spending_changes` | 0 |
| `earliest_first_payment` | **0** |
| `fewer_payments` | **0** |
| `lowest_payment_option_id` | **0** |

Only four requests reach a tie-break, all `installments` versus
`partial_payment`. Criteria 3–5 have never fired on real data and rest on
synthetic tests.
