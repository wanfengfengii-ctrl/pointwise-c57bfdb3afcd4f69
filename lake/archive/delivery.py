from dataclasses import dataclass

from lake.archive.artifacts import read_artifact_bytes
from lake.archive.receipts import ArtifactKey, read_artifact_receipt
from lake.archive.verification import verify_payload


@dataclass(frozen=True)
class VerifiedArtifact:
    key: ArtifactKey
    raw: bytes
    payload: dict


def read_verified_artifact(db, key):
    receipt = read_artifact_receipt(db, key)
    raw = read_artifact_bytes(db, receipt.object_id)
    payload = verify_payload(receipt, raw)
    return VerifiedArtifact(key, raw, payload)


def retrieve(db, category, business_id):
    return read_verified_artifact(db, ArtifactKey(category, business_id)).raw
