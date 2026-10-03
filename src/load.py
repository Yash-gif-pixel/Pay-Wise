"""Data loading and normalization.

Reads the nine CSVs under ``dataset/`` into frozen, typed dataclasses and
normalizes the representational noise away: ISO date parsing, ``Decimal``
amounts, pipe-separated profile columns split into real tuples, and
home-currency conversion against the fixed dated table in
``exchange_rates.csv``.

Money is **never** a float. Scoring compares exact values and the ground
truth engineers margins of only a few currency units, so every monetary
field is a :class:`decimal.Decimal` parsed straight from the source text.
Binary floating point would introduce representation error before any
arithmetic happened.

Loading decides nothing. A blank ``amount`` stays ``None`` -- never ``0`` --
and it is :mod:`src.extract` that resolves it from the linked image.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from . import config, paths

logger = logging.getLogger(__name__)

#: Money is stored and compared at two decimal places, matching every amount
#: in the source data (max 2dp across events, requests and payment options).
CENTS = Decimal("0.01")

#: Profile columns that hold ``|``-separated lists.
_LIST_COLUMNS = (
    "financial_priorities",
    "expense_categories_to_protect",
    "expense_categories_user_is_willing_to_reduce",
    "expense_categories_user_is_willing_to_stop",
    "payment_methods_user_will_consider",
)

_ID_SUFFIX_RE = re.compile(r"(\d+)$")


class DatasetError(Exception):
    """Base class for unrecoverable problems in the supplied dataset."""


class MissingExchangeRateError(DatasetError):
    """No exact ``(rate_date, from_currency, to_currency)`` row exists.

    Raised once at the end of loading with the complete list of gaps, so a
    dataset problem is reported in full rather than one row at a time. The
    loader never falls back to a nearby date: a rate from a different day is
    a different number, and silently substituting one would corrupt every
    downstream balance.
    """

    def __init__(self, gaps: Sequence["ExchangeRateGap"]) -> None:
        self.gaps = tuple(gaps)
        preview = "\n".join(f"    {gap}" for gap in self.gaps[:20])
        more = (
            f"\n    ... and {len(self.gaps) - 20} more"
            if len(self.gaps) > 20
            else ""
        )
        super().__init__(
            f"{len(self.gaps)} exchange-rate lookup(s) had no exact "
            f"(rate_date, from_currency, to_currency) row:\n{preview}{more}"
        )


@dataclass(frozen=True)
class ExchangeRateGap:
    """One unsatisfied rate lookup, recorded for the error report."""

    event_id: str
    user_id: str
    rate_date: date
    from_currency: str
    to_currency: str
    reason: str

    def __str__(self) -> str:
        return (
            f"{self.event_id} ({self.user_id}): {self.from_currency} -> "
            f"{self.to_currency} on {self.rate_date.isoformat()} -- {self.reason}"
        )


# --------------------------------------------------------------- parsing


def _text(value: str | None) -> str:
    return (value or "").strip()


def _optional_text(value: str | None) -> str | None:
    stripped = _text(value)
    return stripped or None


def _decimal(value: str | None, *, field: str, row_id: str) -> Decimal:
    stripped = _text(value)
    if not stripped:
        raise DatasetError(f"{row_id}: required amount {field!r} is blank")
    try:
        return Decimal(stripped)
    except ArithmeticError as exc:  # pragma: no cover - malformed source data
        raise DatasetError(f"{row_id}: cannot parse {field}={stripped!r}") from exc


def _optional_decimal(value: str | None, *, field: str, row_id: str) -> Decimal | None:
    """Parse an amount, preserving a blank as ``None``.

    A blank amount is missing information, not zero. Returning ``0`` here
    would silently drop a real expense from the forecast.
    """
    stripped = _text(value)
    if not stripped:
        return None
    try:
        return Decimal(stripped)
    except ArithmeticError as exc:  # pragma: no cover - malformed source data
        raise DatasetError(f"{row_id}: cannot parse {field}={stripped!r}") from exc


def _date(value: str | None, *, field: str, row_id: str) -> date:
    stripped = _text(value)
    if not stripped:
        raise DatasetError(f"{row_id}: required date {field!r} is blank")
    try:
        return date.fromisoformat(stripped)
    except ValueError as exc:
        raise DatasetError(f"{row_id}: cannot parse {field}={stripped!r}") from exc


def _optional_date(value: str | None, *, field: str, row_id: str) -> date | None:
    stripped = _text(value)
    if not stripped:
        return None
    try:
        return date.fromisoformat(stripped)
    except ValueError as exc:
        raise DatasetError(f"{row_id}: cannot parse {field}={stripped!r}") from exc


def _datetime(value: str | None, *, field: str, row_id: str) -> datetime:
    stripped = _text(value)
    if not stripped:
        raise DatasetError(f"{row_id}: required timestamp {field!r} is blank")
    try:
        return datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DatasetError(f"{row_id}: cannot parse {field}={stripped!r}") from exc


def _bool(value: str | None, *, field: str, row_id: str) -> bool:
    stripped = _text(value).lower()
    if stripped == "true":
        return True
    if stripped == "false":
        return False
    raise DatasetError(f"{row_id}: cannot parse {field}={value!r} as a boolean")


def _optional_int(value: str | None, *, field: str, row_id: str) -> int | None:
    stripped = _text(value)
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError as exc:
        raise DatasetError(f"{row_id}: cannot parse {field}={stripped!r}") from exc


def _int(value: str | None, *, field: str, row_id: str) -> int:
    parsed = _optional_int(value, field=field, row_id=row_id)
    if parsed is None:
        raise DatasetError(f"{row_id}: required integer {field!r} is blank")
    return parsed


def _string_list(value: str | None) -> tuple[str, ...]:
    """Split a ``|``-separated profile column into a real tuple."""
    return tuple(part.strip() for part in _text(value).split("|") if part.strip())


def quantize(amount: Decimal) -> Decimal:
    """Round a Decimal to two places, half-up, the way money rounds."""
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


def quantize_on_conversion(amount: Decimal) -> Decimal:
    """Apply cent rounding only if :data:`src.config.QUANTIZE_AT` says so.

    Under ``quantize_at() == "render"`` the full-precision product is kept
    and rounding is deferred to :mod:`src.render`.
    """
    return quantize(amount) if config.quantize_at() == "conversion" else amount


def id_sort_key(identifier: str) -> tuple[str, int, str]:
    """Sort ``event_9`` before ``event_10`` instead of lexicographically."""
    match = _ID_SUFFIX_RE.search(identifier)
    if not match:
        return (identifier, 0, identifier)
    return (identifier[: match.start()], int(match.group(1)), identifier)


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise DatasetError(f"missing dataset file: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise DatasetError(f"{path.name}: file has no header row")
        return [
            {key: (row.get(key) or "") for key in reader.fieldnames} for row in reader
        ]


# ------------------------------------------------------------ rate table


@dataclass(frozen=True)
class ExchangeRate:
    rate_date: date
    from_currency: str
    to_currency: str
    rate: Decimal


class ExchangeRateTable:
    """Fixed, dated conversion rates with strict exact-date lookup.

    The dataset supplies rates only on the 1st and 15th of a month, in five
    one-directional pairs (``USD``/``EUR`` as source only). Every
    foreign-currency event in the supplied data settles on a covered date, so
    an exact match always exists; a miss means the data changed and must be
    surfaced, not papered over.
    """

    def __init__(self, rates: Iterable[ExchangeRate]) -> None:
        self._by_key: dict[tuple[date, str, str], Decimal] = {}
        self._pairs: set[tuple[str, str]] = set()
        for rate in rates:
            key = (rate.rate_date, rate.from_currency, rate.to_currency)
            existing = self._by_key.get(key)
            if existing is not None and existing != rate.rate:
                raise DatasetError(
                    f"exchange_rates.csv: conflicting rates for {key}: "
                    f"{existing} and {rate.rate}"
                )
            self._by_key[key] = rate.rate
            self._pairs.add((rate.from_currency, rate.to_currency))

    def __len__(self) -> int:
        return len(self._by_key)

    @property
    def pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset(self._pairs)

    def rate(
        self, from_currency: str, to_currency: str, on: date
    ) -> Decimal | None:
        """Exact-date rate, or ``None``. Identity conversions return ``1``."""
        if from_currency == to_currency:
            return Decimal(1)
        return self._by_key.get((on, from_currency, to_currency))

    def miss_reason(self, from_currency: str, to_currency: str) -> str:
        if (from_currency, to_currency) in self._pairs:
            return "pair exists but not on that date"
        return "currency pair absent entirely"

    def convert(
        self, amount: Decimal, from_currency: str, to_currency: str, on: date
    ) -> Decimal:
        """Convert an amount, raising if no exact-date rate exists."""
        rate = self.rate(from_currency, to_currency, on)
        if rate is None:
            raise MissingExchangeRateError(
                [
                    ExchangeRateGap(
                        event_id="<direct conversion>",
                        user_id="<unknown>",
                        rate_date=on,
                        from_currency=from_currency,
                        to_currency=to_currency,
                        reason=self.miss_reason(from_currency, to_currency),
                    )
                ]
            )
        return quantize(amount * rate)


# ------------------------------------------------------------- records


@dataclass(frozen=True)
class Profile:
    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    financial_priorities: tuple[str, ...]
    expense_categories_to_protect: tuple[str, ...]
    expense_categories_user_is_willing_to_reduce: tuple[str, ...]
    expense_categories_user_is_willing_to_stop: tuple[str, ...]
    payment_methods_user_will_consider: tuple[str, ...]
    max_installment_months: int | None

    def accepts(self, method: str) -> bool:
        return method in self.payment_methods_user_will_consider


@dataclass(frozen=True)
class FinancialEvent:
    """One ledger row, with both its original and home-currency amounts.

    ``amount`` and ``minimum_allowed_amount`` are in ``currency``;
    ``amount_home`` and ``minimum_allowed_amount_home`` are the same figures
    converted into the user's home currency at ``fx_rate``. All four are
    ``None`` when the source is blank -- a blank amount means "look at the
    linked image", never zero.
    """

    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str
    amount: Decimal | None
    currency: str
    event_date: date
    settlement_date: date | None
    status: str
    linked_event_id: str | None
    flexibility: str
    minimum_allowed_amount: Decimal | None
    home_currency: str
    fx_rate: Decimal | None
    amount_home: Decimal | None
    minimum_allowed_amount_home: Decimal | None

    @property
    def cash_date(self) -> date:
        """The date the money actually moves.

        Settlement where the source supplies it, falling back to the event
        date. The fallback is only ever correct for the ten ``unrealized``
        valuation rows, which never move cash at all --
        :func:`_assert_settlement_dates_present` enforces that no pending or
        scheduled row can reach it. A pending debit falling back to its event
        date would land in the past and silently vanish from the forecast.
        """
        return self.settlement_date or self.event_date

    @property
    def fx_date(self) -> date:
        """The date the exchange-rate lookup is keyed on."""
        if config.fx_date_key() == "event_date":
            return self.event_date
        return self.cash_date

    @property
    def has_amount(self) -> bool:
        return self.amount is not None

    def to_home(self, amount: Decimal) -> Decimal:
        """Convert an amount in this event's currency to the home currency.

        Used by :mod:`src.extract` once an image supplies the amount for a
        blank row, so the extraction step does not need the rate table.
        """
        if self.fx_rate is None:
            raise DatasetError(
                f"{self.event_id}: no exchange rate resolved for "
                f"{self.currency} -> {self.home_currency}"
            )
        return quantize_on_conversion(amount * self.fx_rate)


@dataclass(frozen=True)
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: Decimal
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str
    #: Populated only for ``sample_requests.csv`` rows, which ship with the
    #: eight output columns filled in. Always ``None`` for evaluation rows.
    expected: "SampleAnswer | None" = None


@dataclass(frozen=True)
class SampleAnswer:
    """The solved output columns supplied with ``sample_requests.csv``."""

    amount_safe_to_pay: Decimal
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str
    earliest_date_for_full_payment: date | None
    spending_changes_needed: str
    decision_explanation: str


@dataclass(frozen=True)
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: str
    payment_amount: Decimal
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: int | None
    financing_fee: Decimal
    total_payable_amount: Decimal


@dataclass(frozen=True)
class Message:
    message_id: str
    user_id: str
    request_id: str | None
    related_event_id: str | None
    sent_at: datetime
    source_type: str
    message_text: str


@dataclass(frozen=True)
class ImageRef:
    image_id: str
    user_id: str
    request_id: str | None
    related_event_id: str | None
    path: Path

    @property
    def exists(self) -> bool:
        return self.path.is_file()


@dataclass(frozen=True)
class UserContext:
    """Everything one request needs, and nothing belonging to anyone else.

    Built by :meth:`Dataset.get_user_context`. Filtering happens here in
    code so that whole files never reach a prompt: an extraction call sees
    one user's messages and images, not 215 rows belonging to 275 people.
    """

    request: Request
    profile: Profile
    events: tuple[FinancialEvent, ...]
    messages: tuple[Message, ...]
    images: tuple[ImageRef, ...]
    payment_options: tuple[PaymentOption, ...]

    @property
    def user_id(self) -> str:
        return self.profile.user_id

    @property
    def request_id(self) -> str:
        return self.request.request_id

    @property
    def home_currency(self) -> str:
        return self.profile.home_currency

    def events_by_id(self) -> dict[str, FinancialEvent]:
        return {event.event_id: event for event in self.events}

    def events_on_or_before(self, cutoff: date) -> tuple[FinancialEvent, ...]:
        """History available at ``cutoff``, by the date cash moves."""
        return tuple(event for event in self.events if event.cash_date <= cutoff)

    def events_after(self, cutoff: date) -> tuple[FinancialEvent, ...]:
        return tuple(event for event in self.events if event.cash_date > cutoff)

    def blank_amount_events(self) -> tuple[FinancialEvent, ...]:
        return tuple(event for event in self.events if not event.has_amount)

    def images_for_event(self, event_id: str) -> tuple[ImageRef, ...]:
        return tuple(
            image for image in self.images if image.related_event_id == event_id
        )


# ------------------------------------------------------------- row builders


def _build_profile(row: Mapping[str, str]) -> Profile:
    user_id = _text(row["user_id"])
    return Profile(
        user_id=user_id,
        home_currency=_text(row["home_currency"]),
        current_available_balance=_decimal(
            row["current_available_balance"],
            field="current_available_balance",
            row_id=user_id,
        ),
        minimum_balance_to_keep=_decimal(
            row["minimum_balance_to_keep"],
            field="minimum_balance_to_keep",
            row_id=user_id,
        ),
        financial_priorities=_string_list(row["financial_priorities"]),
        expense_categories_to_protect=_string_list(
            row["expense_categories_to_protect"]
        ),
        expense_categories_user_is_willing_to_reduce=_string_list(
            row["expense_categories_user_is_willing_to_reduce"]
        ),
        expense_categories_user_is_willing_to_stop=_string_list(
            row["expense_categories_user_is_willing_to_stop"]
        ),
        payment_methods_user_will_consider=_string_list(
            row["payment_methods_user_will_consider"]
        ),
        max_installment_months=_optional_int(
            row["max_installment_months"],
            field="max_installment_months",
            row_id=user_id,
        ),
    )


def _build_event(
    row: Mapping[str, str],
    profiles: Mapping[str, Profile],
    rates: ExchangeRateTable,
    gaps: list[ExchangeRateGap],
) -> FinancialEvent:
    event_id = _text(row["event_id"])
    user_id = _text(row["user_id"])
    profile = profiles.get(user_id)
    if profile is None:
        raise DatasetError(f"{event_id}: no profile for user {user_id!r}")

    currency = _text(row["currency"])
    home_currency = profile.home_currency
    amount = _optional_decimal(row["amount"], field="amount", row_id=event_id)
    minimum_allowed = _optional_decimal(
        row["minimum_allowed_amount"],
        field="minimum_allowed_amount",
        row_id=event_id,
    )
    event_date = _date(row["event_date"], field="event_date", row_id=event_id)
    settlement_date = _optional_date(
        row["settlement_date"], field="settlement_date", row_id=event_id
    )

    # Keyed per config.fx_date_key(); the dataset contract states settlement.
    rate_date = (
        event_date
        if config.fx_date_key() == "event_date"
        else (settlement_date or event_date)
    )
    fx_rate = rates.rate(currency, home_currency, rate_date)
    if fx_rate is None:
        gap = ExchangeRateGap(
            event_id=event_id,
            user_id=user_id,
            rate_date=rate_date,
            from_currency=currency,
            to_currency=home_currency,
            reason=rates.miss_reason(currency, home_currency),
        )
        gaps.append(gap)
        logger.error("exchange-rate gap: %s", gap)

    return FinancialEvent(
        event_id=event_id,
        user_id=user_id,
        event_type=_text(row["event_type"]),
        description=_text(row["description"]),
        category=_text(row["category"]),
        direction=_text(row["direction"]),
        amount=amount,
        currency=currency,
        event_date=event_date,
        settlement_date=settlement_date,
        status=_text(row["status"]),
        linked_event_id=_optional_text(row["linked_event_id"]),
        flexibility=_text(row["flexibility"]),
        minimum_allowed_amount=minimum_allowed,
        home_currency=home_currency,
        fx_rate=fx_rate,
        # Blank stays blank. extract.py fills these via FinancialEvent.to_home.
        amount_home=(
            quantize_on_conversion(amount * fx_rate)
            if amount is not None and fx_rate is not None
            else None
        ),
        minimum_allowed_amount_home=(
            quantize_on_conversion(minimum_allowed * fx_rate)
            if minimum_allowed is not None and fx_rate is not None
            else None
        ),
    )


def _build_request(row: Mapping[str, str], *, with_answer: bool) -> Request:
    request_id = _text(row["request_id"])
    answer: SampleAnswer | None = None
    if with_answer:
        answer = SampleAnswer(
            amount_safe_to_pay=_decimal(
                row["amount_safe_to_pay"],
                field="amount_safe_to_pay",
                row_id=request_id,
            ),
            affordability_status=_text(row["affordability_status"]),
            recommended_payment_method=_text(row["recommended_payment_method"]),
            payment_plan=_text(row["payment_plan"]),
            earliest_date_for_full_payment=_optional_date(
                row["earliest_date_for_full_payment"],
                field="earliest_date_for_full_payment",
                row_id=request_id,
            ),
            spending_changes_needed=_text(row["spending_changes_needed"]),
            decision_explanation=_text(row["decision_explanation"]),
        )
    return Request(
        request_id=request_id,
        user_id=_text(row["user_id"]),
        request_date=_date(row["request_date"], field="request_date", row_id=request_id),
        request_type=_text(row["request_type"]),
        requested_amount=_decimal(
            row["requested_amount"], field="requested_amount", row_id=request_id
        ),
        desired_completion_date=_date(
            row["desired_completion_date"],
            field="desired_completion_date",
            row_id=request_id,
        ),
        allows_partial_payment=_bool(
            row["allows_partial_payment"],
            field="allows_partial_payment",
            row_id=request_id,
        ),
        request_text=_text(row["request_text"]),
        expected=answer,
    )


def _build_payment_option(row: Mapping[str, str]) -> PaymentOption:
    option_id = _text(row["payment_option_id"])
    return PaymentOption(
        payment_option_id=option_id,
        request_id=_text(row["request_id"]),
        payment_method=_text(row["payment_method"]),
        payment_amount=_decimal(
            row["payment_amount"], field="payment_amount", row_id=option_id
        ),
        number_of_payments=_int(
            row["number_of_payments"], field="number_of_payments", row_id=option_id
        ),
        first_payment_date=_date(
            row["first_payment_date"], field="first_payment_date", row_id=option_id
        ),
        payment_frequency_days=_optional_int(
            row["payment_frequency_days"],
            field="payment_frequency_days",
            row_id=option_id,
        ),
        financing_fee=_decimal(
            row["financing_fee"], field="financing_fee", row_id=option_id
        ),
        total_payable_amount=_decimal(
            row["total_payable_amount"],
            field="total_payable_amount",
            row_id=option_id,
        ),
    )


def _build_message(row: Mapping[str, str]) -> Message:
    message_id = _text(row["message_id"])
    return Message(
        message_id=message_id,
        user_id=_text(row["user_id"]),
        request_id=_optional_text(row["request_id"]),
        related_event_id=_optional_text(row["related_event_id"]),
        sent_at=_datetime(row["sent_at"], field="sent_at", row_id=message_id),
        source_type=_text(row["source_type"]),
        message_text=row["message_text"],
    )


def _build_image(row: Mapping[str, str]) -> ImageRef:
    image_id = _text(row["image_id"])
    return ImageRef(
        image_id=image_id,
        user_id=_text(row["user_id"]),
        request_id=_optional_text(row["request_id"]),
        related_event_id=_optional_text(row["related_event_id"]),
        path=paths.image_path(image_id),
    )


# ------------------------------------------------------------- dataset


@dataclass(frozen=True)
class Dataset:
    """The whole dataset, indexed for per-user retrieval."""

    profiles: Mapping[str, Profile]
    events: tuple[FinancialEvent, ...]
    requests: tuple[Request, ...]
    sample_requests: tuple[Request, ...]
    payment_options: tuple[PaymentOption, ...]
    messages: tuple[Message, ...]
    images: tuple[ImageRef, ...]
    exchange_rates: ExchangeRateTable
    _events_by_user: Mapping[str, tuple[FinancialEvent, ...]]
    _messages_by_user: Mapping[str, tuple[Message, ...]]
    _images_by_user: Mapping[str, tuple[ImageRef, ...]]
    _options_by_request: Mapping[str, tuple[PaymentOption, ...]]
    _requests_by_id: Mapping[str, Request]

    def request(self, request_id: str) -> Request:
        """Look up a request from ``requests.csv`` or ``sample_requests.csv``."""
        try:
            return self._requests_by_id[request_id]
        except KeyError:
            raise DatasetError(f"unknown request_id {request_id!r}") from None

    def profile(self, user_id: str) -> Profile:
        try:
            return self.profiles[user_id]
        except KeyError:
            raise DatasetError(f"unknown user_id {user_id!r}") from None

    def get_user_context(self, user_id: str, request_id: str) -> UserContext:
        """Return only this user's records, plus the one request row.

        Every downstream stage -- and in particular every prompt built by
        :mod:`src.extract` -- works from this bundle. Filtering is done here,
        in code, so no prompt ever sees another user's finances.

        Raises :class:`DatasetError` if the request does not belong to the
        user, which would otherwise leak one user's request into another
        user's context.
        """
        profile = self.profile(user_id)
        request = self.request(request_id)
        if request.user_id != user_id:
            raise DatasetError(
                f"{request_id} belongs to {request.user_id!r}, not {user_id!r}"
            )

        events = self._events_by_user.get(user_id, ())
        messages = self._messages_by_user.get(user_id, ())
        images = self._images_by_user.get(user_id, ())
        options = self._options_by_request.get(request_id, ())

        # Defence in depth: these are already filtered by the index, but a
        # leak here would be silent and would reach a prompt.
        assert all(event.user_id == user_id for event in events)
        assert all(message.user_id == user_id for message in messages)
        assert all(image.user_id == user_id for image in images)
        assert all(option.request_id == request_id for option in options)

        return UserContext(
            request=request,
            profile=profile,
            events=events,
            messages=messages,
            images=images,
            payment_options=options,
        )

    def iter_contexts(self, *, samples: bool = False) -> Iterable[UserContext]:
        """Yield one context per evaluation request, in file order."""
        source = self.sample_requests if samples else self.requests
        for request in source:
            yield self.get_user_context(request.user_id, request.request_id)


#: Statuses whose cash movement lies in the future and therefore *must* carry
#: an explicit settlement date.
_FUTURE_STATUSES = frozenset({"pending", "scheduled"})


def _assert_settlement_dates_present(events: Sequence[FinancialEvent]) -> None:
    """Guard the ``cash_date`` fallback at the load/state boundary.

    ``cash_date`` falls back to ``event_date`` when ``settlement_date`` is
    blank. That is only correct for the ten ``unrealized`` valuation rows,
    which never move cash. A pending debit with a blank settlement date would
    fall back to its event date, land at or before ``request_date``, be
    classified as settled history, and silently disappear from the forecast --
    an outflow the user is actually going to incur.

    In the supplied data no pending or scheduled row has a blank settlement
    date, so this never fires; it exists to make sure a future dataset
    revision fails loudly instead of quietly under-forecasting.
    """
    offenders = [
        event
        for event in events
        if event.settlement_date is None and event.status in _FUTURE_STATUSES
    ]
    if offenders:
        preview = ", ".join(
            f"{event.event_id}({event.status})" for event in offenders[:20]
        )
        raise DatasetError(
            f"{len(offenders)} {'/'.join(sorted(_FUTURE_STATUSES))} event(s) have a "
            f"blank settlement_date, so cash_date would fall back to event_date "
            f"and place a future cash movement in the past: {preview}"
        )

    unexpected = [
        event
        for event in events
        if event.settlement_date is None and event.status != "unrealized"
    ]
    if unexpected:
        preview = ", ".join(
            f"{event.event_id}({event.status})" for event in unexpected[:20]
        )
        raise DatasetError(
            f"{len(unexpected)} event(s) have a blank settlement_date with an "
            f"unexpected status; only 'unrealized' valuations may omit it: {preview}"
        )


def _event_sort_key(event: FinancialEvent) -> tuple[date, date, tuple[str, int, str]]:
    """Chronological order.

    ``financial_events.csv`` is grouped by series -- all of a user's rent
    rows, then all of their utility rows -- so every consumer needs this.
    Ordered by event date, then settlement date, then numeric event id for a
    stable total order.
    """
    return (event.event_date, event.cash_date, id_sort_key(event.event_id))


def load_dataset(dataset_dir: Path | None = None) -> Dataset:
    """Read and normalize all nine CSVs.

    Raises :class:`MissingExchangeRateError` listing every gap if any
    foreign-currency event lacks an exact-date rate. No fallback to a
    neighbouring date ever happens.
    """
    directory = Path(dataset_dir) if dataset_dir else paths.DATASET_DIR
    logger.info("loading dataset from %s", directory)

    rate_rows = _read_rows(directory / "exchange_rates.csv")
    rates = ExchangeRateTable(
        ExchangeRate(
            rate_date=_date(row["rate_date"], field="rate_date", row_id="exchange_rate"),
            from_currency=_text(row["from_currency"]),
            to_currency=_text(row["to_currency"]),
            rate=_decimal(row["rate"], field="rate", row_id="exchange_rate"),
        )
        for row in rate_rows
    )

    profiles = {}
    for row in _read_rows(directory / "financial_profiles.csv"):
        profile = _build_profile(row)
        if profile.user_id in profiles:
            raise DatasetError(f"duplicate profile for user {profile.user_id!r}")
        profiles[profile.user_id] = profile

    gaps: list[ExchangeRateGap] = []
    events = tuple(
        sorted(
            (
                _build_event(row, profiles, rates, gaps)
                for row in _read_rows(directory / "financial_events.csv")
            ),
            key=_event_sort_key,
        )
    )
    if gaps:
        raise MissingExchangeRateError(gaps)
    _assert_settlement_dates_present(events)

    requests = tuple(
        _build_request(row, with_answer=False)
        for row in _read_rows(directory / "requests.csv")
    )
    sample_requests = tuple(
        _build_request(row, with_answer=True)
        for row in _read_rows(directory / "sample_requests.csv")
    )
    payment_options = tuple(
        _build_payment_option(row)
        for row in _read_rows(directory / "request_payment_options.csv")
    )
    messages = tuple(
        _build_message(row) for row in _read_rows(directory / "messages.csv")
    )
    images = tuple(_build_image(row) for row in _read_rows(directory / "images.csv"))

    requests_by_id: dict[str, Request] = {}
    for request in requests + sample_requests:
        if request.request_id in requests_by_id:
            raise DatasetError(f"duplicate request_id {request.request_id!r}")
        requests_by_id[request.request_id] = request

    events_by_user: dict[str, list[FinancialEvent]] = {}
    for event in events:
        events_by_user.setdefault(event.user_id, []).append(event)

    messages_by_user: dict[str, list[Message]] = {}
    for message in messages:
        messages_by_user.setdefault(message.user_id, []).append(message)

    images_by_user: dict[str, list[ImageRef]] = {}
    for image in images:
        images_by_user.setdefault(image.user_id, []).append(image)

    options_by_request: dict[str, list[PaymentOption]] = {}
    for option in payment_options:
        options_by_request.setdefault(option.request_id, []).append(option)

    logger.info(
        "loaded %d profiles, %d events, %d requests, %d samples, %d options, "
        "%d messages, %d images, %d rates",
        len(profiles),
        len(events),
        len(requests),
        len(sample_requests),
        len(payment_options),
        len(messages),
        len(images),
        len(rates),
    )

    return Dataset(
        profiles=profiles,
        events=events,
        requests=requests,
        sample_requests=sample_requests,
        payment_options=payment_options,
        messages=messages,
        images=images,
        exchange_rates=rates,
        _events_by_user={user: tuple(rows) for user, rows in events_by_user.items()},
        _messages_by_user={
            user: tuple(sorted(rows, key=lambda m: (m.sent_at, id_sort_key(m.message_id))))
            for user, rows in messages_by_user.items()
        },
        _images_by_user={
            user: tuple(sorted(rows, key=lambda i: id_sort_key(i.image_id)))
            for user, rows in images_by_user.items()
        },
        _options_by_request={
            request_id: tuple(
                sorted(rows, key=lambda o: id_sort_key(o.payment_option_id))
            )
            for request_id, rows in options_by_request.items()
        },
        _requests_by_id=requests_by_id,
    )


@lru_cache(maxsize=4)
def _cached_dataset(directory: str) -> Dataset:
    return load_dataset(Path(directory))


def get_dataset(dataset_dir: Path | None = None) -> Dataset:
    """Load once per directory and reuse. Parsing 25k events is not free."""
    directory = Path(dataset_dir) if dataset_dir else paths.DATASET_DIR
    return _cached_dataset(str(directory.resolve()))


def get_user_context(
    user_id: str, request_id: str, dataset_dir: Path | None = None
) -> UserContext:
    """Module-level convenience wrapper around :meth:`Dataset.get_user_context`."""
    return get_dataset(dataset_dir).get_user_context(user_id, request_id)


# ------------------------------------------------------------- introspection


def iter_amount_fields(record: object) -> Iterable[tuple[str, object]]:
    """Yield ``(dotted_name, value)`` for every field of a dataclass record.

    Used by the tests to prove no float ever reaches a money path. Recurses
    into nested dataclasses and tuples.
    """
    if not is_dataclass(record):
        return
    for field in fields(record):
        value = getattr(record, field.name)
        if is_dataclass(value):
            for name, nested in iter_amount_fields(value):
                yield f"{field.name}.{name}", nested
        elif isinstance(value, tuple):
            for index, item in enumerate(value):
                if is_dataclass(item):
                    for name, nested in iter_amount_fields(item):
                        yield f"{field.name}[{index}].{name}", nested
                else:
                    yield f"{field.name}[{index}]", item
        else:
            yield field.name, value
