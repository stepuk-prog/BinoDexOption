# Настройки браузера Playwright

import os

# Актуальный User-Agent Firefox (обновлять периодически)
useragent = 'Mozilla/5.0 (X11; Linux x86_64; rv:134.0) Gecko/20100101 Firefox/134.0'

# Headless — env-driven, ДЕФОЛТ True (на сервере нет DISPLAY → headed падает с 'no DISPLAY').
# Для локальной отладки с X-сервером: BROWSER_HEADLESS=0. Так литеральный False не уедет в прод.
_headless = os.getenv('BROWSER_HEADLESS', '1') not in ('0', 'false', 'False', '')

# Параметры запуска браузера
browser_launch_options = {
    'headless': _headless,
    # Firefox-специфичные настройки для скрытия автоматизации
    'firefox_user_prefs': {
        # Отключить детекцию webdriver
        'dom.webdriver.enabled': False,

        # Отключить телеметрию
        'toolkit.telemetry.enabled': False,
        'toolkit.telemetry.unified': False,
        'toolkit.telemetry.archive.enabled': False,
        # Отключить отчёты о сбоях
        'browser.crashReports.unsubmittedCheck.enabled': False,
        # Отключить проверку первого запуска
        'browser.startup.homepage_override.mstone': 'ignore',
        # Отключить обновления
        'app.update.enabled': False,
        # Отключить Safe Browsing (уменьшает сетевые запросы)
        'browser.safebrowsing.enabled': False,
        'browser.safebrowsing.malware.enabled': False,
        # WebGL — не скрывать (выглядит подозрительно если отключен)
        'webgl.disabled': False,
        # Не показывать предупреждения
        'browser.tabs.warnOnClose': False,
        'browser.tabs.warnOnCloseOtherTabs': False,
        # Тёмная тема Firefox UI
        'ui.systemUsesDarkTheme': 1,
        'extensions.activeThemeID': 'firefox-compact-dark@mozilla.org',
    },
}

# Параметры контекста браузера
context_options = {
    'user_agent': useragent,
    'viewport': None,  # отключаем фиксированный viewport для возможности изменять размер окна
    'ignore_https_errors': True,
    # Локаль и временная зона для реалистичности
    'locale': 'ru-RU',
    'timezone_id': 'Europe/Moscow',
    # Геолокация (Москва)
    'geolocation': {'latitude': 55.7558, 'longitude': 37.6173},
    'permissions': ['geolocation'],
    # Цветовая схема
    'color_scheme': 'dark',
}

# --- Chromium (только binodex-домен: OTC) -------------------------------------------------
# Новый фронт binodex НЕ бутстрапится в Playwright Firefox (зацикливается на boot-recovery-сплеше,
# апп-шелл не монтируется; с датацентр-IP Privy отдаёт 403/400 и #root пуст — грабли 2026-07-20).
# В Chromium поднимается за ~2с. TV/TradingView (binary) остаётся на Firefox — там всё работает.
# Движок выбирается в browser_app.init_browser по binodex (= not binary). UA для Chromium НЕ
# подменяем (нативный Chrome-UA совпадает с бинарём; подмена на Firefox-UA палила бы automation).
chromium_launch_args = [
    '--disable-blink-features=AutomationControlled',  # убрать automation-флаг (navigator.webdriver и пр.)
    '--no-sandbox',                                   # headless-сервер (в т.ч. под ограниченным юзером)
    '--disable-dev-shm-usage',                        # /dev/shm мал на серверах → /tmp, без крэшей вкладок
]
chromium_launch_options = {'headless': _headless, 'args': chromium_launch_args}
