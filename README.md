# Стенд: влияние асинхронных драйверов БД на производительность FastAPI

Практическая работа №4 (часть 2/2). Сравниваются три варианта доступа FastAPI → PostgreSQL:
`sync_psycopg2` (def-эндпоинты + threadpool), `async_asyncpg`, `async_psycopg3` (async def).
Нагрузка: Locust (замкнутая модель, без think time), два сценария: `rows` (CPU/сериализация, 100 строк) и `slow` (`pg_sleep(0.01)`, I/O-ожидание).
Подробный отчёт: [docs/pr4-report.md](docs/pr4-report.md).

## Требования
Docker (Engine ≥ 24, Compose v2; в macOS подойдёт Colima/Docker Desktop с ≥ 6 CPU и 8 ГиБ), [uv](https://docs.astral.sh/uv/), GNU make, curl, git.
Лимиты контейнеров: SUT 1 CPU / 512 МиБ, БД 2 CPU / 1 ГиБ, генератор 2 CPU / 1 ГиБ - итого нужно ≥ 5 свободных ядер VM.

## Воспроизведение
```bash
git clone <repo> && cd <repo>
make smoke        # поднимает все сервисы (в т.ч. Prometheus, cAdvisor, Grafana), проверяет 3 драйвера и метрики
make experiment   # весь эксперимент одной командой: план -> прогревы/замеры -> results/raw/*
make analyze      # статистика, A/A, проверка генератора, стабильность, графики -> results/processed, docs/img
```
`make all` делает всё подряд. Основной эксперимент занимает ≈ 1,5-2 часа (116 прогонов × ~55 с).
Быстрая проверка конвейера (~3 мин, как в CI): `make ci`.

Параметры (факторы, повторения, прогрев, сид, пул) - только в [configs/experiment.yaml](configs/experiment.yaml).
Другой конфиг: `make experiment analyze CFG=configs/ci.yaml`. Прерванный эксперимент продолжается повторным `make experiment`.

## Структура
| Путь | Назначение |
|---|---|
| `sut/` | FastAPI-приложение, Dockerfile, `pyproject.toml` + `uv.lock` (сборка с `--frozen`) |
| `load/locustfile.py` | сценарии нагрузки |
| `runner/run_experiment.py` | план (полный факторный × повторения, перемешивание по сиду), пересоздание SUT, прогрев, замер, манифест |
| `analysis/analyze.py` | вся статистика и графики (без ручной работы) |
| `monitoring/` | Prometheus, Grafana (datasource), cAdvisor |
| `results/raw/` | сырые данные (в `.gitignore`): `*_stats.csv` Locust, `*_docker_stats.csv`, манифест `*.json` на прогон, `plan.json` |
| `results/processed/` | `runs.csv`, `summary.csv`, `comparisons.csv`, `generator.csv`, `report.md` |

## Как проверить, что окружение совпадает
Манифест каждого прогона содержит git-коммит и флаг dirty, sha256 конфига и `uv.lock`, ID образов (`docker image inspect`),
версии Docker, число CPU/память VM, платформу и Python. Сравните свой `results/raw/plan.json` с `results/processed/` из репозитория.
Все образы закреплены по версии: python 3.12.6-slim, uv 0.5.11, postgres 16.4, locust 2.31.5, prometheus v2.54.1, cadvisor v0.49.1, grafana 11.2.0.

## CI
`.github/workflows/ci.yml` запускает smoke и сокращённый эксперимент (`configs/ci.yaml`), результаты сохраняются как artifact.
