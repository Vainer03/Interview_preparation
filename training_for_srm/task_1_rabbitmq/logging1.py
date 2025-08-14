import logging

from pydantic_settings import BaseSettings


def setup_logging(config: BaseSettings) -> None:
    logging.basicConfig(
        format=config.logging_format,
    )
    for name, modules in config.logging.model_dump().items():
        level = logging.getLevelName(name.upper())
        if not isinstance(level, int):  # unknown level name is mapped to string
            continue
        for module in modules.split(","):
            module = module.strip()
            if module:
                logging.getLogger(module).setLevel(level)
