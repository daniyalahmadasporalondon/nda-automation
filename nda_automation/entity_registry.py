"""Registry of Aspora's signing entities.

Each signing entity is a self-contained *bundle* so the draft flow can pick one
and have the legal name, address, and governing-law clause travel together.

The ``governing_law.playbook_option_id`` on every bundle is the join key into the
live playbook's ``governing_law`` clause (``rules.approved_options[].id``). Picking
an entity therefore selects the matching approved governing-law option, so the
right clause wording is pulled automatically when an NDA is generated.

The four positions exposed by the playbook today are ``india``, ``delaware``,
``england_and_wales`` and ``difc``. :func:`validate_registry_against_playbook`
checks that every bundle maps to one of those positions and fails loudly if the
playbook drifts (e.g. an option id is renamed), rather than silently generating
an unapproved governing law.
"""

from __future__ import annotations

from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Entity bundles
# ---------------------------------------------------------------------------
#
# Address shape: {id, label, lines[], country, default}. Exactly one address per
# entity is the default. Real Transfer Limited carries two (a registered office
# in Belfast and a corporate office in London); the London corporate office is
# the default for an NDA because the matching playbook governing-law position is
# England and Wales (see the flag in ENTITY_LAW_MAPPING_NOTES below).
#
# governing_law.playbook_option_id MUST equal a governing_law approved_options id
# in playbook.json. Keep these in sync; validate_registry_against_playbook()
# enforces it.

SIGNING_ENTITIES: list[dict[str, Any]] = [
    {
        "id": "aspora_technology",
        "legal_name": "Aspora Technology Services Private Limited",
        "short_name": "Aspora",
        "addresses": [
            {
                "id": "registered",
                "label": "Registered office",
                "lines": [
                    "Aswini Layout, Viveknagar",
                    "Bangalore 560047",
                    "India",
                ],
                "country": "India",
                "default": True,
            },
        ],
        "governing_law": {"playbook_option_id": "india", "label": "India"},
        "jurisdiction": "Courts of India",
        "incorporation_jurisdiction": "India",
        "signatory": {"name": "[Authorised Signatory]", "title": "[Title]"},
    },
    {
        "id": "vance_money",
        "legal_name": "Vance Money Services LLC",
        "short_name": "Vance Money",
        "addresses": [
            {
                "id": "registered",
                "label": "Registered office",
                "lines": [
                    "838 Walker Road",
                    "Dover, Delaware 19904",
                    "United States of America",
                ],
                "country": "United States of America",
                "default": True,
            },
        ],
        "governing_law": {"playbook_option_id": "delaware", "label": "Delaware"},
        "jurisdiction": "Courts of the State of Delaware",
        "incorporation_jurisdiction": "Delaware",
        "signatory": {"name": "[Authorised Signatory]", "title": "[Title]"},
    },
    {
        "id": "real_transfer",
        "legal_name": "Real Transfer Limited",
        "short_name": "Real Transfer",
        # Two addresses. The London corporate office is the NDA default because it
        # is the one that maps cleanly to the playbook's England and Wales
        # governing-law position. The Belfast registered office sits in Northern
        # Ireland, which is a separate legal jurisdiction with NO matching
        # playbook position (see ENTITY_LAW_MAPPING_NOTES["real_transfer"]).
        "addresses": [
            {
                "id": "corporate",
                "label": "Corporate office",
                "lines": [
                    "3rd Floor",
                    "141-145 Curtain Road",
                    "London, EC2A 3BX",
                    "United Kingdom",
                ],
                "country": "United Kingdom",
                "default": True,
            },
            {
                "id": "registered",
                "label": "Registered office",
                "lines": [
                    "Office 8, Merrion Business Centre",
                    "58 Howard Street",
                    "Belfast, Northern Ireland, BT1 6PJ",
                ],
                "country": "United Kingdom",
                "default": False,
            },
        ],
        "governing_law": {
            "playbook_option_id": "england_and_wales",
            "label": "England and Wales",
        },
        "jurisdiction": "Courts of England and Wales",
        # Legal confirmed England and Wales as the place of incorporation (not
        # Northern Ireland, despite the Belfast registered office) so it aligns
        # with the chosen governing law via the London corporate office.
        "incorporation_jurisdiction": "England and Wales",
        "signatory": {"name": "[Authorised Signatory]", "title": "[Title]"},
    },
    {
        "id": "vance_techlabs",
        "legal_name": "Vance Techlabs Limited",
        "short_name": "Vance Techlabs",
        "addresses": [
            {
                "id": "registered",
                "label": "Registered office",
                "lines": [
                    "Gate Avenue, DIFC",
                    "Dubai",
                    "United Arab Emirates",
                ],
                "country": "United Arab Emirates",
                "default": True,
            },
        ],
        # The entity sits in the DIFC free zone, a common-law jurisdiction with its
        # own DIFC Courts, distinct from UAE onshore/federal law. The playbook's
        # `difc` position is the right match. This would only be a gap if NDAs for
        # this entity were intended to run under UAE federal law, which the
        # playbook does not offer (see ENTITY_LAW_MAPPING_NOTES["vance_techlabs"]).
        "governing_law": {"playbook_option_id": "difc", "label": "DIFC"},
        "jurisdiction": "DIFC Courts, Dubai",
        "incorporation_jurisdiction": "DIFC",
        "signatory": {"name": "[Authorised Signatory]", "title": "[Title]"},
    },
    {
        "id": "nesse_technologies",
        "legal_name": "Nesse Technologies Inc",
        "short_name": "Nesse Technologies",
        "addresses": [
            {
                "id": "registered",
                "label": "Registered office",
                "lines": [
                    "151 Yonge Street, 11th Floor",
                    "Toronto, Ontario M5C 2W7",
                    "Canada",
                ],
                "country": "Canada",
                "default": True,
            },
        ],
        "governing_law": {"playbook_option_id": "ontario_canada", "label": "Ontario, Canada"},
        "jurisdiction": "Courts of Ontario, Canada",
        "incorporation_jurisdiction": "Ontario, Canada",
        "signatory": {"name": "[Authorised Signatory]", "title": "[Title]"},
    },
    {
        "id": "vance_technologies",
        "legal_name": "Vance Technologies Limited",
        "short_name": "Vance Technologies",
        "addresses": [
            {
                "id": "registered",
                "label": "Registered office",
                "lines": [
                    "Profile West, 950 Great West Road",
                    "Suite 2, First Floor",
                    "Brentford, TW8 9ES",
                    "United Kingdom",
                ],
                "country": "United Kingdom",
                "default": True,
            },
        ],
        "governing_law": {"playbook_option_id": "england_and_wales", "label": "England and Wales"},
        "jurisdiction": "Courts of England and Wales",
        "incorporation_jurisdiction": "England and Wales",
        "signatory": {"name": "[Authorised Signatory]", "title": "[Title]"},
    },
    {
        "id": "aspora_financial_services",
        "legal_name": "Aspora Financial Services (IFSC) Private Limited",
        "short_name": "Aspora Financial Services",
        "addresses": [
            {
                "id": "registered",
                "label": "Registered office",
                "lines": [
                    "Cabin No. 03-05, 3rd floor",
                    "Flexone, Building 15C2",
                    "Gift City, Gandhi Nagar",
                    "Gandhi Nagar- 382050, Gujarat",
                ],
                "country": "India",
                "default": True,
            },
        ],
        "governing_law": {"playbook_option_id": "india", "label": "India"},
        "jurisdiction": "Courts of India",
        "incorporation_jurisdiction": "India",
        "signatory": {"name": "[Authorised Signatory]", "title": "[Title]"},
    },
]

# Human-readable notes for the two entities whose jurisdiction needed a judgement
# call between addresses/legal systems. Both calls were CONFIRMED by legal, so the
# defaults below are locked. Surfaced to reviewers; not consumed by generation
# logic.
ENTITY_LAW_MAPPING_NOTES: dict[str, str] = {
    "real_transfer": (
        "Real Transfer Limited has a registered office in Belfast (Northern "
        "Ireland) and a corporate office in London (England). Northern Ireland is "
        "a separate legal jurisdiction from England and Wales with no matching "
        "playbook position. CONFIRMED by legal: NDAs run under England and Wales "
        "law via the London corporate office (the default address); the Belfast "
        "registered office is retained for reference. No new playbook position is "
        "needed."
    ),
    "vance_techlabs": (
        "Vance Techlabs Limited is registered in the DIFC free zone, whose DIFC "
        "Courts are a distinct common-law jurisdiction from UAE federal/onshore "
        "law. CONFIRMED by legal: NDAs run under the playbook's `difc` position, "
        "not UAE federal law."
    ),
}


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def list_entities() -> list[dict[str, Any]]:
    """Return all signing-entity bundles."""

    return [_copy_bundle(entity) for entity in SIGNING_ENTITIES]


def get_entity(entity_id: str) -> dict[str, Any] | None:
    """Return the bundle for ``entity_id`` or ``None`` if it is not registered."""

    for entity in SIGNING_ENTITIES:
        if entity["id"] == entity_id:
            return _copy_bundle(entity)
    return None


def default_address(entity: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the address marked ``default`` for ``entity`` (or ``None``)."""

    for address in entity.get("addresses", []):
        if address.get("default"):
            return dict(address)
    return None


def actor_slug(entity: Mapping[str, Any]) -> str:
    """The actor identifier for this entity when it signs/generates an artifact.

    The artifact registry slugs whatever ``actor`` it is given into an artifact
    filename (``{seq}_{actor}_{role}_v{n}.ext``). The entity ``id`` is already a
    stable, short slug, so passing it as the actor yields clean, predictable
    names (e.g. ``aspora_technology`` -> ``...aspora-technology...``) rather than
    a truncated slug of the long legal name.
    """

    return str(entity.get("id") or "")


def _copy_bundle(entity: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-ish copy so callers cannot mutate the module-level registry."""

    copied = dict(entity)
    copied["addresses"] = [dict(address) for address in entity.get("addresses", [])]
    copied["governing_law"] = dict(entity.get("governing_law", {}))
    copied["signatory"] = dict(entity.get("signatory", {}))
    return copied


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _playbook_governing_law_option_ids(playbook: Mapping[str, Any]) -> set[str]:
    """Collect the governing_law approved_options ids from a playbook mapping."""

    return {option["id"] for option in _playbook_governing_law_options(playbook)}


def _playbook_governing_law_options(playbook: Mapping[str, Any]) -> list[dict[str, str]]:
    """The governing_law approved_options as ``[{"id", "label"}]`` from the playbook.

    This is the canonical source for the draft-intake override dropdown's law
    choices (id + display label), so the frontend does not have to derive them
    from the embedded entity mirror. Ids are taken verbatim; the label falls back
    to value then id when an option omits it. Order follows the playbook.
    """

    for clause in playbook.get("clauses", []):
        if clause.get("id") != "governing_law":
            continue
        rules = clause.get("rules") or {}
        options = rules.get("approved_options") or []
        result: list[dict[str, str]] = []
        for option in options:
            if not isinstance(option, Mapping):
                continue
            option_id = str(option.get("id") or "").strip()
            if not option_id:
                continue
            label = (
                str(option.get("label") or "").strip()
                or str(option.get("value") or "").strip()
                or option_id
            )
            result.append({"id": option_id, "label": label})
        return result
    return []


def _playbook_max_term_years(playbook: Mapping[str, Any]) -> int | None:
    """Read the term cap from the playbook's ``term_and_survival`` clause.

    The cap lives as a top-level ``max_term_years`` integer on the
    ``term_and_survival`` clause (the same field the generation clamp reads).
    Returns the live value, or ``None`` when the clause/field is missing or
    malformed so the caller can omit it and let the frontend fall back.
    """

    for clause in playbook.get("clauses", []):
        if clause.get("id") != "term_and_survival":
            continue
        value = clause.get("max_term_years")
        # bool is an int subclass; reject it explicitly so True/False can never
        # masquerade as a 1-year cap.
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if value < 1:
            return None
        return value
    return None


def validate_registry() -> None:
    """Validate internal consistency of the bundles (no playbook needed).

    Checks that every bundle has the required fields and that each entity has
    exactly one default address. Raises ``ValueError`` on the first problem.
    """

    seen_ids: set[str] = set()
    for entity in SIGNING_ENTITIES:
        entity_id = entity.get("id")
        if not entity_id:
            raise ValueError("Signing entity is missing an id.")
        if entity_id in seen_ids:
            raise ValueError(f"Duplicate signing entity id: {entity_id}.")
        seen_ids.add(entity_id)

        if not entity.get("legal_name"):
            raise ValueError(f"Entity {entity_id} is missing legal_name.")

        if not entity.get("incorporation_jurisdiction"):
            raise ValueError(
                f"Entity {entity_id} is missing incorporation_jurisdiction."
            )

        addresses = entity.get("addresses") or []
        if not addresses:
            raise ValueError(f"Entity {entity_id} has no addresses.")
        defaults = [a for a in addresses if a.get("default")]
        if len(defaults) != 1:
            raise ValueError(
                f"Entity {entity_id} must have exactly one default address, "
                f"found {len(defaults)}."
            )
        for address in addresses:
            if not address.get("id"):
                raise ValueError(f"Entity {entity_id} has an address with no id.")
            if not address.get("lines"):
                raise ValueError(
                    f"Entity {entity_id} address {address.get('id')} has no lines."
                )

        law = entity.get("governing_law") or {}
        if not law.get("playbook_option_id"):
            raise ValueError(
                f"Entity {entity_id} is missing governing_law.playbook_option_id."
            )


def validate_registry_against_playbook(playbook: Mapping[str, Any]) -> None:
    """Validate the registry, then check the law mapping against ``playbook``.

    Every entity's ``governing_law.playbook_option_id`` must exist among the
    playbook's ``governing_law`` ``approved_options`` ids, AND each bundle's cached
    ``governing_law.label`` must equal that approved option's playbook label.
    Raises ``ValueError`` if the playbook has drifted away from a position a bundle
    relies on, or if a bundle's display label has drifted from the playbook's
    (the label is display-only -- the operative value already comes from the
    playbook via the id -- but asserting it here keeps the duplicated copy honest).
    """

    validate_registry()

    options = _playbook_governing_law_options(playbook)
    if not options:
        raise ValueError(
            "Playbook has no governing_law approved_options to map entities to."
        )

    option_ids = {option["id"] for option in options}
    # Map each approved-option id to its playbook label so the cached
    # governing_law.label on every bundle can be asserted against the single
    # source of truth. The operative governing-law VALUE in generated docs is
    # already pulled from the playbook via the option id, so the cached label is
    # display-only -- but if it silently drifts from the playbook (an option
    # renamed without updating the bundle) the UI would show a stale jurisdiction
    # name. Asserting equality here fails loudly instead, closing that drift.
    option_label_by_id = {option["id"]: option["label"] for option in options}

    for entity in SIGNING_ENTITIES:
        option_id = entity["governing_law"]["playbook_option_id"]
        if option_id not in option_ids:
            raise ValueError(
                f"Entity {entity['id']} maps to governing-law position "
                f"'{option_id}', which is not an approved playbook option "
                f"(have: {sorted(option_ids)})."
            )
        cached_label = str(entity["governing_law"].get("label") or "")
        playbook_label = option_label_by_id[option_id]
        if cached_label != playbook_label:
            raise ValueError(
                f"Entity {entity['id']} caches governing-law label "
                f"'{cached_label}', which has drifted from the playbook label "
                f"'{playbook_label}' for option '{option_id}'. Update the bundle's "
                f"governing_law.label to match the playbook."
            )


def entity_law_mapping(playbook: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return the entity -> governing-law mapping for reporting/UI.

    When ``playbook`` is supplied, each row carries ``matches_playbook`` so a
    caller can surface drift. ``flag`` carries any human note from
    :data:`ENTITY_LAW_MAPPING_NOTES`.
    """

    option_ids: set[str] = (
        _playbook_governing_law_option_ids(playbook) if playbook is not None else set()
    )
    rows: list[dict[str, Any]] = []
    for entity in SIGNING_ENTITIES:
        option_id = entity["governing_law"]["playbook_option_id"]
        row: dict[str, Any] = {
            "entity_id": entity["id"],
            "legal_name": entity["legal_name"],
            "playbook_option_id": option_id,
            "law_label": entity["governing_law"].get("label"),
            "flag": ENTITY_LAW_MAPPING_NOTES.get(entity["id"]),
        }
        if playbook is not None:
            row["matches_playbook"] = option_id in option_ids
        rows.append(row)
    return rows


def signing_entities_payload(playbook: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the API payload the draft-intake UI consumes.

    Returns the entity bundles, the entity -> governing-law mapping (with
    ``matches_playbook`` drift flags when ``playbook`` is supplied), the live
    set of playbook governing-law option ids, and the ``governing_law_options``
    ({id,label}) list — both sourced from the playbook's governing_law
    approved_options so the UI's override dropdown can be playbook-driven rather
    than derived from the embedded entity mirror.

    When the playbook is supplied and carries a usable term cap, the payload also
    exposes ``playbook_meta.max_term_years`` sourced live from the playbook's
    ``term_and_survival`` clause, so the Generator's term cap is playbook-driven
    rather than a hardcoded duplicate. The field is omitted (not invented) when
    the cap is missing or malformed; the frontend keeps its own fallback.
    """

    option_ids = (
        sorted(_playbook_governing_law_option_ids(playbook))
        if playbook is not None
        else []
    )
    payload: dict[str, Any] = {
        "entities": list_entities(),
        "law_mapping": entity_law_mapping(playbook),
        "playbook_option_ids": option_ids,
        # The governing-law dropdown choices ({id,label}) sourced live from the
        # playbook's governing_law approved_options, so the draft-intake override
        # dropdown is playbook-driven rather than derived from the embedded entity
        # mirror. Empty when no playbook is supplied; the frontend then falls back
        # to its entity-derived options.
        "governing_law_options": (
            _playbook_governing_law_options(playbook) if playbook is not None else []
        ),
    }
    max_term_years = (
        _playbook_max_term_years(playbook) if playbook is not None else None
    )
    if max_term_years is not None:
        payload["playbook_meta"] = {"max_term_years": max_term_years}
    return payload


__all__ = [
    "SIGNING_ENTITIES",
    "ENTITY_LAW_MAPPING_NOTES",
    "list_entities",
    "get_entity",
    "default_address",
    "actor_slug",
    "validate_registry",
    "validate_registry_against_playbook",
    "entity_law_mapping",
    "signing_entities_payload",
    "_playbook_max_term_years",
]
