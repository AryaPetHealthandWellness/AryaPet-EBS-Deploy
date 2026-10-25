import json
import uuid
from types import SimpleNamespace

import pytest

from app.db.models.document import DocumentStatus
from app.workers import processing_consumer
from app.workers.processing_consumer import _process_document, s3_keys_from_event


def _s3_event(key: str, event_name: str = "ObjectCreated:Put") -> dict:
    # Shape copied from a real notification the dev worker received.
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": event_name,
                "s3": {
                    "bucket": {"name": "arya-dev-documents-188620291813"},
                    "object": {"key": key, "size": 209098},
                },
            }
        ]
    }


# ── s3_keys_from_event ──────────────────────────────────────────────────────


def test_extracts_the_object_key_from_an_upload_event():
    key = "f9463f12-d85c-4601-a69f-4424f104f30a/ea610c10-Daisy_LFT_KFT.pdf"
    assert s3_keys_from_event(_s3_event(key)) == [key]


def test_decodes_the_url_encoded_key_s3_sends():
    # S3 encodes spaces as "+" in notifications; the Document row stores the raw key,
    # so an undecoded key would never match and the document would never process.
    assert s3_keys_from_event(_s3_event("user/abc-my+lab+report.pdf")) == [
        "user/abc-my lab report.pdf"
    ]
    assert s3_keys_from_event(_s3_event("user/abc-a%2Bb.pdf")) == ["user/abc-a+b.pdf"]


def test_ignores_the_test_event_s3_sends_on_setup():
    body = {"Service": "Amazon S3", "Event": "s3:TestEvent", "Bucket": "arya-dev-documents"}
    assert s3_keys_from_event(body) == []


def test_ignores_non_create_events():
    assert s3_keys_from_event(_s3_event("user/x.pdf", "ObjectRemoved:Delete")) == []


def test_ignores_records_from_other_sources():
    body = {"Records": [{"eventSource": "aws:sns", "eventName": "ObjectCreated:Put"}]}
    assert s3_keys_from_event(body) == []


def test_ignores_a_legacy_document_id_message():
    assert s3_keys_from_event({"document_id": str(uuid.uuid4())}) == []


# ── _process_document ───────────────────────────────────────────────────────


class _Session:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _document(s3_key: str, raw_text: str | None = None, status=DocumentStatus.PENDING):
    return SimpleNamespace(
        id=uuid.uuid4(),
        s3_key=s3_key,
        raw_text=raw_text,
        status=status,
        parsed_result=None,
        processed_at=None,
        failure_reason=None,
    )


_SETTINGS = SimpleNamespace(documents_bucket="bucket", aws_region="ap-south-1")
_PDF_CARD = {"document_type": "lab_report", "parameters": []}


@pytest.mark.asyncio
async def test_pdf_with_a_table_completes_without_any_client_text(monkeypatch):
    # The whole point of triggering off the upload: a PDF needs nothing from the app.
    async def fake_fetch(*_):
        return b"%PDF"

    monkeypatch.setattr(processing_consumer, "_fetch_pdf_bytes", fake_fetch)
    monkeypatch.setattr(processing_consumer, "build_record_from_pdf", lambda *_: _PDF_CARD)

    document = _document("user/report.pdf", raw_text=None)
    session = _Session()

    assert await _process_document(document, session, _SETTINGS) is True
    assert document.status == DocumentStatus.COMPLETED
    assert document.parsed_result == _PDF_CARD


@pytest.mark.asyncio
async def test_photo_without_text_waits_for_redelivery():
    # The S3 event usually beats the client's text submission for a photo. Recording
    # an empty result here would mark it done with nothing in it.
    document = _document("user/photo.jpg", raw_text=None)
    session = _Session()

    assert await _process_document(document, session, _SETTINGS) is False
    assert document.status == DocumentStatus.PENDING
    assert document.parsed_result is None
    assert session.commits == 0


@pytest.mark.asyncio
async def test_photo_with_text_completes_from_the_text(monkeypatch):
    monkeypatch.setattr(
        processing_consumer, "build_record_from_text", lambda text: {"document_type": "lab_report"}
    )
    document = _document("user/photo.jpg", raw_text="LIVER FUNCTION TEST ...")
    session = _Session()

    assert await _process_document(document, session, _SETTINGS) is True
    assert document.status == DocumentStatus.COMPLETED
    assert document.parsed_result == {"document_type": "lab_report"}


@pytest.mark.asyncio
async def test_pdf_hands_the_client_text_over_as_a_fallback(monkeypatch):
    # The PDF's own text layer is used first (inside build_record_from_pdf); the
    # client's OCR text is passed along for scanned PDFs that have none.
    async def fake_fetch(*_):
        return b"%PDF"

    seen = {}

    def fake_build(pdf_bytes, fallback_text):
        seen["fallback_text"] = fallback_text
        return {"document_type": "vet_visit"}

    monkeypatch.setattr(processing_consumer, "_fetch_pdf_bytes", fake_fetch)
    monkeypatch.setattr(processing_consumer, "build_record_from_pdf", fake_build)

    document = _document("user/letter.pdf", raw_text="Diagnosis: otitis externa.")
    assert await _process_document(document, _Session(), _SETTINGS) is True
    assert seen["fallback_text"] == "Diagnosis: otitis externa."
    assert document.parsed_result["document_type"] == "vet_visit"


@pytest.mark.asyncio
async def test_scanned_pdf_without_text_waits_for_redelivery(monkeypatch):
    # No text layer and no table: nothing to work from until the client's OCR arrives.
    async def fake_fetch(*_):
        return b"%PDF"

    monkeypatch.setattr(processing_consumer, "_fetch_pdf_bytes", fake_fetch)
    monkeypatch.setattr(processing_consumer, "build_record_from_pdf", lambda *_: None)

    document = _document("user/scan.pdf", raw_text=None)
    assert await _process_document(document, _Session(), _SETTINGS) is False
    assert document.status == DocumentStatus.PENDING


@pytest.mark.asyncio
async def test_already_completed_document_is_not_reprocessed(monkeypatch):
    # A redelivered event arriving after the document finished must not redo the work
    # or overwrite the stored result.
    def must_not_run(*_):
        raise AssertionError("reprocessed a completed document")

    monkeypatch.setattr(processing_consumer, "build_record_from_pdf", must_not_run)
    document = _document("user/report.pdf", status=DocumentStatus.COMPLETED)
    document.parsed_result = {"kept": True}

    assert await _process_document(document, _Session(), _SETTINGS) is True
    assert document.parsed_result == {"kept": True}


@pytest.mark.asyncio
async def test_a_failing_pdf_still_uses_text_when_present(monkeypatch):
    async def broken_fetch(*_):
        raise RuntimeError("NoSuchKey")

    monkeypatch.setattr(processing_consumer, "_fetch_pdf_bytes", broken_fetch)
    monkeypatch.setattr(
        processing_consumer, "build_record_from_text", lambda text: {"document_type": "lab_report"}
    )
    document = _document("user/report.pdf", raw_text="some text")

    assert await _process_document(document, _Session(), _SETTINGS) is True
    assert document.status == DocumentStatus.COMPLETED


def test_the_real_logged_event_parses():
    # Verbatim body the dev worker logged and discarded as "no document_id" before this
    # change - the exact message that was being thrown away.
    body = json.loads(
        '{"Records": [{"eventVersion": "2.6", "eventSource": "aws:s3", '
        '"awsRegion": "ap-south-1", "eventName": "ObjectCreated:Put", '
        '"s3": {"bucket": {"name": "arya-dev-documents-188620291813"}, '
        '"object": {"key": "f9463f12-d85c-4601-a69f-4424f104f30a/'
        'ea610c10-7d69-4a18-94e3-de1a16b3a842-Daisy_LFT_KFT.pdf", "size": 209098}}}]}'
    )
    assert s3_keys_from_event(body) == [
        "f9463f12-d85c-4601-a69f-4424f104f30a/"
        "ea610c10-7d69-4a18-94e3-de1a16b3a842-Daisy_LFT_KFT.pdf"
    ]
