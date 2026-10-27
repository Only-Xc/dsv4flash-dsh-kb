"""Shared English/Arabic record intent parsing, independent of account existence.

Only the authenticated Profile inventory may resolve an owner. Never look up an
unknown owner to decide whether to refuse; that would create an enumeration oracle.
The vocabulary describes record nouns and query grammar, not acceptance prompts.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .profile_scope import ProfileContext, requested_profile


def normalize_record_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[\u0610-\u061a\u064b-\u065f\u0670\u0640]", "", text)
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))
    return " ".join(text.split())


AR_RECORD_NOUNS = {
    "طلب": "applications", "طلبات": "applications",
    "رخصه": "licenses", "رخص": "licenses", "ترخيص": "licenses", "تراخيص": "licenses",
    "غرامه": "fines", "غرامات": "fines", "مخالفه": "fines", "مخالفات": "fines",
    "مهام": "pending", "اجراء": "pending", "اجراءات": "pending",
}
EN_RECORD_NOUNS = {
    "application": "applications", "applications": "applications", "request": "applications", "requests": "applications",
    "license": "licenses", "licenses": "licenses", "licence": "licenses", "licences": "licenses", "permit": "licenses", "permits": "licenses",
    "fine": "fines", "fines": "fines", "violation": "fines", "violations": "fines", "tasks": "pending", "actions": "pending",
}
QUERY_WORDS = frozenset({
    "show", "list", "view", "find", "search", "check", "query", "display", "status", "progress", "latest", "pending",
    "عرض", "اعرض", "ارني", "اظهر", "استعرض", "ابحث", "استعلم", "قائمه", "حاله", "تقدم", "احدث",
})
SELF_WORDS = frozenset({"my", "mine", "our", "ours", "لي", "حسابي", "ملفي", "لدي", "لدينا"})


@dataclass(frozen=True)
class CustomerRecordIntent:
    categories: frozenset[str]
    query: bool
    external_owner: bool
    read_query: bool = False


def customer_record_intent(content: str, context: ProfileContext | None = None) -> CustomerRecordIntent:
    text = normalize_record_text(content)
    words = re.findall(r"[\w]+", text)
    categories: set[str] = set()
    record_words: set[str] = set()
    own = any(word in SELF_WORDS for word in words)
    for word in words:
        if word in EN_RECORD_NOUNS:
            categories.add(EN_RECORD_NOUNS[word])
            record_words.add(word)
        arabic = word.removeprefix("و").removeprefix("ال")
        if arabic.startswith(("ل", "ب")):
            remainder = arabic[1:].removeprefix("ال")
            if remainder in AR_RECORD_NOUNS or any(remainder.endswith(suffix) and remainder[:-len(suffix)] in AR_RECORD_NOUNS for suffix in ("ي", "نا")):
                arabic = remainder
        if arabic in AR_RECORD_NOUNS:
            categories.add(AR_RECORD_NOUNS[arabic])
            record_words.add(word)
        for suffix in ("ي", "نا"):
            if arabic.endswith(suffix) and arabic[:-len(suffix)] in AR_RECORD_NOUNS:
                categories.add(AR_RECORD_NOUNS[arabic[:-len(suffix)]])
                own = True
                record_words.add(word)
    has_query_word = any(
        word in QUERY_WORDS or word.removeprefix("و").removeprefix("ال") in QUERY_WORDS
        for word in words
    )
    read_query = bool(categories) and (
        has_query_word
        or (own and all(word in SELF_WORDS or word in record_words for word in words))
    )
    query = bool(categories) and (own or any(word in QUERY_WORDS for word in words))
    if not query:
        # A bare account handle is a common follow-up to a records question.
        # Refuse it without probing existence or letting the model reuse the
        # previous external target. Public application references are hyphenated.
        bare_owner = text.strip(" .،؟?!")
        if re.fullmatch(r"[\w+-]+(?:[.@][\w+-]+)+", bare_owner) and not requested_profile(content, context):
            return CustomerRecordIntent(frozenset(categories), True, True)
        return CustomerRecordIntent(frozenset(categories), False, False)

    # Remove only an unambiguous, authorized Profile name from owner grammar.
    # Other identifiers in the same sentence must still be refused.
    target = requested_profile(content, context)
    owner_text = text
    if target:
        for profile_name in (target.name, *target.aliases):
            name = normalize_record_text(profile_name)
            owner_text = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", " ", owner_text)
    # Account handles/email addresses are owners, not first-person pronouns.
    # Immutable application references (ML-...) do not match this grammar.
    # Sentence punctuation is not part of the authorization boundary. Dots
    # inside a handle still separate its labels, while a final full stop (or
    # leading ellipsis) must not hide an otherwise external owner.
    external = bool(re.search(r"(?<![\w-])[\w+-]+(?:[.@][\w+-]+)+(?![\w-])", owner_text))
    owner_patterns = (
        r"\b(?:show|find|query|list|check|search|view|display)\s+(?:me\s+)?([\w@.+-]+)(?:['’]s|\s+(?=(?:licenses?|licences?|permits?|fines?|violations?|applications?|requests?|pending)\b))",
        r"\b(?:licenses?|licences?|permits?|fines?|violations?|applications?|requests?)\s+(?:for|of|belonging\s+to)\s+([\w@.+-]+)",
        r"(?:" + "|".join(map(re.escape, AR_RECORD_NOUNS)) + r")\s+(?:الخاصه\s+ب)?([\w@.+-]+)",
        r"(?:\bprofile\s+|ملف\s+)([\w@.+-]+)",
    )
    non_owners = SELF_WORDS | QUERY_WORDS | frozenset(EN_RECORD_NOUNS) | frozenset(AR_RECORD_NOUNS) | {
        "and", "in", "the", "a", "this", "current", "selected", "to", "و", "والغرامات", "والمهام", "المعلقه", "الشخصي", "الحالي", "المحدد",
        "في", "من", "ضمن", "تحت", "هذا", "هذه",
    }
    for pattern in owner_patterns:
        for match in re.finditer(pattern, owner_text):
            candidate = match.group(1)
            arabic_candidate = candidate.removeprefix("و").removeprefix("ال")
            is_record_word = arabic_candidate in AR_RECORD_NOUNS or any(
                arabic_candidate.endswith(suffix) and arabic_candidate[:-len(suffix)] in AR_RECORD_NOUNS
                for suffix in ("ي", "نا")
            )
            if candidate not in non_owners and not is_record_word and not re.fullmatch(r"[a-z]{2,6}(?:-\d+){3,}", candidate):
                external = True
    return CustomerRecordIntent(frozenset(categories), query, external, read_query)
