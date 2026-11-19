from hashlib import sha256

from lake.archive.codec import decode_artifact
from lake.archive.receipts import ArtifactReceipt
from lake.store import IntegrityError


def payload_digest(raw):
    return sha256(raw).hexdigest()


def verify_payload(receipt: ArtifactReceipt, raw):
    if not isinstance(raw, bytes) or payload_digest(raw) != receipt.digest:
        raise IntegrityError(f'artifact {receipt.key.business_id!r} failed its byte check')
    payload = decode_artifact(raw)
    if (payload.get('category'), payload.get('business_id')) != (receipt.key.category,
                                                                 receipt.key.business_id):
        raise IntegrityError(f'artifact identity disagrees with {receipt.key!r}')
    return payload
