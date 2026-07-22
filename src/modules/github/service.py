"""
Обёртка над GitHub REST API (api.github.com) — чтобы код, который
присылает бот, попадал не только текстом в чат, но и полноценным
коммитом в отдельную ветку репозитория.

Нужен персональный токен (fine-grained, права Contents: Read and
write на конкретный репозиторий) — https://github.com/settings/personal-access-tokens.
GITHUB_TOKEN не обязателен, если этим навыком не пользуетесь — при
отсутствии токена функции ниже кидают понятную ошибку, а не падают
где-то в середине.

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

log = get_logger(__name__)

API_BASE = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


def _request(method: str, path: str, body: dict | None = None) -> dict:
    if not config.GITHUB_TOKEN:
        raise GitHubError(
            "GITHUB_TOKEN не задан — навык GitHub не настроен. "
            "Создайте fine-grained токен с правами Contents: Read and write."
        )
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
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
        log.error("GitHub API %s %s -> HTTP %s: %s", method, path, e.code, body_text)
        raise GitHubError(f"GitHub вернул {e.code}: {body_text[:200]}") from e
    except urllib.error.URLError as e:
        log.error("GitHub network error: %s", e)
        raise GitHubError("Не удалось связаться с GitHub API") from e


def _get_branch_sha(repo: str, branch: str) -> str:
    branch_q = urllib.parse.quote(branch, safe="")
    data = _request("GET", f"/repos/{repo}/git/ref/heads/{branch_q}")
    return data["object"]["sha"]


def branch_exists(repo: str, branch: str) -> bool:
    try:
        _get_branch_sha(repo, branch)
        return True
    except GitHubError:
        return False


def _ensure_branch(repo: str, branch: str, base_branch: str) -> bool:
    """Возвращает True, если ветку пришлось создать."""
    try:
        _get_branch_sha(repo, branch)
        return False  # уже существует, ничего не создаём
    except GitHubError:
        pass
    base_sha = _get_branch_sha(repo, base_branch)
    _request(
        "POST",
        f"/repos/{repo}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    return True


def _get_existing_file_sha(repo: str, branch: str, path: str) -> str | None:
    path_q = urllib.parse.quote(path)
    branch_q = urllib.parse.quote(branch, safe="")
    try:
        data = _request("GET", f"/repos/{repo}/contents/{path_q}?ref={branch_q}")
        return data.get("sha")
    except GitHubError:
        return None


def get_file_content(repo: str, branch: str, path: str) -> str | None:
    """Текущее содержимое файла в данной ветке, или None, если файла
    нет (используется modules/github/editor.py, чтобы дать модели
    исходник для правки)."""
    path_q = urllib.parse.quote(path)
    branch_q = urllib.parse.quote(branch, safe="")
    try:
        data = _request("GET", f"/repos/{repo}/contents/{path_q}?ref={branch_q}")
    except GitHubError:
        return None
    if data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode("utf-8")
    return data.get("content", "")


def push_file_to_branch(
    repo: str,
    branch: str,
    path: str,
    content: str,
    message: str,
    base_branch: str | None = None,
) -> dict:
    """Создаёт branch от base_branch, если её ещё нет, и создаёт/обновляет
    в ней один файл одним коммитом. repo — вида "owner/name"."""
    base_branch = base_branch or config.GITHUB_BASE_BRANCH
    created_branch = _ensure_branch(repo, branch, base_branch)
    existing_sha = _get_existing_file_sha(repo, branch, path)

    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if existing_sha:
        body["sha"] = existing_sha

    path_q = urllib.parse.quote(path)
    result = _request("PUT", f"/repos/{repo}/contents/{path_q}", body)

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

def _create_blob(repo: str, content: str) -> str:
    data = _request("POST", f"/repos/{repo}/git/blobs", {"content": content, "encoding": "utf-8"})
    return data["sha"]


def _get_commit_tree_sha(repo: str, commit_sha: str) -> str:
    data = _request("GET", f"/repos/{repo}/git/commits/{commit_sha}")
    return data["tree"]["sha"]


def _create_tree(repo: str, base_tree_sha: str, entries: list[dict]) -> str:
    data = _request("POST", f"/repos/{repo}/git/trees", {"base_tree": base_tree_sha, "tree": entries})
    return data["sha"]


def _create_commit(repo: str, message: str, tree_sha: str, parent_sha: str) -> str:
    data = _request(
        "POST",
        f"/repos/{repo}/git/commits",
        {"message": message, "tree": tree_sha, "parents": [parent_sha]},
    )
    return data["sha"]


def _update_ref(repo: str, branch: str, commit_sha: str) -> None:
    branch_q = urllib.parse.quote(branch, safe="")
    _request(
        "PATCH",
        f"/repos/{repo}/git/refs/heads/{branch_q}",
        {"sha": commit_sha, "force": False},
    )


def push_files_to_branch(
    repo: str,
    branch: str,
    files: dict[str, str],
    message: str,
    base_branch: str | None = None,
) -> dict:
    """Создаёт/обновляет НЕСКОЛЬКО файлов ОДНИМ атомарным коммитом —
    для случая, когда правки затрагивают больше одного файла разом."""
    base_branch = base_branch or config.GITHUB_BASE_BRANCH
    created_branch = _ensure_branch(repo, branch, base_branch)

    parent_sha = _get_branch_sha(repo, branch)
    base_tree_sha = _get_commit_tree_sha(repo, parent_sha)

    entries = []
    for path, content in files.items():
        blob_sha = _create_blob(repo, content)
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})

    new_tree_sha = _create_tree(repo, base_tree_sha, entries)
    new_commit_sha = _create_commit(repo, message, new_tree_sha, parent_sha)
    _update_ref(repo, branch, new_commit_sha)

    return {
        "created_branch": created_branch,
        "commit_url": f"https://github.com/{repo}/commit/{new_commit_sha}",
        "branch_url": f"https://github.com/{repo}/tree/{branch}",
    }
