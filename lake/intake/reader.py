from lake.intake.batches import fingerprint_observations, read_batch
from lake.intake.parse import parse_observations
from lake.store import IntegrityError, ValidationError, business_day, identifier


class BatchReader:
    def __init__(self, objects):
        self._objects = objects

    def read(self, db, batch_id):
        batch = read_batch(db, batch_id)
        raw = self._objects.require(db, batch.batch_id)
        try:
            identifier(batch.station, 'station')
            business_day(batch.day)
            observations = parse_observations(raw)
        except ValidationError as exc:
            raise IntegrityError(f'invalid stored observation batch {batch_id!r}') from exc
        if fingerprint_observations(batch.station, batch.day, observations) != batch.fingerprint:
            raise IntegrityError(f'observation object does not match batch {batch_id!r}')
        return batch, observations
