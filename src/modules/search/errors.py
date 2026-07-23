class SearchError(RuntimeError):
    pass


class SearchConfigError(SearchError):
    """Ошибка конфигурации (не задан ключ/URL) — повторная попытка не
    поможет, поэтому modules/search/service.py не делает retry на неё,
    в отличие от обычных сетевых/временных сбоев."""
    pass
