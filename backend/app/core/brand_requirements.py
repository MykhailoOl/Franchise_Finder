from copy import deepcopy

REQUIREMENTS_VERSION = "2026-11-01"
REVIEWED_AT = "2026-11-01"
SOURCE_SHA256 = {
    "zabka-property": "8046bbfa797fa6c9a8b118cb971657c3a099ae9832a5f59c82d91c26336099b5",
    "zabka-franchise": "5f7b52b9da33957409f53fc75d1eae152c92d7d45e3faabccfef53e2d875c892",
    "lewiatan-conditions": "0194c4a292f919bd68495f4487340e86c53328cafb8da87237db81413d7a910c",
    "da-grasso-offer": "4939e72d3617907c550eb16a7b4473ae5e91b05078c0dd7f64f32d1ee1c46bc6",
    "dominos-franchise": "303c4f6e4af6eeb3b99dc8bf6945ab08b343d800f7f055967fbb31d16042b8da",
    "mcdonalds-faq": "c41a3f17ca02ee8a30b2a63219bb0fd2a798d52f1e4af6044e8085f160ef1b63",
    "yasumi-franchise": "c5ab4453870b8425c3791374663e5cca1c972b13db6188302aa74e5f59d9f349",
    "depilconcept-franchise": "207aafe1e48f8860c7409257ee2d932574561875f07e9c611f05acfb20a3617e",
    "subway-official-poland": "ee46fed0f2813f56210c4bcc009cc350ac916a84abf8638df6cc3e77e4b96645",
    "depilconcept-premises": "7db3afd49a560a970aecfa1fd925e432b91c89e555d9d9c9a5080b5c256da27a",
}


def source(identifier: str, url: str, retrieved_at: str) -> dict:
    return {
        "id": identifier,
        "url": url,
        "retrieved_at": retrieved_at,
        "published_at": None,
        "publisher_type": "brand_official",
        "reviewed_sha256": SOURCE_SHA256[identifier],
    }


def requirement(
    identifier: str,
    label: str,
    field: str,
    operator: str,
    value: float | bool | list[int] | None,
    unit: str | None,
    strength: str,
    source_id: str,
    evidence_note: str,
) -> dict:
    return {
        "id": identifier,
        "label": label,
        "field": field,
        "operator": operator,
        "value": value,
        "unit": unit,
        "strength": strength,
        "source_id": source_id,
        "evidence_note": evidence_note,
        "public_building_evaluable": False,
        "evidence_status": "verified" if strength != "unknown" else "unknown",
    }


REQUIREMENTS_BY_BRAND = {
    "zabka": {
        "brand_id": "zabka",
        "reviewed_at": REVIEWED_AT,
        "sources": [
            source(
                "zabka-property",
                "https://www.zabka.pl/wynajem-lokali/",
                "2026-11-01T17:21:29.343482+00:00",
            ),
            source(
                "zabka-franchise",
                "https://www.zabka.pl/franczyza/",
                "2026-11-01T17:21:29.371777+00:00",
            ),
        ],
        "formats": [
            {
                "id": "standard-store",
                "name": "Standard store property proposal",
                "requirements": [
                    requirement(
                        "total-area",
                        "Total premises area above 70 m²",
                        "unit_total_area_m2",
                        "gt",
                        70,
                        "m2",
                        "mandatory",
                        "zabka-property",
                        "The property criteria specify total unit area greater than 70 m².",
                    ),
                    requirement(
                        "sales-area",
                        "Salesroom area above 50 m²",
                        "sales_area_m2",
                        "gt",
                        50,
                        "m2",
                        "mandatory",
                        "zabka-property",
                        "The salesroom threshold is separate from total premises area.",
                    ),
                    requirement(
                        "entrance",
                        "Separate ground-floor entrance or feasible adaptation",
                        "independent_ground_floor_entrance_or_adaptable",
                        "eq",
                        True,
                        None,
                        "mandatory",
                        "zabka-property",
                        "An independent entrance on the ground floor must exist or be feasible.",
                    ),
                    requirement(
                        "salesroom-shape",
                        "Rectangular salesroom preferred",
                        "rectangular_salesroom",
                        "eq",
                        True,
                        None,
                        "preferred",
                        "zabka-property",
                        "The page describes the rectangular shape as a preference.",
                    ),
                    requirement(
                        "site-selection",
                        "Location assigned by the chain",
                        "chain_location_assignment",
                        "manual_review",
                        None,
                        None,
                        "approval",
                        "zabka-franchise",
                        "Żabka selects the trading premises and accepts location suggestions; "
                        "a proposed footprint is not an available franchise offer.",
                    ),
                ],
            }
        ],
        "notes": [
            "The introductory 70–120 m² search range is not treated as a hard upper limit.",
            "These are standard store property criteria, not Żabka Nano requirements.",
        ],
    },
    "lewiatan": {
        "brand_id": "lewiatan",
        "reviewed_at": REVIEWED_AT,
        "sources": [
            source(
                "lewiatan-conditions",
                "https://www.lewiatan.pl/franczyza/warunki-wspolpracy",
                "2026-11-01T17:21:29.647590+00:00",
            )
        ],
        "formats": [
            {
                "id": "retail-store",
                "name": "Retail store joining the network",
                "requirements": [
                    requirement(
                        "retail-location",
                        "Retail premises in a suitable location",
                        "suitable_retail_premises",
                        "manual_review",
                        None,
                        None,
                        "mandatory",
                        "lewiatan-conditions",
                        "The published conditions require a retail outlet with a suitable "
                        "location; no numerical location threshold is supplied.",
                    ),
                    requirement(
                        "unit-area",
                        "Unit-area threshold not published on reviewed page",
                        "unit_total_area_m2",
                        "not_published",
                        None,
                        "m2",
                        "unknown",
                        "lewiatan-conditions",
                        "No universal unit-area minimum was found in "
                        "the reviewed national cooperation conditions.",
                    ),
                ],
            }
        ],
        "notes": [
            "Regional-company agreement and operator conditions are separate from site facts."
        ],
    },
    "da-grasso": {
        "brand_id": "da-grasso",
        "reviewed_at": REVIEWED_AT,
        "sources": [
            source(
                "da-grasso-offer",
                "https://www.dagrassofranczyza.pl/oferta.html",
                "2026-11-01T17:21:29.349051+00:00",
            )
        ],
        "formats": [
            {
                "id": "express",
                "name": "Express: delivery and collection",
                "requirements": [
                    requirement(
                        "unit-area",
                        "Published format area: 65–100 m²",
                        "unit_total_area_m2",
                        "range",
                        [65, 100],
                        "m2",
                        "format_range",
                        "da-grasso-offer",
                        "Express is described as a smaller unit for "
                        "delivery and collection without a dining room.",
                    ),
                    requirement(
                        "technical-audit",
                        "Electrical and ventilation assessment",
                        "electrical_and_ventilation_suitability",
                        "manual_review",
                        None,
                        None,
                        "approval",
                        "da-grasso-offer",
                        "The chain checks electricity and ventilation feasibility "
                        "through a technical audit; no numerical capacity is published.",
                    ),
                ],
            },
            {
                "id": "classic",
                "name": "Classic restaurant: dining, delivery and collection",
                "requirements": [
                    requirement(
                        "unit-area",
                        "Published format area: 100–200 m²",
                        "unit_total_area_m2",
                        "range",
                        [100, 200],
                        "m2",
                        "format_range",
                        "da-grasso-offer",
                        "The classic format includes a dining room, delivery and collection.",
                    ),
                    requirement(
                        "technical-audit",
                        "Electrical and ventilation assessment",
                        "electrical_and_ventilation_suitability",
                        "manual_review",
                        None,
                        None,
                        "approval",
                        "da-grasso-offer",
                        "Technical suitability is checked individually by the chain.",
                    ),
                    requirement(
                        "parking",
                        "Parking is an additional advantage",
                        "parking_available",
                        "eq",
                        True,
                        None,
                        "preferred",
                        "da-grasso-offer",
                        "Parking and a seasonal garden are advantages, "
                        "not published mandatory conditions.",
                    ),
                ],
            },
        ],
        "notes": [
            "The main dagrasso.pl franchise link redirects to this official franchise site.",
            "Published format ranges are not interpreted as universal hard rejection thresholds.",
            "Older material describing a universal 100 m² minimum does not override these formats.",
        ],
    },
    "dominos": {
        "brand_id": "dominos",
        "reviewed_at": REVIEWED_AT,
        "sources": [
            source(
                "dominos-franchise",
                "https://www.dominospizza.pl/franczyza",
                "2026-11-01T17:21:29.538902+00:00",
            )
        ],
        "formats": [
            {
                "id": "unspecified-pizzeria",
                "name": "Pizzeria; format not specified publicly",
                "requirements": [
                    requirement(
                        "unit-area",
                        "Unit-area threshold not published on reviewed page",
                        "unit_total_area_m2",
                        "not_published",
                        None,
                        "m2",
                        "unknown",
                        "dominos-franchise",
                        "The reviewed Polish franchise page supplies "
                        "no numerical unit-area threshold.",
                    ),
                    requirement(
                        "technical-standard",
                        "Technical premises specification unknown",
                        "technical_premises_specification",
                        "not_published",
                        None,
                        None,
                        "unknown",
                        "dominos-franchise",
                        "Detailed premises requirements "
                        "are not given in the reviewed public offer.",
                    ),
                ],
            }
        ],
        "notes": [
            "The page welcomes applicants who already have premises; ownership is not mandatory.",
            "The indicative information uses historical data; the page invites negotiation.",
        ],
    },
    "mcdonalds": {
        "brand_id": "mcdonalds",
        "reviewed_at": REVIEWED_AT,
        "sources": [
            source(
                "mcdonalds-faq",
                "https://mcdonalds.pl/o-mcdonalds/faq/",
                "2026-11-01T17:21:29.661292+00:00",
            )
        ],
        "formats": [
            {
                "id": "chain-selected-restaurant",
                "name": "Chain-selected restaurant",
                "requirements": [
                    requirement(
                        "site-selection",
                        "Location sourced and selected by McDonald's",
                        "chain_location_assignment",
                        "manual_review",
                        None,
                        None,
                        "approval",
                        "mcdonalds-faq",
                        "The development department sources "
                        "restaurant properties; a franchise applicant is not required "
                        "to supply premises.",
                    ),
                    requirement(
                        "unit-area",
                        "Format-specific area requirement not published here",
                        "unit_total_area_m2",
                        "not_published",
                        None,
                        "m2",
                        "unknown",
                        "mcdonalds-faq",
                        "The FAQ does not publish a universal area threshold; "
                        "a new-restaurant press release is not a premises specification.",
                    ),
                ],
            }
        ],
        "notes": [
            "A map location is not confirmation that the chain will offer a franchise there."
        ],
    },
    "subway": {
        "brand_id": "subway",
        "reviewed_at": REVIEWED_AT,
        "sources": [
            source(
                "subway-official-poland", "https://mysubway.pl/", "2026-11-01T17:23:52.878526+00:00"
            )
        ],
        "formats": [
            {
                "id": "poland-unspecified",
                "name": "Poland format not verified",
                "requirements": [
                    requirement(
                        "unit-area",
                        "Current Polish unit-area threshold unverified",
                        "unit_total_area_m2",
                        "not_verified",
                        None,
                        "m2",
                        "unknown",
                        "subway-official-poland",
                        "The official Polish website links to "
                        "wlasnysubway.pl; its redirected franchise target could not be "
                        "verified during this review.",
                    ),
                    requirement(
                        "technical-standard",
                        "Current Polish technical specification unverified",
                        "technical_premises_specification",
                        "not_verified",
                        None,
                        None,
                        "unknown",
                        "subway-official-poland",
                        "No numerical requirement "
                        "is transferred from another country or an old brochure.",
                    ),
                ],
            }
        ],
        "notes": [
            "Unknown means current Polish requirements were not verified, not that none exist.",
            "The official linked franchise target redirected to subway-franchise.de/en.",
        ],
    },
    "yasumi": {
        "brand_id": "yasumi",
        "reviewed_at": REVIEWED_AT,
        "sources": [
            source(
                "yasumi-franchise",
                "https://yasumi.pl/franczyza/",
                "2026-11-01T17:21:30.800106+00:00",
            )
        ],
        "formats": [
            {
                "id": "institute",
                "name": "Instytut Zdrowia i Urody",
                "requirements": [
                    requirement(
                        "unit-area",
                        "Premises area at least 100 m²",
                        "unit_total_area_m2",
                        "gte",
                        100,
                        "m2",
                        "mandatory",
                        "yasumi-franchise",
                        "The Institute format specifies at least "
                        "100 m² of owned or rented premises.",
                    ),
                    requirement(
                        "treatment-rooms",
                        "Capacity for at least four treatment rooms",
                        "treatment_room_capacity",
                        "gte",
                        4,
                        "rooms",
                        "mandatory",
                        "yasumi-franchise",
                        "The unit must allow at least four treatment rooms to be separated.",
                    ),
                    requirement(
                        "town-size",
                        "Mainly intended for towns above 50,000 residents",
                        "town_population",
                        "gt",
                        50000,
                        "people",
                        "preferred",
                        "yasumi-franchise",
                        "This is a town-level target market, "
                        "not a population threshold within a building catchment.",
                    ),
                    requirement(
                        "territory",
                        "Territorial exclusivity requires verification",
                        "territory_available",
                        "manual_review",
                        None,
                        None,
                        "approval",
                        "yasumi-franchise",
                        "The brand describes exclusive areas but "
                        "does not publish their boundaries in the reviewed page.",
                    ),
                ],
            },
            {
                "id": "beauty-point",
                "name": "Beauty Point",
                "requirements": [
                    requirement(
                        "town-size",
                        "Intended for towns up to 30,000 residents",
                        "town_population",
                        "lte",
                        30000,
                        "people",
                        "preferred",
                        "yasumi-franchise",
                        "The public format positioning targets "
                        "smaller towns; this is not a catchment-population condition.",
                    ),
                    requirement(
                        "unit-area",
                        "Beauty Point unit-area minimum not published here",
                        "unit_total_area_m2",
                        "not_published",
                        None,
                        "m2",
                        "unknown",
                        "yasumi-franchise",
                        "The Institute's 100 m² condition is not "
                        "copied to the separate Beauty Point format.",
                    ),
                ],
            },
        ],
        "notes": [
            "Existing outlet records do not prove which franchise format applies to each site."
        ],
    },
    "depilconcept": {
        "brand_id": "depilconcept",
        "reviewed_at": REVIEWED_AT,
        "sources": [
            source(
                "depilconcept-franchise",
                "https://depilconcept.pl/franczyza/",
                "2026-11-01T17:21:30.509413+00:00",
            ),
            source(
                "depilconcept-premises",
                "https://www.depilconcept-franczyza.pl/otwarcie-gabinetu-i-kwestie-lokalowe/",
                "2026-11-01T17:25:33.023672+00:00",
            ),
        ],
        "formats": [
            {
                "id": "salon",
                "name": "Depilation salon",
                "requirements": [
                    {
                        **requirement(
                            "unit-area",
                            "Official premises-area guidance differs",
                            "unit_total_area_m2",
                            "not_verified",
                            None,
                            "m2",
                            "unknown",
                            "depilconcept-franchise",
                            "The main page describes approximately "
                            "70 m²; the separate official FAQ gives 40–70 m². Neither page "
                            "establishes which guidance supersedes the other.",
                        ),
                        "evidence_status": "conflicting",
                        "reported_claims": [
                            {
                                "source_id": "depilconcept-franchise",
                                "operator": "approximate",
                                "value": 70,
                                "strength": "approximate",
                            },
                            {
                                "source_id": "depilconcept-premises",
                                "operator": "range",
                                "value": [40, 70],
                                "strength": "format_range",
                            },
                        ],
                    },
                    requirement(
                        "ground-floor-window",
                        "Ground floor with display window preferred",
                        "ground_floor_with_display_window",
                        "eq",
                        True,
                        None,
                        "preferred",
                        "depilconcept-franchise",
                        "Ground floor and windows are preferences.",
                    ),
                    requirement(
                        "shopping-mall",
                        "Published FAQ excludes premises inside malls",
                        "inside_shopping_mall",
                        "eq",
                        False,
                        None,
                        "mandatory",
                        "depilconcept-premises",
                        "The undated FAQ rules out premises inside "
                        "shopping malls while recommending locations nearby.",
                    ),
                    {
                        **requirement(
                            "town-size",
                            "Town-size boundary differs across official pages",
                            "town_population",
                            "not_verified",
                            None,
                            "people",
                            "unknown",
                            "depilconcept-franchise",
                            "Above 40,000 and at least 40,000 are "
                            "different boundaries. These describe the town, not a catchment.",
                        ),
                        "evidence_status": "conflicting",
                        "reported_claims": [
                            {
                                "source_id": "depilconcept-franchise",
                                "operator": "gt",
                                "value": 40000,
                                "strength": "mandatory",
                            },
                            {
                                "source_id": "depilconcept-premises",
                                "operator": "gte",
                                "value": 40000,
                                "strength": "mandatory",
                            },
                        ],
                    },
                ],
            }
        ],
        "notes": [
            "Both sites identify Brown Sugar Esthetical at Kamionka, Zaleśna 1J as franchisor; "
            "the separate site's footer uses franchising@depilconcept.pl.",
            "Publication dates and precedence are unverified; retrieval dates are distinct.",
            "The animated area counter targets 70 m² in HTML; intermediate values are not claims.",
            "Conflicting guidance requires clarification before any future eligibility decision.",
            "Promotional profit statements are not verified outcomes or model training labels.",
        ],
    },
}


def get_brand_requirements(brand_id: str) -> dict:
    return deepcopy(REQUIREMENTS_BY_BRAND[brand_id])
