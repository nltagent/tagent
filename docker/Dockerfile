# Фиксируем конкретный минорный тег, а не "latest" — чтобы пересборка
# образа через месяц не подсунула неожиданно другую версию Python.
FROM python:3.12.7-slim

WORKDIR /app/src

# requirements.txt на этом шаге пуст (stdlib-only), но копируем и
# ставим заранее — когда появятся зависимости (шаг с поиском/LLM),
# слой с pip install будет кэшироваться отдельно от кода приложения.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY src/ /app/src/

# Непривилегированный пользователь — минимальная гигиена контейнера.
RUN useradd --create-home --shell /bin/false agent
USER agent

CMD ["python", "main.py"]
