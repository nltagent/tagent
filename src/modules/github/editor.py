"""
LLM редактирует файлы репозитория по текстовому запросу и пушит
результат в отдельную ветку — без клонирования репозитория. Файлы
читаются и записываются только через GitHub API:
  1. Текущее содержимое каждого файла — GET через Contents API
     (modules.github.service.get_file_content).
  2. Модель получает содержимое + запрос, возвращает новую версию
     каждого файла в строгом формате (см. _SYSTEM_PROMPT).
  3. Результат пушится ОДНИМ атомарным коммитом через Git Data API
     (modules.github.service.push_files_to_branch) — даже если файлов
     несколько, коммит один.
"""
import re

from config import config
from llm.client import chat_completion, LLMError
from modules.github import service as github_service
from modules.github.service import GitHubError
from core.logger import get_logger

log = get_logger(__name__)

_FILE_BLOCK_RE = re.compile(
    r"===FILE:\s*(?P<path>\S+)\s*===\n(?P<content>.*?)\n===END===",
    re.DOTALL,
)

_SYSTEM_PROMPT = (
    "Ты — инструмент для редактирования файлов кода. Тебе дают текущее "
    "содержимое одного или нескольких файлов репозитория (или пометку "
    "«(файла ещё нет)», если файл ещё не существует) и запрос на "
    "естественном языке, что нужно сделать. Верни ПОЛНОЕ новое "
    "содержимое КАЖДОГО файла из запроса, строго в этом формате, без "
    "каких-либо пояснений до, между или после блоков:\n\n"
    "===FILE: путь/к/файлу===\n"
    "<полное содержимое файла>\n"
    "===END===\n\n"
    "Повтори такой блок для каждого файла, в том же порядке, что и во "
    "входных данных. Не сокращай файлы многоточиями или комментариями "
    "вида «остальное без изменений» — всегда полное содержимое целиком."
)


class EditError(RuntimeError):
    pass


def edit_files(
    repo: str,
    branch: str,
    paths: list[str],
    instruction: str,
    base_branch: str | None = None,
) -> dict:
    base_branch = base_branch or config.GITHUB_BASE_BRANCH

    # Если ветка уже существует (например, продолжаем правки в ней же)
    # — читаем исходники из неё, а не из базовой, чтобы не потерять
    # предыдущие изменения. Если ветки ещё нет — читаем из базовой,
    # именно от неё она и будет создана.
    try:
        read_branch = branch if github_service.branch_exists(repo, branch) else base_branch
    except GitHubError as e:
        raise EditError(str(e)) from e

    current_contents = {}
    for path in paths:
        try:
            current_contents[path] = github_service.get_file_content(repo, read_branch, path)
        except GitHubError as e:
            raise EditError(f"Не удалось прочитать {path}: {e}") from e

    user_parts = [f"Запрос: {instruction}\n"]
    for path, content in current_contents.items():
        shown = content if content is not None else "(файла ещё нет)"
        user_parts.append(f"--- Текущее содержимое {path} ---\n{shown}\n")

    try:
        raw_reply = chat_completion(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(user_parts)},
            ],
            max_tokens=4000,
            temperature=0.2,
        )
    except LLMError as e:
        raise EditError(f"Не удалось получить правки от модели: {e}") from e

    new_files = {m.group("path"): m.group("content") for m in _FILE_BLOCK_RE.finditer(raw_reply)}
    missing = [p for p in paths if p not in new_files]
    if missing:
        log.error("Модель не вернула часть файлов. Полный ответ: %s", raw_reply)
        raise EditError(
            f"Модель не вернула содержимое для: {', '.join(missing)} — "
            "попробуйте переформулировать запрос."
        )

    message = instruction.strip().splitlines()[0][:70] if instruction.strip() else "Правки от бота"

    try:
        result = github_service.push_files_to_branch(
            repo, branch, new_files, message=message, base_branch=base_branch
        )
    except GitHubError as e:
        raise EditError(f"Не удалось запушить изменения: {e}") from e

    result["files"] = list(new_files.keys())
    return result
