"""Filesystem locations, resolved relative to this file.

Nothing in the project may hardcode a user path or a platform home
directory; every path is derived from the repository root so the project
stays correct across clones, renames, and checkouts.
"""

from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent

DATASET_DIR = REPO_ROOT / "dataset"
MEDIA_IMAGES_DIR = DATASET_DIR / "media" / "images"

AUDIT_DIR = REPO_ROOT / "audit"
EVALUATION_DIR = REPO_ROOT / "evaluation"
TESTS_DIR = REPO_ROOT / "tests"

FINANCIAL_PROFILES_CSV = DATASET_DIR / "financial_profiles.csv"
FINANCIAL_EVENTS_CSV = DATASET_DIR / "financial_events.csv"
EXCHANGE_RATES_CSV = DATASET_DIR / "exchange_rates.csv"
REQUESTS_CSV = DATASET_DIR / "requests.csv"
SAMPLE_REQUESTS_CSV = DATASET_DIR / "sample_requests.csv"
REQUEST_PAYMENT_OPTIONS_CSV = DATASET_DIR / "request_payment_options.csv"
MESSAGES_CSV = DATASET_DIR / "messages.csv"
IMAGES_CSV = DATASET_DIR / "images.csv"
OUTPUT_TEMPLATE_CSV = DATASET_DIR / "output.csv"

# The graded artefact lives in the repository root, not in dataset/.
OUTPUT_CSV = REPO_ROOT / "output.csv"

OUTPUT_COLUMNS = (
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)


def image_path(image_id: str) -> Path:
    """Resolve ``image_07`` to ``dataset/media/images/image_07.png``."""
    return MEDIA_IMAGES_DIR / f"{image_id}.png"
