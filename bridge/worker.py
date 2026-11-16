import logging
import time

from sqlalchemy.exc import SQLAlchemyError

from . import db, runstore, telemetry, workflow


def main():
    from benchmarks.harness import work_one

    telemetry.configure("browserbridge-worker")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    last_cleanup = 0
    while True:
        try:
            if db.now() - last_cleanup >= 30:
                runstore.expire()
                last_cleanup = db.now()
            lease = runstore.claim()
            if lease:
                workflow.execute(*lease)
            elif not work_one():
                runstore.wait()
        except SQLAlchemyError:
            telemetry.log.exception("database_unavailable")
            time.sleep(2)


if __name__ == "__main__":
    main()
