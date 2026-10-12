"""Адреса страниц и идентификаторы элементов платформы.

Селекторы взяты из out/form_map.json — снимка реальной формы. Любой можно
переопределить в config.yaml, если вендор изменит вёрстку.

Идентификаторы ASP.NET стабильны внутри версии платформы и надёжнее подписей:
подпись «Комментарий» относится к textarea, а рядом стоят чекбоксы.
"""

from __future__ import annotations

LOGIN_PATH = "/default.aspx"
PERSON_LIST_PATH = "/register/person_list.aspx"
FIRM_LIST_PATH = "/register/firm_list.aspx"
ASSIGNMENT_LIST_PATH = "/register/assignment_list.aspx"

# Есть на всех экранах платформы: форма входа и строка поиска любого реестра.
COMMON_SELECTORS: dict[str, str] = {
    "login_input": "#Login1_UserName",
    "password_input": "#Login1_Password",
    "login_button": "#Login1_LoginButton",

    "search_input": "#ctl00_pContent_ctrl_search_txtSearch",
    "status_filter": "#ctl00_pContent_dd_status",
}

# Реестр физлиц и карточка физлица. Регистрируются сценарием person_card.
PERSON_SELECTORS: dict[str, str] = {
    "result_links": "a[href*='person_card.aspx?id=']",
    "add_button": "#ctl00_pContent_BtnAddPerson",
    "close_button": "#ctl00_pContent_BtnItemPersonCancel",

    "last_name": "#ctl00_pContent_TabContainer1_TabPanel1_txt_pn_name_last",
    "first_name": "#ctl00_pContent_TabContainer1_TabPanel1_txt_pn_name_first",
    "middle_name": "#ctl00_pContent_TabContainer1_TabPanel1_txt_pn_name_mid",
    "inn": "#ctl00_pContent_TabContainer1_TabPanel1_txt_id_inn",
    "comment": "#ctl00_pContent_TabContainer1_TabPanel1_txt_pn_comment",
    "sex_select": "#ctl00_pContent_TabContainer1_TabPanel1_dd_pn_sex",
    "status_select": "#ctl00_pContent_TabContainer1_TabPanel1_dd_dic_id",

    "address_kind_select": (
        "#ctl00_pContent_TabContainer1_TabPanel5_ctrl_contact1"
        "_TabContainer1_TabPanel1_dd_addr_type"
    ),
    "addr_index": "#ctl00_pContent_TabContainer1_TabPanel5_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_index",
    "addr_area": "#ctl00_pContent_TabContainer1_TabPanel5_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_area",
    "addr_district": "#ctl00_pContent_TabContainer1_TabPanel5_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_dist",
    "addr_city": "#ctl00_pContent_TabContainer1_TabPanel5_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_city",
    "addr_street": "#ctl00_pContent_TabContainer1_TabPanel5_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_street",
    "addr_build": "#ctl00_pContent_TabContainer1_TabPanel5_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_build",
    "addr_room": "#ctl00_pContent_TabContainer1_TabPanel5_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_room",
}
