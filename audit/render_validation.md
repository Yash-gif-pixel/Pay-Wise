# Formatting validation against the 25 labelled rows

Rendered from **ground-truth values**, so this measures formatting only and is unaffected by the forecast bias in C3e.

- field checks run: **100**
- character-for-character matches: **100**
- mismatches: **0**

## Rule coverage in the samples

| rule | rows exercising it | verified |
| --- | ---: | --- |
| amount_safe_to_pay with a stripped trailing zero | **0** | **NO SAMPLE EXERCISES THIS** |
| amount_safe_to_pay fractional | 11 | yes (`request_02`) |
| amount_safe_to_pay whole | 14 | yes (`request_01`) |
| payment_plan amount keeping a trailing zero | 5 | yes (`request_06`) |
| payment_plan whole-number amount | 8 | yes (`request_01`) |
| payment_plan multi-payment | 6 | yes (`request_02`) |
| wait row carries a dated plan | 6 | yes (`request_03`) |
| not_recommended: plan none, earliest blank | 7 | yes (`request_05`) |
| affordable_now: earliest == request_date | 3 | yes (`request_01`) |
| spending_changes non-empty | 3 | yes (`request_06`) |
| reduce_to amount with decimals | 1 | yes (`request_21`) |
| spending_changes multiple | 1 | yes (`request_21`) |
| installments amounts copied verbatim | 5 | yes (`request_02`) |
| partial two-payment plan | 1 | yes (`request_19`) |
| prose thousands separators | 23 | yes (`request_01`) |

### Unverified rules (1)

No labelled row exercises these, so they rest on the spec alone:

- amount_safe_to_pay with a stripped trailing zero

## Template inventory

| template | rows | exact | closest divergence |
| --- | ---: | ---: | --- |
| `affordable_now` | 3 | 2/3 | `request_09` |
| `installments` | 5 | 5/5 | - |
| `wait` | 6 | 5/6 | `request_04` |
| `full+changes` | 3 | 3/3 | - |
| `partial` | 1 | 1/1 | - |
| `not_affordable_A` | 5 | 5/5 | - |
| `not_affordable_B` | 2 | 2/2 | - |

### Rows whose label differs from the filled template

**`request_09`**

```
GT: Pay EUR 166.61 today. This keeps the EUR 600 minimum available over the next 90 days.
  MINE: Pay EUR 166.61 today. This leaves at least EUR 600 available over the next 90 days.
```

**`request_04`**

```
GT: Wait until 15 June 2024, then pay IDR 12,693,000 in full. Paying sooner would put the IDR 30,686,600 minimum at risk.
  MINE: Pay IDR 12,693,000 in full on 15 June 2024. Paying earlier would take the balance below the IDR 30,686,600 minimum.
```
