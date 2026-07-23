#!/bin/sh
# Запуск полного набора тестов проекта. Использование:
#   cd tests && ./run_tests.sh
# или из корня репозитория:
#   ./tests/run_tests.sh
set -e
cd "$(dirname "$0")/.."
PYTHONPATH=src python3 -m unittest tests.test_all -v
