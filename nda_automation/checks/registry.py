from __future__ import annotations

from typing import Callable, Dict, List

from ..review_document import Paragraph
from .common import ClauseResult
from .confidential_information import _check_confidential_information
from .governing_law import _check_governing_law
from .mutuality import _check_mutuality
from .non_circumvention import _check_non_circumvention
from .signatures import _check_signatures
from .term_and_survival import _check_term_and_survival

CheckFn = Callable[[str, str, Dict[str, object], List[Paragraph]], ClauseResult]

CLAUSE_CHECKS: List[tuple[str, CheckFn]] = [
    ("mutuality", _check_mutuality),
    ("confidential_information", _check_confidential_information),
    ("governing_law", _check_governing_law),
    ("term_and_survival", _check_term_and_survival),
    ("non_circumvention", _check_non_circumvention),
    ("signatures", _check_signatures),
]
