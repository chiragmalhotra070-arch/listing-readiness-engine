"""Customer-aware intake -- resolve or create the seller's customer.

Covers ``POST /listing-files`` resolution end to end: the EXISTING /
NEW / NEEDS_REVIEW outcomes, the no-seller skip, idempotent replays that
never re-resolve, the review-queue + audit surfacing of ambiguity, and
the whole chain (file -> customer -> twelve CA requirements -> reconcile
received-vs-pending) on a customer-linked file.
"""

from __future__ import annotations

import re

from app.db.models import AuditEvent, Customer, ListingFile, Requirement
from tests.test_listing_api import CA_FACTS, _create_listing_file, _ingest_demo_documents

CUSTOMER_KEY_PATTERN = re.compile(r"^CUST-\d{6}$")


def _create(client, *, seller_name: str | None = "Jane Seller", idempotency_key: str | None = None):
    body: dict = {
        "property_address": "123 Main St, Pasadena, CA 91101",
        "apn": "5842-018-024",
        "property_attributes": CA_FACTS,
    }
    if seller_name is not None:
        body["seller_name"] = seller_name
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    return client.post("/v1/listing-files", json=body)


def test_new_seller_creates_one_customer_and_links_the_file(client, db):
    response = _create(client, seller_name="Zed Seller")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["customer_resolution"] == "NEW_CUSTOMER"
    assert CUSTOMER_KEY_PATTERN.match(body["customer_id"]), body["customer_id"]

    customer = db.query(Customer).filter(Customer.customer_name == "Zed Seller").one()
    assert customer.customer_id == body["customer_id"]
    assert customer.customer_name == "Zed Seller"

    listing_file = db.get(ListingFile, body["id"])
    assert listing_file.customer_id == customer.id
    assert listing_file.customer_resolution == "NEW_CUSTOMER"


def test_existing_seller_matches_case_insensitively_without_new_customer(client, db):
    db.add(Customer(customer_id="CUST-000777", customer_name="Jane Seller"))
    db.commit()

    response = _create(client, seller_name="jane seller")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["customer_resolution"] == "EXISTING_CUSTOMER"
    assert body["customer_id"] == "CUST-000777"

    assert db.query(Customer).filter(Customer.customer_id == "CUST-000777").count() == 1
    listing_file = db.get(ListingFile, body["id"])
    assert listing_file.customer_id is not None
    assert listing_file.customer_resolution == "EXISTING_CUSTOMER"


def test_name_matching_two_customers_needs_review_without_creating_anything(client, db):
    db.add(Customer(customer_id="CUST-000101", customer_name="Jane Seller"))
    db.add(Customer(customer_id="CUST-000102", customer_name="Jane Seller"))
    db.commit()

    response = _create(client, seller_name="Jane Seller")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["customer_resolution"] == "NEEDS_REVIEW"
    assert body["customer_id"] is None

    assert db.query(Customer).filter(Customer.customer_name == "Jane Seller").count() == 2
    listing_file = db.get(ListingFile, body["id"])
    assert listing_file.customer_id is None
    assert listing_file.customer_resolution == "NEEDS_REVIEW"

    audit = db.query(AuditEvent).filter(AuditEvent.event_type == "CUSTOMER_RESOLUTION").one()
    assert audit.status == "NEEDS_REVIEW"
    assert audit.details["listing_file_id"] == body["id"]
    assert audit.details["seller_name"] == "Jane Seller"

    queue = client.get(f"/v1/listing-files/{body['id']}/review-queue").json()
    assert queue["customer_needs_review"] == [
        {"listing_file_id": body["id"], "seller_name": "Jane Seller"}
    ]


def test_idempotent_replay_returns_stored_resolution_without_re_resolving(client, db):
    first = _create(client, seller_name="Replay Seller", idempotency_key="customer-replay-1")
    assert first.status_code == 201, first.text
    first_body = first.json()
    assert first_body["customer_resolution"] == "NEW_CUSTOMER"
    assert db.query(Customer).filter(Customer.customer_name == "Replay Seller").count() == 1

    second = _create(client, seller_name="Replay Seller", idempotency_key="customer-replay-1")

    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["id"] == first_body["id"]
    assert second_body["customer_id"] == first_body["customer_id"]
    assert second_body["customer_resolution"] == "NEW_CUSTOMER"
    assert db.query(Customer).filter(Customer.customer_name == "Replay Seller").count() == 1


def test_missing_seller_skips_resolution(client, db):
    response = _create(client, seller_name=None)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["customer_id"] is None
    assert body["customer_resolution"] is None

    # conftest seeds 5 fixture customers; without a seller none are added.
    assert db.query(Customer).count() == 5
    listing_file = db.get(ListingFile, body["id"])
    assert listing_file.customer_id is None
    assert listing_file.customer_resolution is None


def test_chain_file_to_customer_to_requirements_to_reconcile(client, db):
    created = _create(client, seller_name="Chain Seller")
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["customer_resolution"] == "NEW_CUSTOMER"
    customer = db.get(Customer, db.get(ListingFile, body["id"]).customer_id)
    assert customer.customer_id == body["customer_id"]

    generated = client.post(f"/v1/listing-files/{body['id']}/generate-requirements")
    assert generated.status_code == 200, generated.text
    requirements = db.query(Requirement).filter(Requirement.listing_file_id == body["id"]).all()
    assert len(requirements) == 12

    _ingest_demo_documents(client, body["id"], ["demo-ca-tds.pdf"])
    reconciled = client.post(f"/v1/listing-files/{body['id']}/reconcile")
    assert reconciled.status_code == 200, reconciled.text
    report = reconciled.json()

    assert report["requirements_moved_to_received"] == ["ca_tds"]
    assert len(report["unmet_requirement_keys"]) == 11
    received = db.query(Requirement).filter(
        Requirement.listing_file_id == body["id"], Requirement.state == "RECEIVED"
    ).all()
    assert [requirement.requirement_key for requirement in received] == ["ca_tds"]
    pending = db.query(Requirement).filter(
        Requirement.listing_file_id == body["id"], Requirement.state == "PENDING"
    ).count()
    assert pending == 11

    listing_file = db.get(ListingFile, body["id"])
    assert listing_file.customer_id == customer.id
    assert listing_file.customer_resolution == "NEW_CUSTOMER"
