import logging
import logging.config


class MaxLevelFilter(logging.Filter):
    def __init__(self, max_level: int):
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


def init_logger(level: str = "INFO"):
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "info_only": {
                    "()": MaxLevelFilter,
                    "max_level": logging.INFO,
                },
            },
            "formatters": {
                "standard": {
                    "format": "%(asctime)s : %(name)s : %(levelname)s : %(message)s",
                },
            },
            "handlers": {
                "console_info": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                    "formatter": "standard",
                    "filters": ["info_only"],
                    "level": "DEBUG",
                },
                "console_errors": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                    "formatter": "standard",
                    "level": "WARNING",
                },
            },
            "loggers": {
                "curl_cffi": {"level": "WARNING"},
            },
            "root": {
                "level": level,
                "handlers": ["console_info", "console_errors"],
            },
        }
    )
