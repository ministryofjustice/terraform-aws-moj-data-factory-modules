import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    _STANDARD_ATTRS = set(
        logging.LogRecord("x", 0, "x", 0, "x", (), None).__dict__
    ) | {"message", "asctime"}

    def format(self, record):
        log_record = {
            "timestamp": datetime.fromtimestamp(
                record.created,
                timezone.utc,
            ).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in self._STANDARD_ATTRS and key not in log_record:
                log_record[key] = value

        if record.exc_info:
            log_record["error_details"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "traceback": self.formatException(record.exc_info),
            }

        return json.dumps(log_record, default=str)


def get_logger(name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)

    return logger
