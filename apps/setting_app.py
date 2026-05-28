import sys

from logs import init_logger
#
logger = init_logger(__name__)

def find_par(data, par):
    result = next((item['par_value'] for item in data if item['par_name'] == par), None)
    if result is None:
        logger.error(f'Не найден параметр {par} для браузера')
        sys.exit(1)
    return result
