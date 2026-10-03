# tests

Deterministic tests for the Buy or Wait? pipeline.

Run from the repository root:

```
py -m pytest tests
```

Planned coverage (phase 2+):

- `test_load.py` — date/decimal parsing, list-column splitting, blank-vs-zero,
  exchange-rate lookup by settlement date and direction
- `test_state.py` — pending-debit reservation, pending-credit exclusion,
  cancelled/failed/duplicate handling, non-cash investment exclusion
- `test_extract.py` — evidence parsing, prompt-injection resistance
  (imperative text in messages/images must not change a decision)
- `test_forecast.py` — recurrence detection, 90-day horizon, minimum-balance floor
- `test_capacity.py` — `amount_safe_to_pay` bounds, `earliest_date_for_full_payment`
- `test_candidates.py` — eligibility against `payment_methods_user_will_consider`
  and `max_installment_months`; installment schedules matching supplied options
- `test_changes.py` — flexible-only, unprotected-only, max three, stop/reduce exclusivity
- `test_rank.py` — the six-step preference chain and status mapping
- `test_render.py` — plan string format, `none`/empty conventions
- `test_validate.py` — the validator rejects each contract violation
- `test_samples.py` — scoring against the 25 solved rows in `sample_requests.csv`

NOTE ON TEST NAMES
------------------
A test name states the *claim*, not the mechanics. Tests whose first docstring
line reads "PINNED MEASUREMENT" assert an observed number about this particular
dataset rather than a behavioural guarantee: if the dataset or a calibrated
default changes, they are expected to fail and to be updated with the new
observation, not loosened.
