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

# Есть на всех экранах платформы: форма входа и строка поиска любого реестра.
COMMON_SELECTORS: dict[str, str] = {
    "login_input": "#Login1_UserName",
    "password_input": "#Login1_Password",
    "login_button": "#Login1_LoginButton",

    "search_input": "#ctl00_pContent_ctrl_search_txtSearch",
    "search_button": "#ctl00_pContent_ctrl_search_BtnSearch",
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
    "addr_country": "#ctl00_pContent_TabContainer1_TabPanel5_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_country",
    "addr_box": "#ctl00_pContent_TabContainer1_TabPanel5_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_box",
    "person_contacts_tab": "#__tab_ctl00_pContent_TabContainer1_TabPanel5",
    "person_address_add": "#ctl00_pContent_TabContainer1_TabPanel5_ctrl_contact1_btnAddAddress",
    "person_address_cancel": "#ctl00_pContent_TabContainer1_TabPanel5_ctrl_contact1_BtnItemAddressCancel",
    "person_address_save": "#ctl00_pContent_TabContainer1_TabPanel5_ctrl_contact1_BtnItemAddressOk",
}


# Identifiers observed on a fresh, Add-created firm draft on 2026-11-05.
# The top-level Save is intentionally absent: only the client one-shot
# operator approval path may address BtnItemFirmOk.
FIRM_SELECTORS: dict[str, str] = {
    "result_links": "a[href*='firm_card.aspx?id=']",
    "add_button": "#ctl00_pContent_BtnAddFirm",
    "close_button": "#ctl00_pContent_BtnItemFirmCancel",
    "firm_general_tab": "#__tab_ctl00_pContent_TabContainer1_TabPanel1",
    "firm_identifiers_tab": "#__tab_ctl00_pContent_TabContainer1_TabPanel2",
    "firm_short_name": "#ctl00_pContent_TabContainer1_TabPanel1_txt_fm_name",
    "firm_full_name": "#ctl00_pContent_TabContainer1_TabPanel1_txt_fm_name_full",
    "firm_previous_name": "#ctl00_pContent_TabContainer1_TabPanel1_txt_fm_name_perv",
    "firm_opf": "#ctl00_pContent_TabContainer1_TabPanel1_dd_fm_opf",
    "firm_inn": "#ctl00_pContent_TabContainer1_TabPanel1_txt_id_inn",
    "firm_comment": "#ctl00_pContent_TabContainer1_TabPanel1_txt_fm_comment",
    "firm_status": "#ctl00_pContent_TabContainer1_TabPanel1_dd_dic_id",
    "firm_ogrn": "#ctl00_pContent_TabContainer1_TabPanel2_txt_id_ogrn",
    "firm_kpp": "#ctl00_pContent_TabContainer1_TabPanel2_txt_id_kpp",
    "firm_contacts_tab": "#__tab_ctl00_pContent_TabContainer1_TabPanel6",
    "firm_address_add": "#ctl00_pContent_TabContainer1_TabPanel6_ctrl_contact1_btnAddAddress",
    "firm_address_cancel": "#ctl00_pContent_TabContainer1_TabPanel6_ctrl_contact1_BtnItemAddressCancel",
    "firm_address_save": "#ctl00_pContent_TabContainer1_TabPanel6_ctrl_contact1_BtnItemAddressOk",
    "firm_address_type": "#ctl00_pContent_TabContainer1_TabPanel6_ctrl_contact1_TabContainer1_TabPanel1_dd_addr_type",
    "firm_addr_index": "#ctl00_pContent_TabContainer1_TabPanel6_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_index",
    "firm_addr_country": "#ctl00_pContent_TabContainer1_TabPanel6_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_country",
    "firm_addr_area": "#ctl00_pContent_TabContainer1_TabPanel6_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_area",
    "firm_addr_district": "#ctl00_pContent_TabContainer1_TabPanel6_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_dist",
    "firm_addr_city": "#ctl00_pContent_TabContainer1_TabPanel6_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_city",
    "firm_addr_street": "#ctl00_pContent_TabContainer1_TabPanel6_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_street",
    "firm_addr_build": "#ctl00_pContent_TabContainer1_TabPanel6_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_build",
    "firm_addr_box": "#ctl00_pContent_TabContainer1_TabPanel6_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_box",
    "firm_addr_room": "#ctl00_pContent_TabContainer1_TabPanel6_ctrl_contact1_TabContainer1_TabPanel1_txt_addr_room",
}
