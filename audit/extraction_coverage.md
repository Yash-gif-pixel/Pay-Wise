# Deterministic extraction coverage

- messages: **215**
- classified by at least one template: **215**
- unmatched: **0**
- **coverage: 100.0%** (gate: 85%)

Gate **PASSED** -- proceed to the LLM fallback.

## Hits per template

| template | kind | messages | note |
| --- | --- | ---: | --- |
| `prize_fee_demand` | `scam_prize_fee` | 2 | demands an up-front fee to release a prize; instruction, not fact |
| `salary_increase` | `salary_amount_change` | 9 | recurring salary rises to a stated amount from a stated date |
| `salary_reduced_next_payroll` | `salary_reduced` | 10 | next payroll only is reduced, typically for unpaid leave |
| `salary_temporary_amount` | `salary_reduced` | 10 | temporarily reduced pay continues for the next payroll |
| `salary_date_change` | `salary_date_change` | 7 | payday moves; replaces the date in an earlier update |
| `salary_resumes` | `salary_resumes` | 8 | salary returns to its normal amount on a stated date |
| `confirmed_base_salary` | `confirmed_base_salary` | 9 | base pay is confirmed; any commission shown alongside is not |
| `regular_salary_confirmed_no_amount` | `confirmed_base_salary` | 1 | regular pay confirmed without restating the amount |
| `regular_salary_next_payroll` | `confirmed_base_salary` | 8 | regular pay for the next cycle is confirmed |
| `remaining_salary_after_ending` | `income_ending` | 7 | one income stream ended; a smaller confirmed salary remains |
| `household_record_ended` | `income_ending` | 7 | one of several income records stops |
| `seasonal_contract_ended` | `income_ending` | 9 | seasonal income stops with no confirmed renewal |
| `employment_ended` | `employment_ended` | 4 | all salary stops; nothing scheduled after final settlement |
| `first_salary_with_credit_date` | `confirmed_future_income` | 27 | first salary from a new employer, confirmed for a date |
| `salary_confirmed_for_date` | `confirmed_future_income` | 6 | a specific salary amount is confirmed for a specific date |
| `confirmed_credit_date` | `confirmed_future_income` | 21 | credit date for an already-stated amount |
| `employer_confirmed_salary_credit_long_date` | `confirmed_future_income` | 1 | confirmed salary credit with the date spelled out rather than ISO |
| `invoice_payment_approved` | `confirmed_invoice_payment` | 15 | one approved invoice; other submitted invoices stay excluded |
| `invoice_settlement_date` | `confirmed_invoice_payment` | 15 | settlement date for the approved invoice |
| `pending_bonus` | `pending_bonus` | 8 | bonus not approved; excluded until it settles |
| `pending_commission` | `pending_commission` | 9 | commission unearned; excluded |
| `pending_payout` | `pending_payout` | 8 | gig-platform payout not closed; not withdrawable |
| `pending_refund` | `pending_refund` | 13 | refund not credited; excluded until it settles |
| `pending_prize_claim` | `pending_prize` | 4 | prize claim unpaid; excluded |
| `prize_proceeds_settled` | `prize_settled` | 6 | prize already settled and inside the balance; no repeat |
| `one_off_arrears` | `one_off_arrears` | 8 | one-off top-up; must not be treated as recurring income |
| `claim_closed` | `claim_closed` | 5 | no further payments from this claim |
| `investment_valuation_only` | `investment_valuation_only` | 7 | paper gain or loss only; not spendable cash |
| `investment_sale_settled` | `investment_sale_settled` | 3 | sale proceeds already in the cash account |
| `reimbursement_settled` | `reimbursement_settled` | 3 | reimbursement of an earlier work expense, not new income |
| `rent_increase_percent` | `rent_increase` | 7 | monthly rent rises by a percentage from the next payment |
| `rent_increase_amount` | `rent_increase` | 0 | monthly rent rises to a stated amount |
| `new_recurring_expense` | `new_recurring_expense` | 8 | a new recurring outflow starts in the same month |
| `failed_debit_retry` | `failed_debit_retry` | 4 | a failed debit will be retried; the bill is still owed |
| `disputed_charge` | `disputed_charge` | 6 | disputed debit stands until a reversal posts |
| `payment_receipt` | `payment_receipt` | 3 | receipt confirming an already-settled payment |
| `internal_transfer` | `internal_transfer` | 6 | self-transfer; nets to zero and must not double-count |
| `fx_settlement_note` | `fx_settlement_note` | 16 | restates the fixed settlement-date conversion rule; no new fact |
| `separate_card_minimums` | `separate_card_minimums` | 2 | two card minimums are owed separately; one payment clears only one |
| `cancellation` | `cancellation` | 9 | a previously expected movement will not happen |

## Hits per fact kind

| kind | messages |
| --- | ---: |
| `confirmed_future_income` | 34 |
| `salary_reduced` | 20 |
| `confirmed_base_salary` | 18 |
| `fx_settlement_note` | 16 |
| `income_ending` | 16 |
| `confirmed_invoice_payment` | 15 |
| `pending_refund` | 13 |
| `cancellation` | 9 |
| `pending_commission` | 9 |
| `salary_amount_change` | 9 |
| `new_recurring_expense` | 8 |
| `one_off_arrears` | 8 |
| `pending_bonus` | 8 |
| `pending_payout` | 8 |
| `salary_resumes` | 8 |
| `investment_valuation_only` | 7 |
| `rent_increase` | 7 |
| `salary_date_change` | 7 |
| `disputed_charge` | 6 |
| `internal_transfer` | 6 |
| `prize_settled` | 6 |
| `claim_closed` | 5 |
| `employment_ended` | 4 |
| `failed_debit_retry` | 4 |
| `pending_prize` | 4 |
| `investment_sale_settled` | 3 |
| `payment_receipt` | 3 |
| `reimbursement_settled` | 3 |
| `scam_prize_fee` | 2 |
| `separate_card_minimums` | 2 |

## Templates that never fired (1)

These are defensive -- written from the message vocabulary but not exercised by the current dataset.

- `rent_increase_amount`

## Messages matching more than one kind (52)

Expected: a single message often carries two claims, such as a salary resuming *and* a new recurring expense starting.

- `message_08`: `confirmed_base_salary`, `pending_commission`
- `message_09`: `cancellation`, `income_ending`
- `message_10`: `new_recurring_expense`, `salary_resumes`
- `message_17`: `claim_closed`, `prize_settled`
- `message_20`: `confirmed_base_salary`, `one_off_arrears`
- `message_21`: `cancellation`, `income_ending`
- `message_27`: `confirmed_base_salary`, `one_off_arrears`
- `message_28`: `claim_closed`, `prize_settled`
- `message_45`: `cancellation`, `income_ending`
- `message_47`: `fx_settlement_note`, `pending_refund`
- `message_53`: `confirmed_future_income`, `fx_settlement_note`
- `message_58`: `confirmed_base_salary`, `pending_commission`
- `message_60`: `confirmed_base_salary`, `pending_commission`
- `message_62`: `confirmed_base_salary`, `one_off_arrears`
- `message_63`: `new_recurring_expense`, `salary_resumes`
- `message_66`: `new_recurring_expense`, `salary_resumes`
- `message_70`: `confirmed_base_salary`, `pending_commission`
- `message_74`: `confirmed_future_income`, `fx_settlement_note`
- `message_78`: `confirmed_base_salary`, `pending_commission`
- `message_82`: `confirmed_base_salary`, `pending_commission`
- `message_86`: `confirmed_future_income`, `fx_settlement_note`, `payment_receipt`
- `message_88`: `claim_closed`, `prize_settled`
- `message_90`: `confirmed_base_salary`, `one_off_arrears`
- `message_91`: `new_recurring_expense`, `salary_resumes`
- `message_95`: `confirmed_future_income`, `fx_settlement_note`
- `message_97`: `new_recurring_expense`, `salary_resumes`
- `message_99`: `claim_closed`, `prize_settled`
- `message_103`: `cancellation`, `income_ending`
- `message_110`: `claim_closed`, `prize_settled`
- `message_112`: `confirmed_base_salary`, `one_off_arrears`
- `message_113`: `new_recurring_expense`, `salary_resumes`
- `message_120`: `new_recurring_expense`, `salary_resumes`
- `message_127`: `confirmed_base_salary`, `one_off_arrears`
- `message_128`: `confirmed_base_salary`, `pending_commission`
- `message_133`: `fx_settlement_note`, `pending_refund`
- `message_137`: `confirmed_future_income`, `fx_settlement_note`
- `message_139`: `confirmed_base_salary`, `pending_commission`
- `message_146`: `fx_settlement_note`, `pending_refund`
- `message_160`: `cancellation`, `income_ending`
- `message_166`: `cancellation`, `income_ending`
- ... and 12 more

## Unmatched messages (0)

None.
## Source-type / language split

| source_type | english | indonesian |
| --- | ---: | ---: |
| bank | 10 | 8 |
| employer | 75 | 51 |
| financial_service | 4 | 19 |
| merchant | 7 | 10 |
| service_provider | 15 | 16 |
