-- Таблица CSS/XPath-селекторов сайта binodex.app для OTC-браузера.
-- Аналог Program.settings.pocket_settings, но в БД binodex (схема settings).
-- Заполняется вручную по мере подбора селекторов (см. scripts/binodex_selectors.py);
-- читается так же, как pocket_settings — по par_name (apps/setting_app.py::find_par).
--
-- Применить: psql -h <host> -p <port> -U <user> -d binodex -f scripts/binodex_settings.sql
-- (в проекте таблица уже создана этим DDL).

CREATE TABLE IF NOT EXISTS settings.binodex_settings (
    id_par      serial PRIMARY KEY,
    par_name    varchar NOT NULL UNIQUE,   -- ключ селектора (как в pocket_settings)
    par_value   varchar,                   -- сам CSS/XPath-селектор
    description  varchar                    -- пояснение, что это за элемент
);

COMMENT ON TABLE settings.binodex_settings IS
    'CSS/XPath селекторы сайта binodex.app для OTC-браузера (аналог Program.settings.pocket_settings)';

-- Пример заполнения (par_name произвольные — определяются при подборе):
-- INSERT INTO settings.binodex_settings (par_name, par_value, description) VALUES
--     ('list_valute_css', 'a.some-pair-row', 'Строка валютной пары в списке')
-- ON CONFLICT (par_name) DO UPDATE SET par_value = EXCLUDED.par_value,
--                                      description = EXCLUDED.description;
