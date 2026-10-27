"""Long-polls the processing SQS queue and classifies/extracts info from uploaded
documents.

The queue is fed by the documents bucket itself: Terraform wires an
`s3:ObjectCreated:*` notification to it (Arya-Infra modules/storage), so a message
arrives the moment a file lands in S3. The API has no permission to send to this
queue and doesn't need one - uploads are the trigger.

That timing suits the kinds of document differently:

  CSV/TSV exports (an analyzer's "UserReport_*.csv") are read straight from the
  file's own columns (csv_report), needing nothing from the client.

  PDFs are processed straight away. Their tables are read from the file's own
  geometry and their wording from the file's own text layer (pdf_table_extractor),
  which needs nothing from the client. Only a scanned PDF with no text layer waits
  for the client's OCR text, like a photo.

  Photos have no geometry, only the OCR text the client submits afterwards via
  POST /documents/{id}/text - which may not have arrived when the S3 event does.
  Those messages are left undeleted so SQS redelivers them after the visibility
  timeout, by which point the text is usually there. After maxReceiveCount they
  move to the `-dlq` queue, which is where a photo whose text never came ends up.

Runs as a standalone supervisord program on the same EB SingleInstance environment as
the API, not inside the FastAPI event loop.
"""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote_plus

import boto3
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import build_database_url, get_settings
from app.db.models.document import Document, DocumentStatus
from app.db.session import get_session_maker, init_engine
from app.services.csv_report import is_spreadsheet_report
from app.services.record_extractor import (
    build_record_from_pdf,
    build_record_from_spreadsheet,
    build_record_from_text,
)

logger = logging.getLogger(__name__)

WAIT_TIME_SECONDS = 20
MAX_MESSAGES = 10
VISIBILITY_TIMEOUT = 120


def s3_keys_from_event(body: dict[str, Any]) -> list[str]:
    """Object keys named by an S3 event notification.

    S3 URL-encodes keys in these events ("my report.pdf" arrives as
    "my+report.pdf"), so they're decoded here to match the key stored on the
    Document row. Returns [] for anything that isn't an object-created event,
    including the s3:TestEvent S3 sends when the notification is first set up.
    """
    keys: list[str] = []
    for record in body.get("Records") or []:
        if not isinstance(record, dict):
            continue
        if record.get("eventSource") != "aws:s3":
            continue
        if not str(record.get("eventName", "")).startswith("ObjectCreated"):
            continue
        key = ((record.get("s3") or {}).get("object") or {}).get("key")
        if key:
            keys.append(unquote_plus(key))
    return keys


async def _fetch_pdf_bytes(s3_key: str, bucket: str, region: str) -> bytes:
    s3 = boto3.client("s3", region_name=region)
    response = await asyncio.to_thread(s3.get_object, Bucket=bucket, Key=s3_key)
    return response["Body"].read()


async def _build_card(document: Document, settings: Any) -> dict[str, Any] | None:
    """Builds the form-shaped record (app/services/record_extractor.py). For a PDF it
    prefers the file's own table geometry and text layer over the client's flattened
    OCR text, which has already lost the column positions that say which value
    belongs to which analyte.

    Returns None when there's nothing to work from *yet* - a photo, or a scanned
    PDF, whose OCR text the client hasn't submitted - so the caller can wait for it
    instead of recording an empty result."""
    s3_key = document.s3_key or ""
    if is_spreadsheet_report(s3_key):
        # A CSV/TSV export already holds the table as columns - read the file
        # itself; the client's text adds nothing.
        try:
            data = await _fetch_pdf_bytes(s3_key, settings.documents_bucket, settings.aws_region)
            card = await asyncio.to_thread(build_record_from_spreadsheet, data)
            if card is not None:
                return card
        except Exception:
            logger.exception("Spreadsheet extraction failed for document %s", document.id)

    if s3_key.lower().endswith(".pdf"):
        try:
            pdf_bytes = await _fetch_pdf_bytes(
                s3_key, settings.documents_bucket, settings.aws_region
            )
            card = await asyncio.to_thread(build_record_from_pdf, pdf_bytes, document.raw_text)
            if card is not None:
                return card
            logger.info("PDF for document %s has no text layer or table", document.id)
        except Exception:
            # A missing/corrupt object or an unparseable PDF shouldn't fail the whole
            # document when there may still be OCR text to fall back on.
            logger.exception("PDF extraction failed for document %s", document.id)

    if not (document.raw_text or "").strip():
        return None

    return await asyncio.to_thread(build_record_from_text, document.raw_text)


async def _process_document(
    document: Document,
    session: AsyncSession,
    settings: Any,
) -> bool:
    """Processes one document. Returns False when it should be retried later
    because the client's OCR text hasn't arrived yet."""
    if document.status == DocumentStatus.COMPLETED:
        # Already done - e.g. a redelivered event, or a PDF finished before a
        # retry fired. Reprocessing would only repeat the work.
        return True

    try:
        card = await _build_card(document, settings)
    except Exception as exc:
        document.status = DocumentStatus.FAILED
        document.failure_reason = str(exc)[:1024]
        await session.commit()
        raise

    if card is None:
        logger.info("Document %s has no usable content yet; waiting for its text", document.id)
        return False

    document.status = DocumentStatus.COMPLETED
    document.parsed_result = card
    document.processed_at = datetime.now(UTC)
    await session.commit()
    return True


async def handle_message(
    message: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> bool:
    """Returns True when the message is fully handled and can be deleted, False
    to leave it on the queue for redelivery."""
    body = json.loads(message["Body"])
    settings = get_settings()

    keys = s3_keys_from_event(body)
    document_id = body.get("document_id")
    if not keys and not document_id:
        # s3:TestEvent, or something this worker doesn't understand - retrying
        # won't make it meaningful, so let it go.
        logger.info("Ignoring message with no document reference: %s", body.get("Event", "?"))
        return True

    all_done = True
    async with session_maker() as session:
        documents: list[Document] = []
        if document_id:
            document = await session.get(Document, uuid.UUID(document_id))
            if document is not None:
                documents.append(document)
        for key in keys:
            result = await session.execute(select(Document).where(Document.s3_key == key))
            document = result.scalar_one_or_none()
            if document is None:
                # Presign creates the row before issuing the URL, so an object with
                # no row wasn't uploaded through the API - nothing to attach it to.
                logger.warning("No Document row for uploaded object %s; skipping", key)
                continue
            documents.append(document)

        for document in documents:
            if not await _process_document(document, session, settings):
                all_done = False

    return all_done


async def run() -> None:
    settings = get_settings()
    init_engine(
        build_database_url(settings),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    session_maker = get_session_maker()

    sqs = boto3.client("sqs", region_name=settings.aws_region)

    logger.info("Starting processing consumer on %s", settings.sqs_processing_queue_url)

    while True:
        response = sqs.receive_message(
            QueueUrl=settings.sqs_processing_queue_url,
            MaxNumberOfMessages=MAX_MESSAGES,
            WaitTimeSeconds=WAIT_TIME_SECONDS,
            VisibilityTimeout=VISIBILITY_TIMEOUT,
        )

        for message in response.get("Messages", []):
            try:
                done = await handle_message(message, session_maker)
            except Exception:
                logger.exception(
                    "Failed to process message %s; leaving for redrive", message.get("MessageId")
                )
                continue

            if done:
                sqs.delete_message(
                    QueueUrl=settings.sqs_processing_queue_url,
                    ReceiptHandle=message["ReceiptHandle"],
                )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
