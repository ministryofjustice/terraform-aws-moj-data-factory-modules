import json
import logging
import sys
from datetime import datetime


class JsonFormatter(logging.Formatter):
    def format(self, record):

        log_record = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
            "execution_id": getattr(record, "execution_id", None),
            "database_name": getattr(record, "database_name", None),
            "schema_name": getattr(record, "schema_name", None),
            "table_name": getattr(record, "table_name", None),
            "object_key": getattr(record, "object_key", None)
        }

        return json.dumps(log_record)


def get_logger(name: str):

    logger = logging.getLogger(name)

    if not logger.handlers:

        logger.setLevel(logging.INFO)

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())

        logger.addHandler(handler)

    return logger