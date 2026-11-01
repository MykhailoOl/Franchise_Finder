MUNICIPAL_SOURCE_URL = "https://wms2.um.warszawa.pl/geoserver/wfs/wfs"
MUNICIPAL_RETRIEVED_AT = "2026-10-23T17:36:30.459927+00:00"
ALIASES = {
    ("dominos", "Kasprowicza"): (
        "ul. Kasprowicza",
        "119",
        "ulica Jana Kasprowicza",
        "b61c0942-af5c-4361-a6c2-301d133ee7fe",
    ),
    ("dominos", "Kondratowicza"): (
        "ul. Kondratowicza",
        "27a",
        "ulica Ludwika Kondratowicza",
        "96cd6733-70e9-4a76-a971-b35ad16095d5",
    ),
    ("dominos", "Slominskiego"): (
        "ul. Słomińskiego",
        "19",
        "ulica Zygmunta Słomińskiego",
        "cd819fbc-1214-45b4-9bae-a995e9d51f5a",
    ),
    ("dominos", "Reymonta"): (
        "ul. Reymonta",
        "12",
        "Aleja Władysława Reymonta",
        "dbedf137-dcbb-4094-b5e0-38b6c5e20700",
    ),
    ("dominos", "Taylora"): (
        "ul. Taylora",
        "2",
        "ulica Karola Taylora",
        "2cfa4718-2615-4056-872d-373e3453e499",
    ),
    ("dominos", "Skladkowskiego"): (
        "ul. Gen. Felicjana Składkowskiego",
        "4",
        "ulica gen. Felicjana Sławoja Składkowskiego",
        "5973c143-e3a4-463f-9325-5b4d9e9469fa",
    ),
    ("da-grasso", "76"): (
        "Taylora",
        "7",
        "ulica Karola Taylora",
        "29364b90-6001-4398-b858-9ea2bf17177e",
    ),
    ("da-grasso", "179"): (
        "Os Królewska",
        "18B",
        "ulica Oś Królewska",
        "798dbcff-75a7-4623-af2a-4dd4d0766db8",
    ),
}


def apply_municipal_aliases(records: list[dict]) -> None:
    for record in records:
        alias = ALIASES.get((record["brand_id"], record["source_id"]))
        if alias is None:
            continue
        street, number, canonical_street, identifier = alias
        address = record["address"]
        if address["street"] != street or address["house_number"] != number:
            raise ValueError(
                f"Reviewed municipal alias no longer matches outlet {record['source_id']}"
            )
        record["attributes"]["source_parsed_address"] = dict(address)
        record["attributes"]["address_normalization"] = {
            "method": "reviewed_municipal_street_alias",
            "source_url": MUNICIPAL_SOURCE_URL,
            "source_retrieved_at": MUNICIPAL_RETRIEVED_AT,
            "municipal_address_source_id": f"PL.ZIPIN.1469.EMUiA.{identifier}",
            "note": "Explicit full-name or spelling alias; raw address and house number preserved",
        }
        address["street"] = canonical_street
        if record["brand_id"] == "dominos" and record["source_id"] == "Reymonta":
            address["postal_code"] = "01-842"
            record["attributes"]["address_normalization"].update(
                {
                    "additional_source_url": (
                        "https://www.dominospizza.pl/lokale/Warszawa/Reymonta"
                    ),
                    "additional_source_capture": "sources/dominos-reymonta-browser.txt",
                    "note": (
                        "Official outlet detail identifies northwestern Warsaw parks; this "
                        "disambiguates the municipal Aleja Władysława Reymonta 12, 01-842 "
                        "from ulica Władysława Reymonta 12, 05-075 in Wesoła. "
                        "Postal code is municipal, not supplied by the locator."
                    ),
                }
            )
