# Настройки браузера Playwright

# Актуальный User-Agent Firefox (обновлять периодически)
useragent = 'Mozilla/5.0 (X11; Linux x86_64; rv:134.0) Gecko/20100101 Firefox/134.0'

# Параметры запуска браузера
browser_launch_options = {
    'headless': True,
    # Firefox-специфичные настройки для скрытия автоматизации
    'firefox_user_prefs': {
        # Отключить детекцию webdriver
        'dom.webdriver.enabled': False,
        # Скрыть признаки автоматизации
        'useragentoverride': useragent,
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
