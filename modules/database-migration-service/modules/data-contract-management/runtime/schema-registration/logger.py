import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    _STANDARD_ATTRS = set(
        logging.LogRecord("x", 0, "x", 0, "x", (), None).__dict__.keys()
    )

    def format(self, record):
        extra_attrs = {
            k: v
            for k, v in record.__dict__.items()
            if k not in self._STANDARD_ATTRS
        }

        log_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }
        log_record.update(extra_attrs)
        if record.exc_info:
            log_record["error_details"] = {
                "type": str(record.exc_info[0]),
                "message": str(record.exc_info[1]),
                "traceback": self.formatException(record.exc_info),
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