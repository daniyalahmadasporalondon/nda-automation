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

    for clause in playbook.get("clauses", []):
        if clause.get("id") != "governing_law":
            continue
        rules = clause.get("rules") or {}
        options = rules.get("approved_options") or []
        return {
            str(option.get("id"))
            for option in options
            if isinstance(option, Mapping) and option.get("id")
        }
    return set()


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
    playbook's ``governing_law`` ``approved_options`` ids. Raises ``ValueError``
    if the playbook has drifted away from a position a bundle relies on.
    """

    validate_registry()

    option_ids = _playbook_governing_law_option_ids(playbook)
    if not option_ids:
        raise ValueError(
            "Playbook has no governing_law approved_options to map entities to."
        )

    for entity in SIGNING_ENTITIES:
        option_id = entity["governing_law"]["playbook_option_id"]
        if option_id not in option_ids:
            raise ValueError(
                f"Entity {entity['id']} maps to governing-law position "
                f"'{option_id}', which is not an approved playbook option "
                f"(have: {sorted(option_ids)})."
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
    ``matches_playbook`` drift flags when ``playbook`` is supplied), and the live
    set of playbook governing-law option ids so the UI's override dropdown can be
    playbook-driven rather than hardcoded.
    """

    option_ids = (
        sorted(_playbook_governing_law_option_ids(playbook))
        if playbook is not None
        else []
    )
    return {
        "entities": list_entities(),
        "law_mapping": entity_law_mapping(playbook),
        "playbook_option_ids": option_ids,
    }


__all__ = [
    "SIGNING_ENTITIES",
    "ENTITY_LAW_MAPPING_NOTES",
    "list_entities",
    "get_entity",
    "default_address",
    "validate_registry",
    "validate_registry_against_playbook",
    "entity_law_mapping",
    "signing_entities_payload",
]
