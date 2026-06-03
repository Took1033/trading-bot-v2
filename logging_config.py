"""
logging_config.py - Configuration unifiee de structlog.

Sortie double :
  - Console : format lisible (developpement)
  - Fichier : JSON pur dans logs/trading.log avec rotation 5 MB x 5 fichiers

Appeler configure_logging() une seule fois au demarrage (main.py).
Pattern recommande par structlog via ProcessorFormatter.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

import structlog
from dotenv import load_dotenv

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR   = Path("logs")
LOG_FILE  = LOG_DIR / "trading.log"


def configure_logging() -> None:
    """Configure structlog + handlers fichier et console."""
    LOG_DIR.mkdir(exist_ok=True)

    # Processeurs partages : appliques a chaque log avant le rendu final
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    # Configuration structlog (utilise stdlib pour les handlers)
    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, LOG_LEVEL, logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )

    # Handler fichier : JSON rotatif (5 MB max, 5 backups)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    )

    # Handler console : human-friendly
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(colors=False),
            ],
        )
    )

    # Root logger : attache les 2 handlers
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # Reduire le bruit des libs tierces
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    # werkzeug = access-logs du dashboard (heure LOCALE + bruit). On les coupe :
    # tous les timestamps des logs restent ainsi uniformement en UTC.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    # Repere d'horloge : tous les logs sont en UTC (suffixe Z). On affiche au
    # demarrage la correspondance UTC <-> heure locale pour ne pas s'y perdre.
    from datetime import datetime, timezone
    _now_utc   = datetime.now(timezone.utc)
    _now_local = _now_utc.astimezone()
    structlog.get_logger().info(
        "clock_reference",
        utc=_now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        local=_now_local.strftime("%Y-%m-%d %H:%M:%S"),
        utc_offset=_now_local.strftime("%z"),
        note="tous les logs ci-dessous sont en UTC",
    )
