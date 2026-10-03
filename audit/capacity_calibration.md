# Capacity calibration against the 25 labelled samples

`src/capacity.py` is exact **given the forecast it is handed**: it returns the headroom at the binding trough, and the tests prove a cent more breaks the floor. Any error below is therefore an error in the *forecast*, not in the capacity solve.

## Accuracy

- exact: **2/25**
- within 1%: **3/25**
- within 5%: **6/25**
- within 20%: **11/25**
- `earliest_date_for_full_payment` exact: **11/25**

- overstating capacity: **19/25**
- understating capacity: **4/25**

## Per-row

| request | requested | ground truth | computed | error | dir | gt earliest | computed earliest | gt status | binding trough |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |
| `request_05` | 15488 | 737 | 15488.00 | 2001.5% | over | - | 2025-11-06 | not_affordable | 2025-11-13 bal 43427.72 (head 30327.72) |
| `request_10` | 266700 | 12700 | 171192.13 | 1248.0% | over | - | - | not_affordable | 2025-03-03 bal 396592.13 (head 171192.13) |
| `request_20` | 303700 | 5400 | 17626.43 | 226.4% | over | - | - | not_affordable | 2026-02-13 bal 82126.43 (head 17626.43) |
| `request_25` | 60496000 | 1425000 | 3311337.28 | 132.4% | over | - | 2024-05-15 | not_affordable | 2024-03-14 bal 26690437.28 (head 3311337.28) |
| `request_13` | 941.6 | 433.4 | 941.60 | 117.3% | over | 2024-05-15 | 2024-03-07 | affordable_later | 2024-03-14 bal 2592.45 (head 1292.45) |
| `request_14` | 5414.2 | 597.74 | 0.00 | 100.0% | under | - | - | not_affordable | 2025-11-02 bal -1375.41 (head -3575.41) |
| `request_15` | 3685 | 83.05 | 0.00 | 100.0% | under | - | - | not_affordable | 2026-04-05 bal 576.27 (head -623.73) |
| `request_18` | 3246.1 | 462 | 797.59 | 72.6% | over | 2026-09-15 | 2026-08-15 | affordable_later | 2026-07-11 bal 2197.59 (head 797.59) |
| `request_08` | 996.6 | 284.57 | 473.95 | 66.5% | over | 2025-04-15 | 2025-02-15 | affordable_later | 2025-02-12 bal 1273.95 (head 473.95) |
| `request_04` | 12693000 | 8401800 | 12314052.57 | 46.6% | over | 2024-06-15 | 2024-06-15 | affordable_later | 2024-06-13 bal 43000652.57 (head 12314052.57) |
| `request_19` | 39660 | 28820 | 39660.00 | 37.6% | over | 2024-09-15 | 2024-09-04 | affordable_with_plan | 2024-09-14 bal 166851.96 (head 74051.96) |
| `request_09` | 166.61 | 166.61 | 117.75 | 29.3% | under | 2026-07-04 | - | affordable_now | 2026-10-02 bal 717.75 (head 117.75) |
| `request_03` | 5491000 | 873000 | 1123359.34 | 28.7% | over | 2019-11-15 | 2019-11-15 | affordable_later | 2019-09-14 bal 3792059.34 (head 1123359.34) |
| `request_01` | 25256 | 25256 | 18408.93 | 27.1% | under | 2024-03-03 | - | affordable_now | 2024-05-19 bal 36408.93 (head 18408.93) |
| `request_24` | 109600 | 13420 | 15894.82 | 18.4% | over | - | - | not_affordable | 2026-01-13 bal 66894.82 (head 15894.82) |
| `request_07` | 197400 | 87170.56 | 102240.88 | 17.3% | over | 2024-10-23 | 2024-09-15 | affordable_with_plan | 2024-09-13 bal 195240.88 (head 102240.88) |
| `request_02` | 46018000 | 17229139.2 | 20083593.26 | 16.6% | over | 2025-09-15 | 2025-08-15 | affordable_with_plan | 2025-08-13 bal 49241993.26 (head 20083593.26) |
| `request_23` | 38016 | 9152 | 10275.22 | 12.3% | over | 2025-07-15 | 2025-07-15 | affordable_later | 2025-05-14 bal 37275.22 (head 10275.22) |
| `request_22` | 731.5 | 475.46 | 529.94 | 11.5% | over | 2025-01-15 | 2024-12-15 | affordable_with_plan | 2024-12-14 bal 1029.94 (head 529.94) |
| `request_11` | 13110000 | 12510645 | 13110000.00 | 4.8% | over | 2025-07-15 | 2025-05-03 | affordable_with_plan | 2025-05-14 bal 49213828.09 (head 15073228.09) |
| `request_06` | 620.4 | 603.3 | 620.40 | 2.8% | over | 2026-01-15 | 2026-01-03 | affordable_with_plan | 2026-01-13 bal 1795.81 (head 995.81) |
| `request_21` | 1574.4 | 1543.35 | 1574.40 | 2.0% | over | 2026-04-15 | 2026-04-03 | affordable_with_plan | 2026-04-12 bal 3482.52 (head 1682.52) |
| `request_17` | 274600 | 243849.58 | 244718.83 | 0.4% | over | 2026-03-15 | 2026-03-15 | affordable_with_plan | 2026-03-14 bal 410818.83 (head 244718.83) |
| `request_12` | 65164 | 65164 | 65164.00 | 0.0% | exact | 2026-04-05 | 2026-04-05 | affordable_with_plan | 2026-07-01 bal 123038.68 (head 79838.68) |
| `request_16` | 122500 | 122500 | 122500.00 | 0.0% | exact | 2023-08-12 | 2023-08-12 | affordable_now | 2023-09-13 bal 289017.24 (head 166617.24) |

## Reading the trace

`audit/capacity_trace.jsonl` holds one record per request (all 275). For a missed row, compare:

- **`binding_trough_date`** -- if this is not the date the ground truth implies, the *shape* of the forecast is wrong (a series projected that should not be, or dropped that should not be).
- **`binding_trough_balance`** -- if the date is right but the balance is wrong, the *amounts* are wrong (series amount estimator, or a missing or double-counted flow).
