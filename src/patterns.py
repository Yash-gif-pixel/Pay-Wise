"""Template patterns for the deterministic message extractor.

Every message in ``messages.csv`` is built the same way::

    <opener naming a company>  <core claim>  [<core claim>]  <closer>  <ref>

Only the core claims carry financial meaning. Openers, closers and reference
codes are boilerplate that varies with the company name, so the patterns here
deliberately match the *claim* sentences and ignore everything else.

Each template is declared once with its English and Indonesian variants.
Keeping them as data rather than code means the coverage report can name the
exact template that fired, and an unmatched message is visible rather than
silently mis-parsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Pattern

MONEY = r"(?P<currency>INR|IDR|ZAR|USD|EUR)\s*(?P<amount>[\d,]+(?:\.\d+)?)"
MONEY2 = r"(?P<currency2>INR|IDR|ZAR|USD|EUR)\s*(?P<amount2>[\d,]+(?:\.\d+)?)"
ISO_DATE = r"(?P<date>\d{4}-\d{2}-\d{2})"
PERCENT = r"(?P<percent>\d+(?:\.\d+)?)\s*%"

#: Month names used by the handful of messages that spell a date out.
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
LONG_DATE = (
    r"(?P<day>\d{1,2})\s+(?P<month>" + "|".join(MONTH_NAMES) + r")\s+(?P<year>\d{4})"
)

#: Curly and straight apostrophes both appear in the source.
APOS = r"['‘’]"


class FactKind(StrEnum):
    """What a message asserts about the user's money."""

    # -- income, counted -------------------------------------------------
    SALARY_AMOUNT_CHANGE = "salary_amount_change"
    SALARY_REDUCED = "salary_reduced"
    SALARY_DATE_CHANGE = "salary_date_change"
    SALARY_RESUMES = "salary_resumes"
    CONFIRMED_FUTURE_INCOME = "confirmed_future_income"
    CONFIRMED_BASE_SALARY = "confirmed_base_salary"
    CONFIRMED_INVOICE_PAYMENT = "confirmed_invoice_payment"
    INVESTMENT_SALE_SETTLED = "investment_sale_settled"
    REIMBURSEMENT_SETTLED = "reimbursement_settled"

    # -- income, ending or excluded --------------------------------------
    INCOME_ENDING = "income_ending"
    EMPLOYMENT_ENDED = "employment_ended"
    PENDING_BONUS = "pending_bonus"
    PENDING_COMMISSION = "pending_commission"
    PENDING_PAYOUT = "pending_payout"
    PENDING_REFUND = "pending_refund"
    PENDING_PRIZE = "pending_prize"
    ONE_OFF_ARREARS = "one_off_arrears"
    PRIZE_SETTLED = "prize_settled"
    CLAIM_CLOSED = "claim_closed"
    INVESTMENT_VALUATION_ONLY = "investment_valuation_only"

    # -- expenses ---------------------------------------------------------
    RENT_INCREASE = "rent_increase"
    NEW_RECURRING_EXPENSE = "new_recurring_expense"
    FAILED_DEBIT_RETRY = "failed_debit_retry"
    DISPUTED_CHARGE = "disputed_charge"
    PAYMENT_RECEIPT = "payment_receipt"

    # -- bookkeeping ------------------------------------------------------
    INTERNAL_TRANSFER = "internal_transfer"
    FX_SETTLEMENT_NOTE = "fx_settlement_note"
    CANCELLATION = "cancellation"
    SEPARATE_CARD_MINIMUMS = "separate_card_minimums"

    # -- adversarial ------------------------------------------------------
    SCAM_PRIZE_FEE = "scam_prize_fee"

    #: Nothing recognised. Handed to the LLM fallback.
    UNKNOWN = "unknown"


#: Kinds whose amount must never be added to the balance. Pending inbound
#: money, paper gains, and anything a scam message claims.
NON_CASH_KINDS = frozenset(
    {
        FactKind.PENDING_BONUS,
        FactKind.PENDING_COMMISSION,
        FactKind.PENDING_PAYOUT,
        FactKind.PENDING_REFUND,
        FactKind.PENDING_PRIZE,
        FactKind.INVESTMENT_VALUATION_ONLY,
        FactKind.SCAM_PRIZE_FEE,
        FactKind.ONE_OFF_ARREARS,
        FactKind.INTERNAL_TRANSFER,
        FactKind.FX_SETTLEMENT_NOTE,
        FactKind.CLAIM_CLOSED,
        FactKind.PRIZE_SETTLED,
        FactKind.PAYMENT_RECEIPT,
        FactKind.DISPUTED_CHARGE,
        FactKind.SEPARATE_CARD_MINIMUMS,
        FactKind.REIMBURSEMENT_SETTLED,
        FactKind.INVESTMENT_SALE_SETTLED,
    }
)

#: Kinds that change recurring income going forward.
INCOME_AMENDMENT_KINDS = frozenset(
    {
        FactKind.SALARY_AMOUNT_CHANGE,
        FactKind.SALARY_REDUCED,
        FactKind.SALARY_RESUMES,
        FactKind.CONFIRMED_BASE_SALARY,
        FactKind.INCOME_ENDING,
        FactKind.EMPLOYMENT_ENDED,
    }
)


@dataclass(frozen=True)
class Template:
    """One recognisable claim, with its language variants."""

    name: str
    kind: FactKind
    regexes: tuple[Pattern[str], ...]
    #: Human-readable note used in the coverage report and explanations.
    note: str

    def search(self, text: str) -> re.Match[str] | None:
        for regex in self.regexes:
            match = regex.search(text)
            if match:
                return match
        return None


def _c(*sources: str) -> tuple[Pattern[str], ...]:
    return tuple(re.compile(s, re.IGNORECASE | re.UNICODE) for s in sources)


# ---------------------------------------------------------------------------
# The template table
#
# Order matters only for reporting; every template is tried against every
# message so a single message can yield several facts (a salary resuming *and*
# a new recurring expense beginning, for instance).
# ---------------------------------------------------------------------------

TEMPLATES: tuple[Template, ...] = (
    # -- adversarial first, so a scam is never read as ordinary income ----
    Template(
        name="prize_fee_demand",
        kind=FactKind.SCAM_PRIZE_FEE,
        regexes=_c(
            r"pay the (?:release|processing|clearance|handling) charge",
            r"bayar biaya (?:pencairan|pemrosesan|pelepasan|penanganan)",
            r"you’?ve been selected for a cash prize",
            r"you have been selected for a cash prize",
            r"anda terpilih untuk menerima hadiah uang tunai",
        ),
        note="demands an up-front fee to release a prize; instruction, not fact",
    ),
    # -- salary changes ---------------------------------------------------
    Template(
        name="salary_increase",
        kind=FactKind.SALARY_AMOUNT_CHANGE,
        regexes=_c(
            rf"monthly salary has increased to {MONEY}",
            rf"gaji bulanan anda naik menjadi {MONEY}",
        ),
        note="recurring salary rises to a stated amount from a stated date",
    ),
    Template(
        name="salary_reduced_next_payroll",
        kind=FactKind.SALARY_REDUCED,
        regexes=_c(
            rf"next salary is reduced to {MONEY}",
            rf"gaji berikutnya (?:anda )?(?:di)?kurangi menjadi {MONEY}",
            rf"gaji anda berikutnya dikurangi menjadi {MONEY}",
        ),
        note="next payroll only is reduced, typically for unpaid leave",
    ),
    Template(
        name="salary_temporary_amount",
        kind=FactKind.SALARY_REDUCED,
        regexes=_c(
            rf"temporary monthly pay is {MONEY}",
            rf"gaji bulanan sementara anda adalah {MONEY}",
        ),
        note="temporarily reduced pay continues for the next payroll",
    ),
    Template(
        name="salary_date_change",
        kind=FactKind.SALARY_DATE_CHANGE,
        regexes=_c(
            rf"confirmed salary is now expected on {ISO_DATE}",
            rf"gaji yang sudah dikonfirmasi kini diperkirakan masuk pada {ISO_DATE}",
        ),
        note="payday moves; replaces the date in an earlier update",
    ),
    Template(
        name="salary_resumes",
        kind=FactKind.SALARY_RESUMES,
        regexes=_c(
            rf"regular salary of {MONEY} resumes on {ISO_DATE}",
            rf"gaji rutin sebesar {MONEY} (?:akan )?dilanjutkan (?:pada|mulai) {ISO_DATE}",
            rf"gaji rutin sebesar {MONEY} kembali (?:pada|mulai) {ISO_DATE}",
        ),
        note="salary returns to its normal amount on a stated date",
    ),
    Template(
        name="confirmed_base_salary",
        kind=FactKind.CONFIRMED_BASE_SALARY,
        regexes=_c(
            rf"confirmed base salary is {MONEY}",
            rf"gaji pokok yang dikonfirmasi adalah {MONEY}",
        ),
        note="base pay is confirmed; any commission shown alongside is not",
    ),
    Template(
        name="regular_salary_confirmed_no_amount",
        kind=FactKind.CONFIRMED_BASE_SALARY,
        regexes=_c(
            r"regular salary for the next payroll is confirmed",
            r"gaji rutin untuk penggajian berikutnya sudah dikonfirmasi",
        ),
        note="regular pay confirmed without restating the amount",
    ),
    Template(
        name="regular_salary_next_payroll",
        kind=FactKind.CONFIRMED_BASE_SALARY,
        regexes=_c(
            rf"regular salary for the next payroll is {MONEY}",
            rf"gaji rutin anda untuk penggajian berikutnya adalah {MONEY}",
        ),
        note="regular pay for the next cycle is confirmed",
    ),
    Template(
        name="remaining_salary_after_ending",
        kind=FactKind.INCOME_ENDING,
        regexes=_c(
            rf"remaining confirmed monthly salary is {MONEY}",
            rf"sisa gaji bulanan yang dikonfirmasi adalah {MONEY}",
        ),
        note="one income stream ended; a smaller confirmed salary remains",
    ),
    Template(
        name="household_record_ended",
        kind=FactKind.INCOME_ENDING,
        regexes=_c(
            r"one household employment record has ended",
            r"salah satu sumber pendapatan kerja rumah tangga telah berakhir",
        ),
        note="one of several income records stops",
    ),
    Template(
        name="seasonal_contract_ended",
        kind=FactKind.INCOME_ENDING,
        regexes=_c(
            r"current seasonal contract has ended",
            r"kontrak musiman saat ini telah berakhir",
        ),
        note="seasonal income stops with no confirmed renewal",
    ),
    Template(
        name="employment_ended",
        kind=FactKind.EMPLOYMENT_ENDED,
        regexes=_c(
            r"your employment has ended",
            r"hubungan kerja anda telah berakhir",
            r"no regular salary payments scheduled after the final settlement",
            r"tidak ada pembayaran gaji rutin yang dijadwalkan setelah penyelesaian akhir",
        ),
        note="all salary stops; nothing scheduled after final settlement",
    ),
    # -- confirmed future income -----------------------------------------
    Template(
        name="first_salary_with_credit_date",
        kind=FactKind.CONFIRMED_FUTURE_INCOME,
        regexes=_c(
            rf"first salary will be {MONEY}",
            rf"first salary from the new employer is {MONEY}",
            rf"first salary of {MONEY} is scheduled for {ISO_DATE}",
            rf"gaji pertama anda (?:adalah|sebesar) {MONEY}",
            rf"gaji pertama dari perusahaan baru adalah {MONEY}",
            rf"gaji pertama anda sebesar {MONEY} dijadwalkan pada {ISO_DATE}",
        ),
        note="first salary from a new employer, confirmed for a date",
    ),
    Template(
        name="salary_confirmed_for_date",
        kind=FactKind.CONFIRMED_FUTURE_INCOME,
        regexes=_c(
            rf"salary of {MONEY} is confirmed for {ISO_DATE}",
            rf"gaji sebesar {MONEY} dikonfirmasi untuk {ISO_DATE}",
        ),
        note="a specific salary amount is confirmed for a specific date",
    ),
    Template(
        name="confirmed_credit_date",
        kind=FactKind.CONFIRMED_FUTURE_INCOME,
        regexes=_c(
            rf"confirmed credit date is {ISO_DATE}",
            rf"tanggal kredit yang dikonfirmasi adalah {ISO_DATE}",
            rf"pembayaran sudah dikonfirmasi untuk {ISO_DATE}",
            rf"it is confirmed for {ISO_DATE}",
        ),
        note="credit date for an already-stated amount",
    ),
    Template(
        name="employer_confirmed_salary_credit_long_date",
        kind=FactKind.CONFIRMED_FUTURE_INCOME,
        regexes=_c(
            rf"confirmed an? {MONEY} salary credit for {LONG_DATE}",
            rf"mengonfirmasi kredit gaji {MONEY} untuk {LONG_DATE}",
        ),
        note="confirmed salary credit with the date spelled out rather than ISO",
    ),
    Template(
        name="invoice_payment_approved",
        kind=FactKind.CONFIRMED_INVOICE_PAYMENT,
        regexes=_c(
            rf"client approved an invoice payment of {MONEY}",
            rf"klien menyetujui pembayaran faktur sebesar {MONEY}",
        ),
        note="one approved invoice; other submitted invoices stay excluded",
    ),
    Template(
        name="invoice_settlement_date",
        kind=FactKind.CONFIRMED_INVOICE_PAYMENT,
        regexes=_c(
            rf"settlement is expected on {ISO_DATE}",
            rf"penyelesaian diperkirakan pada {ISO_DATE}",
        ),
        note="settlement date for the approved invoice",
    ),
    # -- excluded inbound money ------------------------------------------
    Template(
        name="pending_bonus",
        kind=FactKind.PENDING_BONUS,
        regexes=_c(
            r"quarterly bonus is still subject to the final performance review",
            r"bonus kuartalan anda masih menunggu hasil akhir penilaian kinerja",
            r"final amount and payment date have not been approved",
            r"jumlah akhir dan tanggal pembayaran belum disetujui",
        ),
        note="bonus not approved; excluded until it settles",
    ),
    Template(
        name="pending_commission",
        kind=FactKind.PENDING_COMMISSION,
        regexes=_c(
            r"commission shown for open deals is still pending approval",
            r"komisi dari transaksi yang masih berjalan belum disetujui",
            r"open deals will stay out of the payout",
            r"transaksi yang masih berjalan tidak masuk pembayaran",
        ),
        note="commission unearned; excluded",
    ),
    Template(
        name="pending_payout",
        kind=FactKind.PENDING_PAYOUT,
        regexes=_c(
            r"payout is still pending",
            r"pembayaran berikutnya dari .{1,30} masih tertunda",
            rf"balance isn{APOS}?t withdrawable until the payout shows as completed",
            r"saldo belum dapat ditarik sampai status pembayaran menunjukkan selesai",
        ),
        note="gig-platform payout not closed; not withdrawable",
    ),
    Template(
        name="pending_refund",
        kind=FactKind.PENDING_REFUND,
        regexes=_c(
            r"refund has been initiated but has not reached your account",
            r"pengembalian dana telah dimulai tetapi belum sampai ke rekening",
            r"pengembalian dana sudah diproses, tetapi belum masuk ke rekening",
            r"foreign-currency refund is still processing",
            r"pengembalian dana dalam mata uang asing masih diproses",
            r"final home-currency credit will be calculated when the refund settles",
        ),
        note="refund not credited; excluded until it settles",
    ),
    Template(
        name="pending_prize_claim",
        kind=FactKind.PENDING_PRIZE,
        regexes=_c(
            r"prize claim has been verified and is still in payment processing",
            r"klaim hadiah anda (?:telah|sudah) diverifikasi dan masih dalam proses pembayaran",
            r"payment has not been credited to your account yet",
            r"pembayaran tersebut belum masuk ke rekening anda",
        ),
        note="prize claim unpaid; excluded",
    ),
    Template(
        name="prize_proceeds_settled",
        kind=FactKind.PRIZE_SETTLED,
        regexes=_c(
            r"prize proceeds have reached your account after withholding",
            r"dana hadiah telah masuk ke rekening anda setelah pemotongan",
            rf"there won{APOS}?t be another payment unless a separate prize is confirmed",
        ),
        note="prize already settled and inside the balance; no repeat",
    ),
    Template(
        name="one_off_arrears",
        kind=FactKind.ONE_OFF_ARREARS,
        regexes=_c(
            rf"one-time arrears adjustment of {MONEY}",
            rf"penyesuaian tunggakan satu kali sebesar {MONEY}",
        ),
        note="one-off top-up; must not be treated as recurring income",
    ),
    Template(
        name="claim_closed",
        kind=FactKind.CLAIM_CLOSED,
        regexes=_c(
            r"claim is now closed and there are no further scheduled payments",
            r"klaim sekarang ditutup dan tidak ada pembayaran terjadwal lagi",
        ),
        note="no further payments from this claim",
    ),
    Template(
        name="investment_valuation_only",
        kind=FactKind.INVESTMENT_VALUATION_ONLY,
        regexes=_c(
            rf"portfolio{APOS}?s displayed market value has increased",
            r"nilai pasar portofolio anda yang ditampilkan telah meningkat",
            r"no units have been sold and no cash proceeds",
            r"tidak ada unit yang dijual dan tidak ada hasil tunai",
            r"displayed value will continue to move with market prices",
            r"displayed investment value has (?:fallen|dropped|declined)",
            r"nilai investasi yang ditampilkan telah turun",
            r"investasi tersebut belum dijual dan tidak ada transaksi tunai",
            r"nilai yang ditampilkan akan terus berubah mengikuti harga pasar",
        ),
        note="paper gain or loss only; not spendable cash",
    ),
    Template(
        name="investment_sale_settled",
        kind=FactKind.INVESTMENT_SALE_SETTLED,
        regexes=_c(
            r"investment sale proceeds have (?:been )?credited to your cash account",
            r"proceeds from your investment sale have settled in the cash account",
            r"hasil penjualan investasi anda sudah masuk ke rekening tunai",
            r"sale order is complete and there are no remaining proceeds pending",
        ),
        note="sale proceeds already in the cash account",
    ),
    Template(
        name="reimbursement_settled",
        kind=FactKind.REIMBURSEMENT_SETTLED,
        regexes=_c(
            r"latest company credit is a reimbursement",
            r"latest employer credit is the reimbursement for your earlier work expense",
            r"dana terbaru dari perusahaan adalah penggantian atas biaya kerja",
            r"payment is linked to an earlier work expense, not your regular salary",
        ),
        note="reimbursement of an earlier work expense, not new income",
    ),
    # -- expenses ---------------------------------------------------------
    Template(
        name="rent_increase_percent",
        kind=FactKind.RENT_INCREASE,
        regexes=_c(
            rf"renewed lease increases monthly rent by {PERCENT}",
            rf"perpanjangan sewa menaikkan (?:biaya )?sewa bulanan sebesar {PERCENT}",
            rf"sewa bulanan naik sebesar {PERCENT}",
        ),
        note="monthly rent rises by a percentage from the next payment",
    ),
    Template(
        name="rent_increase_amount",
        kind=FactKind.RENT_INCREASE,
        regexes=_c(
            rf"new monthly rent is {MONEY}",
            rf"sewa bulanan baru adalah {MONEY}",
        ),
        note="monthly rent rises to a stated amount",
    ),
    Template(
        name="new_recurring_expense",
        kind=FactKind.NEW_RECURRING_EXPENSE,
        regexes=_c(
            r"new recurring (?P<what>[a-z ]{3,30}?) payment begins",
            r"pembayaran (?P<what>[a-z ]{3,30}?) rutin baru dimulai",
            r"pembayaran rutin baru untuk (?P<what>[a-z ]{3,30}?) dimulai",
        ),
        note="a new recurring outflow starts in the same month",
    ),
    Template(
        name="failed_debit_retry",
        kind=FactKind.FAILED_DEBIT_RETRY,
        regexes=_c(
            r"previous debit attempt failed",
            r"upaya debit sebelumnya gagal",
            r"bill is still outstanding and another debit will be attempted",
            r"bill is still open and another debit may be attempted",
            r"tagihan masih terbuka dan debit lain akan dicoba",
        ),
        note="a failed debit will be retried; the bill is still owed",
    ),
    Template(
        name="disputed_charge",
        kind=FactKind.DISPUTED_CHARGE,
        regexes=_c(
            r"extra card charge is still being investigated",
            r"biaya kartu tambahan masih dalam penyelidikan",
            r"reversal has not been posted to the account yet",
            r"dana pembalikannya belum tercatat di rekening",
            r"dispute is open and no reversal has been posted",
        ),
        note="disputed debit stands until a reversal posts",
    ),
    Template(
        name="payment_receipt",
        kind=FactKind.PAYMENT_RECEIPT,
        regexes=_c(
            r"payment was received on",
            r"pembayaran .{0,40} diterima pada",
            r"confirmed that the .{1,40} order was paid",
            r"receipt has the final .{0,20}amount",
            r"receipt contains the final .{0,20}amount",
            r"wallet was charged for the session",
            r"dompet anda ditagih untuk sesi",
        ),
        note="receipt confirming an already-settled payment",
    ),
    # -- bookkeeping ------------------------------------------------------
    Template(
        name="internal_transfer",
        kind=FactKind.INTERNAL_TRANSFER,
        regexes=_c(
            r"matching debit and credit came from a transfer between your two accounts",
            r"debit dan kredit dengan jumlah yang sama berasal dari transfer antara dua rekening",
            r"both accounts are registered under the same account holder",
            r"kedua rekening terdaftar atas nama pemilik yang sama",
        ),
        note="self-transfer; nets to zero and must not double-count",
    ),
    Template(
        name="fx_settlement_note",
        kind=FactKind.FX_SETTLEMENT_NOTE,
        regexes=_c(
            r"receiving bank will convert it using the rate applied on the settlement date",
            r"bank penerima akan mengonversinya dengan kurs pada tanggal penyelesaian",
            r"home-currency credit may change with the settlement-date rate",
            r"amount received in your home currency will depend on the settlement-date conversion",
            r"jumlah yang diterima dalam mata uang utama bergantung pada kurs tanggal penyelesaian",
            r"jumlah akhir dalam mata uang utama menggunakan kurs saat transaksi selesai",
            r"bank anda akan mengonfirmasi jumlah akhir dalam mata uang utama",
            r"bill was charged in a foreign currency",
            r"tagihan dikenakan dalam mata uang asing",
            r"final home-currency amount will use the rate applied when it settles",
            r"salary will use the exchange rate when it settles",
        ),
        note="restates the fixed settlement-date conversion rule; no new fact",
    ),
    Template(
        name="separate_card_minimums",
        kind=FactKind.SEPARATE_CARD_MINIMUMS,
        regexes=_c(
            r"minimum payments due on two separate card accounts",
            r"pembayaran minimum jatuh tempo pada dua rekening kartu terpisah",
            r"payment to one card will not cover the amount due on the other",
            r"minimums belong to separate accounts",
        ),
        note="two card minimums are owed separately; one payment clears only one",
    ),
    Template(
        name="cancellation",
        kind=FactKind.CANCELLATION,
        regexes=_c(
            r"has been cancelled",
            r"telah dibatalkan",
            r"no longer scheduled",
            r"tidak lagi dijadwalkan",
            r"we[’']?ll contact you separately if another shift block or contract is approved",
            r"kami akan menghubungi anda jika jadwal kerja atau kontrak berikutnya disetujui",
        ),
        note="a previously expected movement will not happen",
    ),
)


#: Sentences that are pure boilerplate. A message consisting only of these
#: carries no financial claim and is not counted against coverage.
BOILERPLATE = _c(
    r"^(?:hi|halo|hello)[,.]",
    r"has updated your payroll record",
    r"telah memperbarui catatan penggajian",
    r"payroll has posted a new update",
    r"a (?:quick )?(?:note|update) (?:from|about)",
    r"here’?s the latest",
    r"berikut informasi",
    r"ada (?:pembaruan|informasi|pemberitahuan)",
    r"a new notice is available",
    r"pemberitahuan baru tersedia",
    r"wanted to let you know about a change",
    r"ingin memberi tahu anda tentang perubahan",
    r"has new information about",
    r"memiliki informasi baru tentang",
    r"has reviewed the transaction",
    r"telah meninjau transaksi",
    r"has shared an update",
    r"telah berbagi pembaruan",
    r"has updated the latest account activity",
    r"(?:payroll|account|order|case|txn|payment) ref\b",
    r"ref (?:payroll|akun|kasus)\b",
    r"will appear on your next payslip",
    r"akan terlihat pada slip gaji berikutnya",
    r"^congratulations[!.]?$",
    r"^selamat[!.]?$",
)


def is_boilerplate(sentence: str) -> bool:
    return any(regex.search(sentence) for regex in BOILERPLATE)
