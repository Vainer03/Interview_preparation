import logging

from pydantic_settings import BaseSettings


def setup_logging(config: BaseSettings) -> None:
    """Setup logging configuration."""
    logging.basicConfig(
        format=config.logging_format,
    )
    for name, modules in config.logging.model_dump().items():
        level = logging.getLevelName(name.upper())
        if not isinstance(level, int):  # unknown level name is mapped to string
            continue
        for module_str in modules.split(","):
            module_str = module_str.strip()
            if module_str:
                logging.getLogger(module_str).setLevel(level)
