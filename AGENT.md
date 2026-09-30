# AGENT.md — AI Quick Start

PROJECT_NAME = "cron3" (hron)

Читается первым. Содержит только навигацию и текущий контекст — всё остальное в первоисточниках ниже.

---

## 1. Первоисточники (читать в этом порядке)

| Документ | Что там |
|---|---|
| `../../../../WORKSPACE/COMMON/DOCS/manifest.md` | Стандарты кодинга: SRP, лимиты строк (500 max, 150–350 gold), правила деплоя... |

| `../../../../WORKSPACE/COMMON/DOCS/{PROJECT_NAME}/tech_debt.md` | Документ описывает основные технические долги и проблемы архитектуры, которые требуют немедленного устранения. (пока локально в tech_debt.md текущего проекта) |

| `../../../../WORKSPACE/COMMON/DOCS/{PROJECT_NAME}/TZ.md` | **Все** бизнес-инварианты системы. Например: математическое ядро, FSM...  (пока локально в tech_debt.md текущего проекта)|

| `../../../../WORKSPACE/COMMON/wiki/{PROJECT_NAME}/` | Obsidian-заметки: архитектура, плейбук... |

---

## 2. Карта файлов (где что)

- ../../../../WORKSPACE/COMMON/DOCS/
  - manifest.md
  - {PROJECT_NAME}/
- ../../../../WORKSPACE/COMMON/wiki/
- {PROJECT_NAME}/

---

## 3. Режимы запуска

- free run -- always allowed


Директ -- всегда в приоритете. То что оператор промсит напрямую в окне редактирования с чатом -- в перую очередь.
---

## 4. CRITICAL RULE:
- null

## 5. После каждой правки обновляй WORKSPACE/COMMON/wiki/{PROJECT_NAME}