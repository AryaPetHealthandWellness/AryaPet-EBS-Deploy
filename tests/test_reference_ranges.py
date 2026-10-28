"""Normal ranges per species (GET/POST /v1/reference-ranges), on in-memory SQLite."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1.reference_ranges import normalize_species, parameter_key
from app.auth.dependencies import get_current_db_user
from app.db.models.reference_range import SpeciesReferenceRange
from app.db.models.user import User
from app.db.session import get_db
from app.main import app


@pytest.fixture
def client():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def db():
        async with sessions() as session:
            yield session

    user = User(
        id=uuid.uuid4(), cognito_username="vet@example.com", cognito_sub="sub", email="vet@example.com"
    )

    async def create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(User.__table__.create)
            await conn.run_sync(SpeciesReferenceRange.__table__.create)
        async with sessions() as session:
            session.add(user)
            await session.commit()

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_db_user] = lambda: user
    with TestClient(app) as test_client:
        test_client.portal.call(create_tables)
        yield test_client
    app.dependency_overrides.clear()


def submit(client, species="Dog", source="Madhuroha Pet Care", **ranges):
    parameters = [{"name": n, "reference_range": r, "unit": u} for n, (r, u) in ranges.items()]
    body = {"species": species, "source": source, "parameters": parameters}
    return client.post("/v1/reference-ranges", json=body)


def ranges(client, species):
    response = client.get("/v1/reference-ranges", params={"species": species})
    assert response.status_code == 200
    return {r["parameter_name"]: (r["reference_range"], r["unit"]) for r in response.json()}


def test_ranges_are_saved_per_species(client):
    assert submit(client, ALB=("3.2-4.3", "g/dL"), GLU=("70-110", "mg/dL")).status_code == 200
    submit(client, species="Cat", ALB=("2.5-3.9", "g/dL"))
    assert ranges(client, "dog") == {"ALB": ("3.2-4.3", "g/dL"), "GLU": ("70-110", "mg/dL")}
    assert ranges(client, "Feline") == {"ALB": ("2.5-3.9", "g/dL")}


def test_the_first_printed_range_is_kept(client):
    submit(client, ALB=("3.2-4.3", "g/dL"))
    # Another lab prints Albumin differently - it fills nothing and changes nothing.
    body = submit(client, source="Other Lab", Albumin=("2.2-4.0", "g/dL")).json()
    assert [(r["parameter_name"], r["reference_range"], r["source"]) for r in body] == [
        ("ALB", "3.2-4.3", "Madhuroha Pet Care")
    ]


def test_ranges_without_a_number_are_ignored(client):
    submit(client, ALB=("see note", ""), TP=("6.4-8.5", "g/dL"))
    assert ranges(client, "dog") == {"TP": ("6.4-8.5", "g/dL")}


def test_species_and_parameter_keys():
    assert normalize_species(" Canine ") == "dog"
    assert normalize_species("Rabbit") == "rabbit"
    assert parameter_key("ALB") == parameter_key("Albumin")
    assert parameter_key("U/C") == "u/c"


def test_a_report_without_ranges_is_matched_by_name(client):
    submit(client, Albumin=("2.2-4.0", "g/dL"), GLU=("70-110", "mg/dL"))
    response = client.post(
        "/v1/reference-ranges/match", json={"species": "canine", "names": ["ALB", "GLU", "Unknown Test"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body) == ["ALB", "GLU"]
    assert (body["ALB"]["reference_range"], body["ALB"]["unit"]) == ("2.2-4.0", "g/dL")
    # Another species gets nothing.
    other = client.post("/v1/reference-ranges/match", json={"species": "cat", "names": ["ALB"]})
    assert other.json() == {}
