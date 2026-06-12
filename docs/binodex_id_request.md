## Селекторы binodex

| `par_name`                | Элемент UI                                           | Текущий селектор                                                                  |
|---------------------------|------------------------------------------------------|-----------------------------------------------------------------------------------|
| `select_pair_add`         | Кнопка открытия модалки выбора пары                  | `.row_w > div:nth-child(1) > span:nth-child(1) > div:nth-child(1)`                |
| `input_pair`              | Поле поиска пары (контейнер реального `<input>`)     | `div.input:nth-child(2)`                                                          |
| `category_valute`         | Категория «Валюты» в модалке                         | `button.select_pair_add_modal_link:nth-child(2) > span:nth-child(2)`              |
| `category_stock`          | Категория «Акции»                                    | `button.select_pair_add_modal_link:nth-child(4) > span:nth-child(2)`              |
| `category_index`          | Категория «Индексы»                                  | `button.select_pair_add_modal_link:nth-child(3) > span:nth-child(2)`              |
| `category_commodity`      | Категория «Сырьё»                                    | `button.select_pair_add_modal_link:nth-child(5) > span:nth-child(2)`              |
| `category_crypto`         | Категория «Крипта»                                   | `button.select_pair_add_modal_link:nth-child(6) > span:nth-child(2)`              |
| `modal_pair_item`         | Строка пары в списке модалки (повторяющийся элемент) | `button.modal_pair_item`                                                          |
| `screen_zone`             | `<canvas>` графика (для скрина)                      | `.graph > div:nth-child(2) > div:nth-child(1) > canvas:nth-child(2)`              |
| `setup_candle_scale`      | Кнопка масштаба свечи (открывает список 30S/M1…)     | `div.graph_pair_setting_w:nth-child(2) > div:nth-child(1) > button:nth-child(1)`  |
| `setup_candle_scale_item` | Пункт «30S» в списке масштаба свечи                  | `.chart_setting_modal_items >> text="30S"`                                        |
| `setup_chart_scale`       | Кнопка масштаба графика (открывает H1…)              | `.profile_add_wrap_selected_w`                                                    |
| `setup_chart_scale_item`  | Пункт «H1» в списке масштаба графика                 | `.profile_add_wrap_selected_wrap_options >> text="H1"`                            |
| `setup_settings_open`     | Кнопка настроек аккаунта (и маркер готовности UI)    | `div.header_settings_w:nth-child(3) > button:nth-child(1)`                        |
| `setup_theme`             | Пункт «Тема» в меню настроек                         | `button.header_settings_link:nth-child(6) > span:nth-child(2)`                    |
| `setup_theme_toggle`      | Переключатель темы                                   | `.switch`                                                                         |
| `setup_indicators`        | Кнопка меню индикаторов                              | `div.graph_pair_setting_w:nth-child(4) > div:nth-child(1) > button:nth-child(1)`  |
| `setup_indicator_item`    | Первый индикатор в списке                            | `button.chart_indicator:nth-child(1) > span:nth-child(2)`                         |
| `tek_val`                 | Текущая пара в сайдбаре                              | `.sidebar_page_overflow > div:nth-child(1) > div:nth-child(1) > div:nth-child(1)` |
| `login_open`              | Ссылка открытия логина в шапке                       | `#root > header > div > div > a`                                                  |

## Вне зоны binodex (модалка Privy — не переводим)

| `par_name`          | Элемент UI              | Текущий селектор              |
|---------------------|-------------------------|-------------------------------|
| `login_email`       | Поле email (уже `id`)   | `#email-input`                |
| `login_submit`      | Кнопка отправки email   | `#privy-modal-content button` |
| `login_code_inputs` | Ячейки OTP-кода (6 шт.) | `#privy-modal-content input`  |
