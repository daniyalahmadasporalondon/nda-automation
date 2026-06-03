from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping

from ..review_document import Paragraph
from .common import ClauseResult
from .confidential_information import _check_confidential_information
from .governing_law import _check_governing_law
from .mutuality import _check_mutuality
from .non_circumvention import _check_non_circumvention
from .signatures import _check_signatures
from .term_and_survival import _check_term_and_survival
from .mutuality import reason_code as _mutuality_reason_code
from .confidential_information import reason_code as _confidential_information_reason_code
from .governing_law import reason_code as _governing_law_reason_code
from .term_and_survival import reason_code as _term_and_survival_reason_code
from .non_circumvention import reason_code as _non_circumvention_reason_code
from .signatures import reason_code as _signatures_reason_code

CheckFn = Callable[[str, str, Dict[str, object], List[Paragraph], Dict[str, object]], ClauseResult]

CLAUSE_CHECKS: List[tuple[str, CheckFn]] = [
    ("mutuality", _check_mutuality),
    ("confidential_information", _check_confidential_information),
    ("governing_law", _check_governing_law),
    ("term_and_survival", _check_term_and_survival),
    ("non_circumvention", _check_non_circumvention),
    ("signatures", _check_signatures),
]

ReasonCodeFn = Callable[[Mapping[str, Any], str], str]

REASON_CODE_FUNCTIONS: Dict[str, ReasonCodeFn] = {
    "mutuality": _mutuality_reason_code,
    "confidential_information": _confidential_information_reason_code,
    "governing_law": _governing_law_reason_code,
    "term_and_survival": _term_and_survival_reason_code,
    "non_circumvention": _non_circumvention_reason_code,
    "signatures": _signatures_reason_code,
}
