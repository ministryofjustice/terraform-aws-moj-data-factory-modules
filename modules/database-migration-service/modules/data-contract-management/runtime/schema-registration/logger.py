import json
import logging
import sys
from datetime import datetime
 
class JsonFormatter(logging.Formatter):
    _STANDARD_ATTRS = set(logging.LogRecord("x", 0, "x", 0, "x", (), None).__dict__.keys())
    
    def format(self, record):
        extra_attrs = {k: v for k, v in record.__dict__.items() if k not in self._STANDARD_ATTRS}
        log_record = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }
        log_record.update(extra_attrs)
        return json.dumps(log_record)
 
 
def get_logger(name: str):
 
    logger = logging.getLogger(name)
 
    if not logger.handlers:
 
        logger.setLevel(logging.INFO)
 
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
 
        logger.addHandler(handler)
 
    return logger
 