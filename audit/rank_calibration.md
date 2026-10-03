# Ranking calibration against the 25 labelled samples

## The three numbers

| metric | result | what it diagnoses |
| --- | ---: | --- |
| method exact | **20/25** | dominated by forecast bias (C3e), not ranking |
| status exact | **18/25** | same; status follows the winning method |
| method exact **given generated** | **20/20** | **the only figure that measures ranking** |

Ranking picks the labelled method **every time it was offered one**. No ranking defect is visible.

## Which criterion decided each row

| criterion | requests (all 275) |
| --- | ---: |
| `only_eligible_candidate` | 199 |
| `no_eligible_candidate` | 71 |
| `minimum_total_amount_paid` | 4 |
| `requires_no_spending_changes` | 1 |

Criterion-5 (`lowest_payment_option_id`) tie-breaks: **0**

No candidate pair survived all five criteria, so ranking was total.

## Per-row

| request | gt method | mine | gt status | mine | available | cands | decided by |
| --- | --- | --- | --- | --- | :-: | ---: | --- |
| `request_01` | full_payment | not_recommended **X** | affordable_now | not_affordable | no | 0 | `no_eligible_candidate` |
| `request_02` | installments | installments OK | affordable_with_plan | affordable_with_plan | yes | 1 | `only_eligible_candidate` |
| `request_03` | wait | wait OK | affordable_later | affordable_later | yes | 1 | `only_eligible_candidate` |
| `request_04` | wait | wait OK | affordable_later | affordable_later | yes | 2 | `requires_no_spending_changes` |
| `request_05` | not_recommended | full_payment **X** | not_affordable | affordable_now | no | 1 | `only_eligible_candidate` |
| `request_06` | full_payment | full_payment OK | affordable_with_plan | affordable_now | yes | 1 | `only_eligible_candidate` |
| `request_07` | installments | installments OK | affordable_with_plan | affordable_with_plan | yes | 1 | `only_eligible_candidate` |
| `request_08` | wait | wait OK | affordable_later | affordable_later | yes | 1 | `only_eligible_candidate` |
| `request_09` | full_payment | not_recommended **X** | affordable_now | not_affordable | no | 0 | `no_eligible_candidate` |
| `request_10` | not_recommended | not_recommended OK | not_affordable | not_affordable | yes | 0 | `no_eligible_candidate` |
| `request_11` | full_payment | full_payment OK | affordable_with_plan | affordable_now | yes | 1 | `only_eligible_candidate` |
| `request_12` | installments | installments OK | affordable_with_plan | affordable_with_plan | yes | 1 | `only_eligible_candidate` |
| `request_13` | wait | full_payment **X** | affordable_later | affordable_now | no | 1 | `only_eligible_candidate` |
| `request_14` | not_recommended | not_recommended OK | not_affordable | not_affordable | yes | 0 | `no_eligible_candidate` |
| `request_15` | not_recommended | not_recommended OK | not_affordable | not_affordable | yes | 0 | `no_eligible_candidate` |
| `request_16` | full_payment | full_payment OK | affordable_now | affordable_now | yes | 1 | `only_eligible_candidate` |
| `request_17` | installments | installments OK | affordable_with_plan | affordable_with_plan | yes | 1 | `only_eligible_candidate` |
| `request_18` | wait | wait OK | affordable_later | affordable_later | yes | 1 | `only_eligible_candidate` |
| `request_19` | partial_payment | installments **X** | affordable_with_plan | affordable_with_plan | no | 1 | `only_eligible_candidate` |
| `request_20` | not_recommended | not_recommended OK | not_affordable | not_affordable | yes | 0 | `no_eligible_candidate` |
| `request_21` | full_payment | full_payment OK | affordable_with_plan | affordable_now | yes | 1 | `only_eligible_candidate` |
| `request_22` | installments | installments OK | affordable_with_plan | affordable_with_plan | yes | 1 | `only_eligible_candidate` |
| `request_23` | wait | wait OK | affordable_later | affordable_later | yes | 1 | `only_eligible_candidate` |
| `request_24` | not_recommended | not_recommended OK | not_affordable | not_affordable | yes | 0 | `no_eligible_candidate` |
| `request_25` | not_recommended | not_recommended OK | not_affordable | not_affordable | yes | 0 | `no_eligible_candidate` |
