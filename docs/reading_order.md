# Reading order

You will read everything. This says what to read **first**, so the rest lands
in context rather than needing reconstruction.

Every core module opens with a `VALIDATION STATUS` block giving its own
confidence and known limits, so you learn a module's standing on arrival.

---

## The data flow, in five lines

1. **`dataset/` → `src/load.py`** — nine CSVs become frozen dataclasses. Money
   is `Decimal` parsed from source text, never float. Foreign amounts convert
   to the user's home currency at the settlement-date rate.
2. **→ `src/state.py`** — each of ~25,000 events lands in exactly one cash
   bucket, and repeating costs are grouped into series with an inferred cadence.
   `current_available_balance` is truth as of `request_date`; settled history is
   never re-applied.
3. **→ `src/extract.py` + `src/forecast.py`** — messages become typed facts;
   those facts amend the projection; a daily balance is walked forward 90 days.
4. **→ `src/capacity.py` → `src/candidates.py` → `src/changes.py` →
   `src/rank.py`** — how much is safe, which plans are eligible and safe, which
   spending changes rescue an unsafe one, and which plan wins.
5. **→ `src/render.py` → `src/validate.py` → `output.csv`** — eight formatted
   columns, re-checked from the written CSV text before the file is accepted.

`src/run.py` wires these together. `src/config.py` holds every decision that
could reasonably have gone another way.

---

## Read these five first

### 1. `src/config.py` — the argument, in one file

Every calibrated constant with the measurement that set it, and every rejected
alternative with why it lost. Reading this tells you what was decided and on
what evidence, before you see any of it applied.

### 2. `src/forecast.py` — where the residual defect lives

The only module whose `VALIDATION STATUS` admits an open problem.
`affordable_now` sits 14 points high because capacity is over-stated for some
users. Three global levers were measured and rejected; two rows remain
unexplained. If you want to find something wrong, start here.

### 3. `src/capacity.py` — where it is provably right

The contrast with (2). `amount_safe_to_pay` is closed-form — one subtraction
off the suffix minimum — and a test shows one cent more breaks the floor.
`earliest_date_for_full_payment` is an exact forward scan, **not** a closed
form; it is correct because the suffix minimum is monotone in the start
date, so the first date clearing the floor is provably the earliest. Reading these two together shows the boundary between
"this is exact" and "this is the open problem", which is the shape of the whole
project.

### 4. `evaluation/calibration_log.md` — **read the FINAL SCORECARD at the foot first**

The body is dated history with as-of markers; the scorecard is the only place
current numbers live. Then skim `P11-S1` through `P11-S5` for how they moved.

### 5. `tests/` — the claims, and which are only observations

Test names state claims. Any whose first docstring line reads
`PINNED MEASUREMENT` asserts an observed number about *this* dataset, not a
behavioural guarantee — they are expected to fail and be updated when data
changes, not loosened.

---

## The three interesting decisions

**Safety is a property of troughs, not payment days** —
`src/forecast.py::troughs`, `src/capacity.py`. A payment lowers every later
balance equally, so the binding constraint is the lowest point in the remaining
horizon. `tests/test_forecast.py::test_comfortable_on_payment_day_but_trough_fails`
builds a payment leaving three times the minimum on the day, which still breaks
the floor 70 days later.

**The task looks like an LLM problem and mostly is not** — `src/patterns.py`,
`src/render.py`. Template matching covers 100% of 215 messages and explanations
are deterministic fills, so per-request model cost is zero. The fallback is
implemented and tested but never fires. See
[`worked_example.md`](worked_example.md) for the one model-touched step.

**Two metrics disagreed and the noisy one lost** —
`config.SERIES_STALENESS_PERIODS`, logged as P11-S2. Exact-match count peaked at
one setting and continuous error at the opposite one; MARE moved monotonically
by 45% (as swept at P11-S2; 43% re-swept after the P11-S4 income fix) while
exact-count spread over 1–3 rows out of 25. The continuous metric
decided, and `request_10`'s 13.5× overstatement disappeared.

---

## Things that look like defects and are not

Each is flagged at its own site, but so you are not surprised:

* The second spending-change gate layer excludes nothing on this data — applied
  as specified anyway, with a test pinning the redundancy.
* `FX_DATE_KEY` and `QUANTIZE_AT` change no output at all — measured inert in
  P11-S5, retained because another dataset would separate them.
* The LLM fallback never runs — that is the headline efficiency result, not
  dead code.
* `request_11`'s labelled spending change targets a **non-recurring** series,
  contradicting the rulebook's own rule. Reported, not resolved.
* One cadence snaps 60 days to 30 on `request_06` — it over-projects, which is
  the safe direction, so it is left as measured rather than tuned.
