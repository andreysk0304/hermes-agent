#!/usr/bin/env python3
"""
Тул автоматизации Яндекс.Почты через веб-интерфейс (OpenAI tool).

Использует примитивы browser_tool (agent-browser): логин в Яндекс.Почту от лица
пользователя и получение писем по ключевым словам в теме.

Важно:
- Рассчитан на сценарий «логин + пароль (+ опционально код 2FA)».
- Если запросили 2FA — вызови инструмент повторно с параметром two_factor_code.
- Секреты (пароль, код) в ответ не попадают.
"""

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from tools.registry import registry
from tools.browser_tool import (
    browser_navigate,
    browser_snapshot,
    browser_click,
    browser_type,
    browser_press,
)


def _enabled() -> bool:
    """Включение тула только при явном env-флаге (по умолчанию выключен)."""
    return os.getenv("HERMES_ENABLE_YANDEX_MAIL_UI", "").strip().lower() in {"1", "true", "yes", "on"}


YANDEX_MAIL_FETCH_SCHEMA: Dict[str, Any] = {
    "name": "yandex_mail_fetch",
    "description": (
        "Log into Yandex Mail via the web UI and fetch emails by subject keywords. "
        "Use when only username/password (and sometimes 2FA) are available. "
        "If 2FA is required, the tool returns status=needs_2fa; call again with two_factor_code."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "login": {"type": "string", "description": "Yandex login/email/phone (e.g. user@yandex.ru)"},
            "password": {"type": "string", "description": "Yandex account password"},
            "subject_query": {"type": "string", "description": "Subject keywords to search for"},
            "max_results": {"type": "integer", "description": "Max messages to return (1-5)", "default": 1},
            "two_factor_code": {"type": "string", "description": "2FA one-time code, if prompted", "default": ""},
            "task_id": {"type": "string", "description": "Optional task id to keep browser session isolated", "default": ""},
        },
        "required": ["login", "password", "subject_query"],
    },
}


_REF_RE = re.compile(r"@e\d+")


def _snapshot(task_id: str, *, full: bool = False, user_task: Optional[str] = None) -> str:
    raw = browser_snapshot(full=full, task_id=task_id, user_task=user_task)
    try:
        data = json.loads(raw)
    except Exception:
        return ""
    if not data.get("success"):
        return ""
    return str(data.get("snapshot") or "")


def _find_ref_by_keywords(snapshot: str, keywords: List[str]) -> Optional[str]:
    """Находит первый ref элемента в строке, содержащей любое из ключевых слов (без учёта регистра)."""
    if not snapshot:
        return None
    kws = [k.lower() for k in keywords if k]
    for line in snapshot.splitlines():
        low = line.lower()
        if not any(k in low for k in kws):
            continue
        m = _REF_RE.search(line)
        if m:
            return m.group(0)
    return None


def _sleep_short():
    time.sleep(1.0)


def _looks_like_needs_2fa(snapshot: str) -> bool:
    s = (snapshot or "").lower()
    triggers = [
        "введите код", "код подтверждения", "подтвердите вход",
        "одноразовый", "смс", "sms", "two-factor", "2fa",
        "authenticator", "введите цифры", "enter code",
    ]
    return any(t in s for t in triggers)


def _looks_like_inbox(snapshot: str) -> bool:
    s = (snapshot or "").lower()
    # Эвристика: признаки интерфейса почтового ящика.
    return any(t in s for t in ["входящие", "inbox", "написать", "compose", "поиск", "search"])


def _sanitize_for_return(text: str, *, login: str) -> str:
    if not text:
        return ""
    # Не светим логин в сырых дампах страницы.
    safe = text.replace(login, "[LOGIN]")
    # Жёсткий лимит длины, чтобы ответ тула не раздувался.
    return safe[:12000]


def yandex_mail_fetch(
    login: str,
    password: str,
    subject_query: str,
    *,
    max_results: int = 1,
    two_factor_code: str = "",
    task_id: Optional[str] = None,
) -> str:
    tid = (task_id or "").strip() or "default"
    max_results = max(1, min(int(max_results or 1), 5))

    # 1) Открываем страницу Яндекс.Почты
    browser_navigate("https://mail.yandex.ru/", task_id=tid)
    _sleep_short()
    snap = _snapshot(tid)

    # 2) Если ещё не в ящике — жмём «Войти»
    if not _looks_like_inbox(snap):
        ref_login_btn = _find_ref_by_keywords(snap, ["войти", "log in", "sign in"])
        if ref_login_btn:
            browser_click(ref_login_btn, task_id=tid)
            _sleep_short()
            snap = _snapshot(tid)

    # 3) Вводим логин
    ref_login_input = _find_ref_by_keywords(
        snap,
        ["логин", "login", "почта", "email", "телефон", "phone", "username", "аккаунт"],
    )
    if ref_login_input:
        browser_click(ref_login_input, task_id=tid)
        browser_type(ref_login_input, login, task_id=tid)
        browser_press("Enter", task_id=tid)
        _sleep_short()
        snap = _snapshot(tid)

    # 4) Вводим пароль (поле может появиться после шага с логином)
    ref_pwd = _find_ref_by_keywords(snap, ["пароль", "password"])
    if ref_pwd:
        browser_click(ref_pwd, task_id=tid)
        browser_type(ref_pwd, password, task_id=tid)
        browser_press("Enter", task_id=tid)
        _sleep_short()
        snap = _snapshot(tid)

    # 5) 2FA: если запросили код и его не передали — возвращаем needs_2fa
    if _looks_like_needs_2fa(snap):
        if not two_factor_code:
            return json.dumps(
                {
                    "success": False,
                    "status": "needs_2fa",
                    "error": "Yandex попросил 2FA код. Повторите вызов yandex_mail_fetch с two_factor_code.",
                    "page_snapshot": _sanitize_for_return(snap, login=login),
                },
                ensure_ascii=False,
            )
        ref_code = _find_ref_by_keywords(snap, ["код", "code", "одноразовый", "sms", "authenticator"])
        if ref_code:
            browser_click(ref_code, task_id=tid)
            browser_type(ref_code, two_factor_code, task_id=tid)
            browser_press("Enter", task_id=tid)
            _sleep_short()
            snap = _snapshot(tid)

    # 6) Проверяем, что попали в интерфейс ящика
    if not _looks_like_inbox(snap):
        # После логина редирект может занять секунду.
        _sleep_short()
        snap = _snapshot(tid)

    if not _looks_like_inbox(snap):
        return json.dumps(
            {
                "success": False,
                "status": "login_failed_or_blocked",
                "error": "Не удалось попасть в интерфейс Яндекс.Почты (возможна капча/подтверждение/блок).",
                "page_snapshot": _sanitize_for_return(snap, login=login),
            },
            ensure_ascii=False,
        )

    # 7) Ищем по ключевым словам в теме (если есть поле поиска)
    ref_search = _find_ref_by_keywords(snap, ["поиск", "search"])
    if ref_search:
        browser_click(ref_search, task_id=tid)
        browser_type(ref_search, subject_query, task_id=tid)
        browser_press("Enter", task_id=tid)
        _sleep_short()
        snap = _snapshot(tid)

    # 8) Кликаем по первому письму, подходящему под запрос (best-effort)
    # Сначала ищем строки, где есть подстрока запроса.
    ref_msg = None
    q = subject_query.strip().lower()
    if q:
        for line in snap.splitlines():
            if q in line.lower():
                m = _REF_RE.search(line)
                if m:
                    ref_msg = m.group(0)
                    break
    if not ref_msg:
        # Запасной вариант: любой кликабельный элемент, похожий на строку письма
        ref_msg = _find_ref_by_keywords(snap, ["тема", "subject"])
    if ref_msg:
        browser_click(ref_msg, task_id=tid)
        _sleep_short()

    # 9) Достаём текст открытого письма из полного снапшота (в browser_tool — task-aware summarization)
    msg_snap = _snapshot(
        tid,
        full=True,
        user_task="Извлеки текст письма, отправителя, тему и дату из открытого письма.",
    )
    if not msg_snap:
        msg_snap = _snapshot(tid, full=False)

    return json.dumps(
        {
            "success": True,
            "status": "ok",
            "query": subject_query,
            "max_results": max_results,
            "message_snapshot": _sanitize_for_return(msg_snap, login=login),
        },
        ensure_ascii=False,
    )


def _handler(args: Dict[str, Any], **kwargs) -> str:
    return yandex_mail_fetch(
        login=str(args.get("login", "") or ""),
        password=str(args.get("password", "") or ""),
        subject_query=str(args.get("subject_query", "") or ""),
        max_results=int(args.get("max_results", 1) or 1),
        two_factor_code=str(args.get("two_factor_code", "") or ""),
        task_id=str(args.get("task_id", "") or kwargs.get("task_id") or ""),
    )


registry.register(
    name="yandex_mail_fetch",
    toolset="browser",
    schema=YANDEX_MAIL_FETCH_SCHEMA,
    handler=_handler,
    check_fn=_enabled,
    requires_env=["HERMES_ENABLE_YANDEX_MAIL_UI"],
    description="Яндекс.Почта: вход по веб-интерфейсу и выборка писем по теме (включение через env).",
)
