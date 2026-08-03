"""
Обёртка над GitHub REST API (api.github.com) — чтобы код, который
присылает бот, попадал не только текстом в чат, но и полноценным
коммитом в отдельную ветку репозитория.

Мультипользовательский слой: токен и базовая ветка теперь ЛИЧНЫЕ на
chat_id (команда /setgithub <токен> [базовая_ветка]). Создатель
(config.OWNER_CHAT_ID) — единственный, для кого есть запасной
вариант: если он не задавал себе токен через /setgithub, используется
GITHUB_TOKEN/GITHUB_BASE_BRANCH из .env — это его собственный токен,
настроенный при деплое. У всех остальных пользователей такого отката
нет — без /setgithub любая функция ниже кидает понятную ошибку.

Нужен персональный токен (fine-grained, права Contents: Read and
write на конкретный репозиторий) — https://github.com/settings/personal-access-tokens.

Реализация: push_file_to_branch — через Contents API (один файл, один
коммит, самый простой путь). push_files_to_branch — через Git Data
API (blobs/tree/commit/ref), несколько файлов ОДНИМ атомарным
коммитом — используется modules/github/editor.py, когда правки
затрагивают больше одного файла.
"""
import base64
import json
import urllib.request
import urllib.parse
import urllib.error

from config import config
from core.logger import get_logger
from modules.users import service as users_service
from storage.db import get_setting, set_setting

log = get_logger(__name__)

API_BASE = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


def _token_key(chat_id: int | str) -> str:
    return f"github_token:{chat_id}"


def _base_branch_key(chat_id: int | str) -> str:
    return f"github_base_branch:{chat_id}"


def set_credentials(chat_id: int | str, token: str, base_branch: str = "") -> None:
    set_setting(_token_key(chat_id), token.strip())
    if base_branch.strip():
        set_setting(_base_branch_key(chat_id), base_branch.strip())


def get_token_for(chat_id: int | str) -> str:
    stored = get_setting(_token_key(chat_id))
    if stored:
        return stored
    if users_service.is_owner(chat_id):
        return config.GITHUB_TOKEN
    return ""


def get_base_branch_for(chat_id: int | str) -> str:
    stored = get_setting(_base_branch_key(chat_id))
    if stored:
        return stored
    return config.GITHUB_BASE_BRANCH


def _request(chat_id: int | str, method: str, path: str, body: dict | None = None) -> dict:
    token = get_token_for(chat_id)
    if not token:
        raise GitHubError(
            "GitHub-токен не настроен для вас — добавьте свой личный "
            "fine-grained токен (права Contents: Read and write): "
            "/setgithub <токен> [базовая_ветка]"
        )
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        log.error("GitHub API %s %s (chat_id=%s) -> HTTP %s: %s", method, path, chat_id, e.code, body_text)
        raise GitHubError(f"GitHub вернул {e.code}: {body_text[:200]}") from e
    except urllib.error.URLError as e:
        log.error("GitHub network error (chat_id=%s): %s", chat_id, e)
        raise GitHubError("Не удалось связаться с GitHub API") from e


def _get_branch_sha(chat_id: int | str, repo: str, branch: str) -> str:
    branch_q = urllib.parse.quote(branch, safe="")
    data = _request(chat_id, "GET", f"/repos/{repo}/git/ref/heads/{branch_q}")
    return data["object"]["sha"]


def branch_exists(chat_id: int | str, repo: str, branch: str) -> bool:
    try:
        _get_branch_sha(chat_id, repo, branch)
        return True
    except GitHubError:
        return False


def _ensure_branch(chat_id: int | str, repo: str, branch: str, base_branch: str) -> bool:
    """Возвращает True, если ветку пришлось создать."""
    try:
        _get_branch_sha(chat_id, repo, branch)
        return False  # уже существует, ничего не создаём
    except GitHubError:
        pass
    base_sha = _get_branch_sha(chat_id, repo, base_branch)
    _request(
        chat_id,
        "POST",
        f"/repos/{repo}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    return True


def _get_existing_file_sha(chat_id: int | str, repo: str, branch: str, path: str) -> str | None:
    path_q = urllib.parse.quote(path)
    branch_q = urllib.parse.quote(branch, safe="")
    try:
        data = _request(chat_id, "GET", f"/repos/{repo}/contents/{path_q}?ref={branch_q}")
        return data.get("sha")
    except GitHubError:
        return None


def get_file_content(chat_id: int | str, repo: str, branch: str, path: str) -> str | None:
    """Текущее содержимое файла в данной ветке, или None, если файла
    нет (используется modules/github/editor.py, чтобы дать модели
    исходник для правки)."""
    path_q = urllib.parse.quote(path)
    branch_q = urllib.parse.quote(branch, safe="")
    try:
        data = _request(chat_id, "GET", f"/repos/{repo}/contents/{path_q}?ref={branch_q}")
    except GitHubError:
        return None
    if data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode("utf-8")
    return data.get("content", "")


def push_file_to_branch(
    chat_id: int | str,
    repo: str,
    branch: str,
    path: str,
    content: str,
    message: str,
    base_branch: str | None = None,
) -> dict:
    """Создаёт branch от base_branch, если её ещё нет, и создаёт/обновляет
    в ней один файл одним коммитом. repo — вида "owner/name"."""
    base_branch = base_branch or get_base_branch_for(chat_id)
    created_branch = _ensure_branch(chat_id, repo, branch, base_branch)
    existing_sha = _get_existing_file_sha(chat_id, repo, branch, path)

    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if existing_sha:
        body["sha"] = existing_sha

    path_q = urllib.parse.quote(path)
    result = _request(chat_id, "PUT", f"/repos/{repo}/contents/{path_q}", body)

    return {
        "created_branch": created_branch,
        "file_html_url": result.get("content", {}).get("html_url", ""),
        "branch_url": f"https://github.com/{repo}/tree/{branch}",
    }


# ── Git Data API — несколько файлов одним атомарным коммитом ──
# Подтверждено по документации GitHub (docs.github.com/en/rest/git):
# blob -> tree (base_tree + новые записи) -> commit (tree + parent) ->
# обновление ref. Пока не сделан последний шаг (update ref), в самой
# ветке ничего не меняется — если что-то упадёт по пути, ветка
# остаётся как была, никаких "недокоммиченных" следов.

def _create_blob(chat_id: int | str, repo: str, content: str) -> str:
    data = _request(chat_id, "POST", f"/repos/{repo}/git/blobs", {"content": content, "encoding": "utf-8"})
    return data["sha"]


def _get_commit_tree_sha(chat_id: int | str, repo: str, commit_sha: str) -> str:
    data = _request(chat_id, "GET", f"/repos/{repo}/git/commits/{commit_sha}")
    return data["tree"]["sha"]


def _create_tree(chat_id: int | str, repo: str, base_tree_sha: str, entries: list[dict]) -> str:
    data = _request(chat_id, "POST", f"/repos/{repo}/git/trees", {"base_tree": base_tree_sha, "tree": entries})
    return data["sha"]


def _create_commit(chat_id: int | str, repo: str, message: str, tree_sha: str, parent_sha: str) -> str:
    data = _request(
        chat_id,
        "POST",
        f"/repos/{repo}/git/commits",
        {"message": message, "tree": tree_sha, "parents": [parent_sha]},
    )
    return data["sha"]


def _update_ref(chat_id: int | str, repo: str, branch: str, commit_sha: str) -> None:
    branch_q = urllib.parse.quote(branch, safe="")
    _request(
        chat_id,
        "PATCH",
        f"/repos/{repo}/git/refs/heads/{branch_q}",
        {"sha": commit_sha, "force": False},
    )


def push_files_to_branch(
    chat_id: int | str,
    repo: str,
    branch: str,
    files: dict[str, str],
    message: str,
    base_branch: str | None = None,
) -> dict:
    """Создаёт/обновляет НЕСКОЛЬКО файлов ОДНИМ атомарным коммитом —
    для случая, когда правки затрагивают больше одного файла разом."""
    base_branch = base_branch or get_base_branch_for(chat_id)
    created_branch = _ensure_branch(chat_id, repo, branch, base_branch)

    parent_sha = _get_branch_sha(chat_id, repo, branch)
    base_tree_sha = _get_commit_tree_sha(chat_id, repo, parent_sha)

    entries = []
    for path, content in files.items():
        blob_sha = _create_blob(chat_id, repo, content)
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})

    new_tree_sha = _create_tree(chat_id, repo, base_tree_sha, entries)
    new_commit_sha = _create_commit(chat_id, repo, message, new_tree_sha, parent_sha)
    _update_ref(chat_id, repo, branch, new_commit_sha)

    return {
        "created_branch": created_branch,
        "commit_url": f"https://github.com/{repo}/commit/{new_commit_sha}",
        "branch_url": f"https://github.com/{repo}/tree/{branch}",
    }
