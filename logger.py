# logger.py

import logging
import os

def setup_logger(log_file: str) -> logging.Logger:
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Prevent duplicate handlers if setup_logger() is called again
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    # Write to file
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)

    # Also show logs in terminal
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger