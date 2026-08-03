"""
Роли пользователей и доступ к боту.

Три реальные роли: user / vip / creator (различие user/vip пока
нигде не используется в коде — задел на будущее, оба дают одинаковый
доступ к обычному функционалу). Создатель — единственный chat_id из
config.OWNER_CHAT_ID, у него полные права и он НЕ хранится в таблице
users как обычная запись (проверяется напрямую по конфигу, см.
is_owner). Остальные проходят через заявку: первое сообщение от
незнакомого chat_id заводит запись со статусом 'pending' и (снаружи,
в telegram/router.py) шлёт уведомление создателю, который решает
/approve <chat_id> user|vip или /deny <chat_id>. /block — забанить
уже одобренного (или отклонённого) пользователя без возможности
снова написать боту.

Все ключи в settings/провайдерах/поиске/GitHub, специфичные для
конкретного пользователя, изолируются по chat_id в соответствующих
модулях (llm/providers.py, llm/client.py, modules/search/service.py,
modules/github/service.py) — этот модуль отвечает только за роль и
доступ, а не за сами ключи.
"""
from storage.db import execute, query, query_one, now_iso
from config import config

ROLE_CREATOR = "creator"
ROLE_USER = "user"
ROLE_VIP = "vip"
ROLE_PENDING = "pending"
ROLE_BLOCKED = "blocked"
ROLE_DENIED = "denied"

# Роли, которые /approve вправе выдать — creator/pending/blocked/denied
# не назначаются этой командой (creator — это только OWNER_CHAT_ID,
# остальные — служебные статусы, выставляемые своими командами).
VALID_APPROVE_ROLES = (ROLE_USER, ROLE_VIP)

# Роли, при которых пользователь считается допущенным к обычному
# функционалу бота (не /status и не /users — это отдельная проверка
# на "создатель ли это", см. telegram/router.py:_OWNER_ONLY_COMMANDS).
AUTHORIZED_ROLES = (ROLE_CREATOR, ROLE_USER, ROLE_VIP)


def is_owner(chat_id: int | str) -> bool:
    return str(chat_id) == str(config.OWNER_CHAT_ID)


def get_role(chat_id: int | str) -> str | None:
    """None — про этот chat_id ещё никогда не слышали (ни разу не
    писал боту, заявки тоже нет)."""
    if is_owner(chat_id):
        return ROLE_CREATOR
    row = query_one("SELECT role FROM users WHERE chat_id = ?", (str(chat_id),))
    return row["role"] if row else None


def is_authorized(chat_id: int | str) -> bool:
    return get_role(chat_id) in AUTHORIZED_ROLES


def request_access(chat_id: int | str) -> bool:
    """Заводит заявку (role='pending'), если о chat_id ещё не слышали.

    Возвращает True, если заявка новая — тогда вызывающий код
    (telegram/router.py) должен уведомить создателя. Возвращает
    False, если запись уже существовала (pending/одобрен/блокирован/
    отклонён) — повторно уведомлять создателя не нужно."""
    existing = query_one("SELECT chat_id FROM users WHERE chat_id = ?", (str(chat_id),))
    if existing:
        return False
    execute(
        "INSERT INTO users (chat_id, role, requested_at) VALUES (?, ?, ?)",
        (str(chat_id), ROLE_PENDING, now_iso()),
    )
    return True


def approve(chat_id: int | str, role: str, approved_by: int | str) -> bool:
    """role должна быть 'user' или 'vip'. Возвращает False при
    неизвестной роли (вызывающий код должен показать пользователю
    подсказку по использованию, не пытаясь угадать роль)."""
    role = role.strip().lower()
    if role not in VALID_APPROVE_ROLES:
        return False
    now = now_iso()
    existing = query_one("SELECT chat_id FROM users WHERE chat_id = ?", (str(chat_id),))
    if existing:
        execute(
            "UPDATE users SET role = ?, approved_by = ?, approved_at = ? WHERE chat_id = ?",
            (role, str(approved_by), now, str(chat_id)),
        )
    else:
        execute(
            """
            INSERT INTO users (chat_id, role, requested_at, approved_by, approved_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(chat_id), role, now, str(approved_by), now),
        )
    return True


def deny(chat_id: int | str) -> bool:
    """Отклонить заявку (или ранее одобренного — снять доступ, но не
    так жёстко, как /block: denied можно снова попросить доступ,
    написав боту заново — новая заявка не создастся автоматически,
    т.к. запись уже есть, но создатель может /approve её в любой
    момент по /users). Возвращает False, если о chat_id вообще не
    слышали (нечего отклонять)."""
    existing = query_one("SELECT chat_id FROM users WHERE chat_id = ?", (str(chat_id),))
    if not existing:
        return False
    execute("UPDATE users SET role = ? WHERE chat_id = ?", (ROLE_DENIED, str(chat_id)))
    return True


def block(chat_id: int | str) -> bool:
    """Забанить chat_id — бот больше не будет отвечать и не будет
    повторно уведомлять создателя об этом chat_id. Создателя
    заблокировать нельзя (возвращает False)."""
    if is_owner(chat_id):
        return False
    now = now_iso()
    existing = query_one("SELECT chat_id FROM users WHERE chat_id = ?", (str(chat_id),))
    if existing:
        execute("UPDATE users SET role = ? WHERE chat_id = ?", (ROLE_BLOCKED, str(chat_id)))
    else:
        execute(
            "INSERT INTO users (chat_id, role, requested_at) VALUES (?, ?, ?)",
            (str(chat_id), ROLE_BLOCKED, now),
        )
    return True


def list_users() -> list[dict]:
    """Все известные записи (без создателя — тот не в таблице),
    новые заявки сверху."""
    rows = query(
        """
        SELECT chat_id, role, requested_at, approved_by, approved_at FROM users
        ORDER BY requested_at DESC
        """
    )
    return [dict(r) for r in rows]
