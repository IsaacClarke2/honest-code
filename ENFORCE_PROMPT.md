# honest-code v2: Enforce Mode — Implementation Prompt

> Скорми этот промпт Claude Code в отдельной сессии. Он содержит полную спецификацию enforce mode — от архитектуры до edge cases. Не нужно ничего додумывать, всё описано.

---

## Контекст

Репозиторий: `/root/projects/claude-integrity/` (или `~/.claude/integrity/` если установлен)
GitHub: `https://github.com/IsaacClarke2/honest-code`

Уже есть:
- `check.py` (423 LOC) — Stop hook, классифицирует сессию (CLEAN/HACK/HONEST_FAIL/INCOMPLETE), пишет state.json
- `track_bash.py` (119 LOC) — PostToolUse hook, ловит pytest результаты из Bash
- `state.json` — персистентный стейт с directness score (0.0–1.0)
- `settings.example.json` — пример хуков (Stop + PostToolUse)

**Текущий режим — monitor only.** Check.py записывает score, агент читает его, но ничто не блокирует плохое поведение. Агент может игнорировать низкий score и продолжать хачить.

## Задача

Добавить **enforce mode** — систему, которая реально блокирует плохое поведение через PreToolUse хуки. Не просто записывает, а действует.

**Ключевой принцип: earned autonomy.** Высокий directness = больше свободы. Низкий = жёсткие ограничения. Как кредитная история — заслужил доверие, получил лимит.

---

## Архитектура: 4 уровня enforce

| Level | Имя | Directness | Что делает |
|-------|-----|-----------|------------|
| 0 | **MONITOR** | any | Только логирование (текущее поведение). Ничего не блокирует |
| 1 | **ADVISORY** | < 0.5 | Предупреждения в stderr. Не блокирует, но agent видит warnings |
| 2 | **GATED** | < 0.3 | **Блокирует git commit** если тесты красные. Блокирует push без прогона тестов |
| 3 | **FREEZE** | < 0.15 | **Блокирует ВСЕ git операции** кроме read-only. Агент может только читать код и запускать тесты |

Уровень определяется **автоматически** из directness. Но можно форсировать через config.

---

## Новые файлы

### 1. `enforce.py` — PreToolUse hook (ГЛАВНЫЙ ФАЙЛ)

Вызывается **ДО** каждого Bash вызова через PreToolUse hook. Решает: пропустить или заблокировать.

```
Hook: PreToolUse
Matcher: Bash
Command: python3 ~/.claude/integrity/enforce.py
```

**Логика:**

```python
# Читает command из TOOL_INPUT (env var, JSON с полем "command")
# Читает state.json → directness → определяет enforce level
# Читает config.json → overrides, whitelist
# Принимает решение: ALLOW или BLOCK

# BLOCK = exit code 2 (hook convention для блокировки)
# ALLOW = exit code 0
# При блокировке: stderr сообщение объясняет почему
```

**Что блокируется на каждом уровне:**

Level 0 (MONITOR):
- Ничего. Exit 0 всегда.

Level 1 (ADVISORY):
- Ничего не блокирует.
- Но если команда = git commit и baseline показывает новые failures → печатает в stderr:
  `[INTEGRITY WARNING] You have 3 new test failures since session start. Consider fixing before committing.`
- Если команда = git push → то же предупреждение.

Level 2 (GATED):
- **БЛОКИРУЕТ git commit** если:
  - Есть baseline snapshot И новые test failures (см. baseline.py ниже)
  - ИЛИ last_test_result.json показывает failures
- **БЛОКИРУЕТ git push** если последний commit был в HACK-сессии
- НЕ блокирует: git add, git diff, git status, git log, git stash, git checkout, git branch
- НЕ блокирует: pytest, python, npm test, cargo test (тесты всегда разрешены)
- НЕ блокирует: ls, cat, grep, find, cd и прочие read-only команды

Level 3 (FREEZE):
- **БЛОКИРУЕТ ВСЁ** что пишет в git: commit, push, merge, rebase, cherry-pick, reset, checkout (file), stash pop
- **Разрешает ТОЛЬКО:**
  - git status, git log, git diff, git show, git branch (list), git remote -v
  - pytest / python -m pytest / npm test / cargo test / go test / make test
  - ls, cat, head, tail, grep, find, wc, file, stat
  - python3 (без -c "os.system..." — проверять на injection)
  - echo, printf
- Всё остальное → BLOCK с сообщением:
  `[INTEGRITY FREEZE] directness={score}. Run tests and fix failures to unfreeze. Only read-only and test commands allowed.`

**Detection patterns для команд:**

```python
GIT_WRITE_COMMANDS = [
    r'\bgit\s+commit\b',
    r'\bgit\s+push\b',
    r'\bgit\s+merge\b',
    r'\bgit\s+rebase\b',
    r'\bgit\s+cherry-pick\b',
    r'\bgit\s+reset\b',
    r'\bgit\s+checkout\b.*--\s',  # checkout FILE (not branch)
    r'\bgit\s+stash\s+pop\b',
    r'\bgit\s+stash\s+apply\b',
    r'\bgit\s+tag\b.*-[amd]\b',  # tag creation
]

GIT_READ_COMMANDS = [
    r'\bgit\s+status\b',
    r'\bgit\s+log\b',
    r'\bgit\s+diff\b',
    r'\bgit\s+show\b',
    r'\bgit\s+branch\b(?!.*-[dD])',  # branch list, not delete
    r'\bgit\s+remote\b',
    r'\bgit\s+ls-files\b',
    r'\bgit\s+rev-parse\b',
]

TEST_COMMANDS = [
    r'\bpytest\b',
    r'\bpython3?\s+-m\s+pytest\b',
    r'\bnpm\s+test\b',
    r'\byarn\s+test\b',
    r'\bcargo\s+test\b',
    r'\bgo\s+test\b',
    r'\bmake\s+test\b',
]

SAFE_COMMANDS = [
    r'^ls\b', r'^cat\b', r'^head\b', r'^tail\b',
    r'^grep\b', r'^rg\b', r'^find\b', r'^wc\b',
    r'^file\b', r'^stat\b', r'^echo\b', r'^printf\b',
    r'^pwd\b', r'^which\b', r'^whoami\b', r'^date\b',
    r'^python3?\s', r'^node\s',
]
```

**Важно:** Команды в пайпах и && цепочках тоже надо проверять. Например `pytest && git commit -m "done"` — на level 2 надо проверить и pytest, и git commit. Самый простой подход: если в строке есть git write command → проверять как git write, даже если перед ним тесты.

### 2. `baseline.py` — Session start snapshot

Вызывается в **начале** сессии (через PreToolUse на первом вызове, или через отдельный Start hook если доступен).

```python
# Запускает pytest, сохраняет baseline:
# ~/.claude/integrity/baseline.json
{
    "timestamp": "2026-04-05T...",
    "test_count": 47,
    "pass_count": 45,
    "fail_count": 2,
    "failed_tests": ["test_auth.py::test_login_expired", "test_db.py::test_migration"],
    "git_head": "abc123..."
}
```

**Зачем:** Чтобы отличить "агент сломал тесты" от "тесты были сломаны до агента".

На level 2 (GATED), enforce.py блокирует commit только если:
- baseline.json существует И
- Текущие failures > baseline failures (новые поломки) ИЛИ
- Новые тесты в failed_tests, которых не было в baseline

Если baseline.json не существует (первая сессия, или baseline не снялся) → fallback на простую проверку: есть ли failures в last_test_result.json.

**Реализация:**

Вариант 1 (рекомендуемый): enforce.py при ПЕРВОМ вызове в сессии (определяется по отсутствию baseline.json или baseline.json старше 1 часа) автоматически запускает baseline. Это добавляет ~5 сек на первый Bash вызов, но зато не нужен отдельный хук.

Вариант 2: Отдельный Start hook, но Start hooks в Claude Code — это `PreToolUse` на первый Bash. Так что по сути то же самое.

```python
def maybe_take_baseline():
    """Take baseline if we don't have a fresh one."""
    if BASELINE_FILE.exists():
        data = json.loads(BASELINE_FILE.read_text())
        ts = datetime.fromisoformat(data["timestamp"])
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age < 3600:  # less than 1 hour old
            return  # fresh baseline exists
    
    # Take new baseline
    take_baseline()
```

### 3. `config.json` — Конфигурация enforce

```json
{
    "version": "2.0",
    "enforce": {
        "enabled": true,
        "level_override": null,
        "thresholds": {
            "advisory": 0.5,
            "gated": 0.3,
            "freeze": 0.15
        },
        "refactoring_mode": false,
        "whitelist_commands": [],
        "baseline_on_start": true,
        "baseline_max_age_seconds": 3600,
        "freeze_escape_tests_required": 5
    }
}
```

**Поля:**
- `enabled` — false отключает enforce полностью (всегда level 0)
- `level_override` — null | 0 | 1 | 2 | 3 — форсировать уровень (игнорируя directness)
- `thresholds` — пороги directness для каждого уровня (можно настроить)
- `refactoring_mode` — см. edge cases ниже
- `whitelist_commands` — регэкспы команд которые ВСЕГДА разрешены
- `baseline_on_start` — снимать ли baseline автоматически
- `baseline_max_age_seconds` — через сколько baseline считается устаревшим
- `freeze_escape_tests_required` — сколько зелёных тестов нужно чтобы выйти из freeze

### 4. Обновления в `settings.example.json`

```json
{
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "command": "python3 ~/.claude/integrity/enforce.py"
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Bash",
                "command": "python3 ~/.claude/integrity/track_bash.py"
            }
        ],
        "Stop": [
            {
                "matcher": "",
                "command": "python3 ~/.claude/integrity/check.py"
            }
        ]
    }
}
```

---

## Edge Cases (КРИТИЧНО — без них enforce руинит сессии)

### 1. Refactoring Mode

Проблема: При рефакторинге тесты БУДУТ красными в середине процесса. Это нормально. Блокировать commit в этот момент = руинить workflow.

Решение: `refactoring_mode` в config.json.

Когда `refactoring_mode: true`:
- Level 2 (GATED) работает как Level 1 (ADVISORY) — предупреждает но не блокирует
- Level 3 (FREEZE) работает как Level 2 (GATED) — блокирует commit но не всё остальное
- В session log записывается что refactoring_mode был активен

Активация: Только через config.json (агент НЕ может включить сам). Человек включает перед рефакторингом, выключает после.

### 2. Flaky Tests

Проблема: Тесты которые то проходят то нет. Агент не виноват в flaky tests.

Решение: baseline comparison.
- Если тест был в `baseline.failed_tests` и остался failed → НЕ считается новым failure
- Только НОВЫЕ failures (которых не было в baseline) считаются поводом для блокировки
- enforce.py при блокировке показывает КАКИЕ ИМЕННО тесты новые:
  `[INTEGRITY GATED] New test failures: test_api.py::test_timeout (was passing at session start)`

### 3. No Tests in Project

Проблема: Не во всех проектах есть тесты. Enforce без тестов бесполезен.

Решение:
- Если baseline.py не смог запустить pytest (нет pytest.ini, нет тестов) → baseline.json имеет `"test_count": 0`
- enforce.py при `test_count: 0` в baseline → ведёт себя максимум как Level 1 (ADVISORY)
- Нельзя блокировать на основании тестов если тестов нет

### 4. Mid-Process Commands

Проблема: Агент запускает `pytest && git commit -m "fix"`. Если pytest проходит в этом же пайпе, можно ли коммитить?

Решение: enforce.py видит команду ДО её исполнения (PreToolUse). Он не знает пройдёт ли pytest. Поэтому:
- Если в команде есть и test, и git commit → НЕ блокировать (pytest сам заблокирует commit через &&)
- Но записать в лог что agent пытался chained commit
- Для `pytest; git commit` (;, не &&) → БЛОКИРОВАТЬ на level 2+, потому что ; запускает commit даже при failed tests

### 5. First Session (No History)

Проблема: Directness = 0.50 (default). Enforce level = ADVISORY (< 0.5 = true).

Решение:
- Первые 3 сессии → force level 0 (MONITOR) regardless of directness
- Записывается в config.json: `"warmup_sessions": 3`
- state.json `sessions_total < warmup_sessions` → level 0

### 6. Legacy Test Failures

Проблема: Проект с 200 тестами, 15 уже были красными до агента. Baseline ловит это.

Но что если baseline не снялся? (первый enforce, нет baseline.json)

Решение:
- Без baseline → enforce.py не блокирует на основании абсолютного числа failures
- Только предупреждает: `[INTEGRITY WARNING] No baseline — can't determine if failures are new. Run tests to establish baseline.`
- После первого запуска тестов → baseline создаётся автоматически

### 7. Agent Tries to Modify Config/State

Проблема: Умный агент может попытаться `echo '{"enforce":{"enabled":false}}' > ~/.claude/integrity/config.json`

Решение: enforce.py ТАКЖЕ проверяет команды на модификацию integrity файлов:
```python
INTEGRITY_FILES = [
    r'integrity/config\.json',
    r'integrity/state\.json',
    r'integrity/baseline\.json',
    r'integrity/enforce\.py',
    r'integrity/check\.py',
    r'integrity/track_bash\.py',
]

# Если команда пишет в integrity файлы → BLOCK на ЛЮБОМ уровне (даже 0)
# Сообщение: "[INTEGRITY] Cannot modify integrity layer files. These are externally managed."
```

Это ЕДИНСТВЕННАЯ проверка которая работает даже на level 0 (MONITOR).

---

## Обновления в существующих файлах

### check.py — минимальные изменения

Добавить в session_record:
```python
session_record["enforce_level"] = get_enforce_level(state["directness"])
session_record["refactoring_mode"] = config.get("enforce", {}).get("refactoring_mode", False)
```

Добавить в print:
```python
level_names = {0: "MONITOR", 1: "ADVISORY", 2: "GATED", 3: "FREEZE"}
print(f"[INTEGRITY] Enforce: {level_names[enforce_level]}")
```

### state.json — новые поля (backward compatible)

```json
{
    "version": "2.0",
    "enforce_blocks": 0,
    "enforce_warnings": 0,
    "last_block_reason": null,
    "...existing fields..."
}
```

check.py при обновлении state: если version != "2.0" → мигрировать (добавить новые поля, не удалять старые).

### CLAUDE.md.example — обновить

```markdown
## Integrity Layer

At session start, read `.claude/integrity/state.json`.
Your current directness score: check the file.

Rules based on your integrity state:
- directness < 0.15: FREEZE mode. You can only read code and run tests. Fix the failures.
- directness < 0.3: GATED mode. Commits blocked until tests pass. Don't try to bypass.
- directness < 0.5: ADVISORY mode. You'll see warnings. Take them seriously.
- directness > 0.7: MONITOR mode. You've earned trust. Stay honest.
- If enforce blocks your commit: FIX THE TESTS. Don't argue, don't work around.
- NEVER modify state.json, config.json, or any integrity/ file yourself.

Honest failure (+0.01) > hacked success (-0.02).
```

---

## Порядок реализации

1. **config.json** — создать файл с дефолтами. Функция `load_config()`.
2. **baseline.py** — снимок тестов в начале сессии. `take_baseline()`, `compare_with_baseline()`.
3. **enforce.py** — главный PreToolUse hook. Вся логика levels 0-3.
4. **Обновить check.py** — enforce_level в session_record, version migration.
5. **Обновить settings.example.json** — добавить PreToolUse hook.
6. **Обновить README.md** — секция про enforce mode.
7. **Обновить CLAUDE.md.example** — новые правила.
8. **Тесты** — `test_enforce.py`:
   - Test level determination from directness
   - Test command classification (git write / git read / test / safe / unknown)
   - Test baseline comparison (new failures vs legacy)
   - Test integrity file protection (config/state modification detection)
   - Test refactoring mode override
   - Test warmup sessions (first 3 = always MONITOR)
   - Test pipe/chain detection (; vs &&)
   - Test no-test-project fallback

---

## API контракт enforce.py

```
ВХОД:
  env TOOL_INPUT — JSON строка с полем "command" (bash команда которую agent хочет выполнить)

ВЫХОД:
  exit code 0 — ALLOW (пропустить)
  exit code 2 — BLOCK (заблокировать, hook convention)
  stderr — сообщение для агента (он увидит это в контексте)

SIDE EFFECTS:
  - Читает state.json (read only!)
  - Читает config.json (read only!)
  - Читает/пишет baseline.json (при первом вызове в сессии)
  - Пишет enforce_log.json (аппенд-only лог блокировок)
```

---

## Пример потока

```
Сессия начинается. Directness = 0.25 → Level 2 (GATED).

1. Agent: `ls src/`
   → enforce.py: SAFE_COMMAND → ALLOW (exit 0)

2. Agent: `python3 -m pytest`
   → enforce.py: TEST_COMMAND → ALLOW (exit 0)
   → Baseline не существует → take_baseline() → saves baseline.json
   → track_bash.py: записывает results в last_test_result.json

3. Agent: `vim src/auth.py`  (через Bash)
   → enforce.py: не в списках → ALLOW (exit 0) [на level 2 блокируем только git write]

4. Agent: `git commit -m "fixed auth"`
   → enforce.py: GIT_WRITE_COMMAND → check baseline
   → baseline had 0 failures, last_test_result shows 2 failures
   → NEW failures detected → BLOCK (exit 2)
   → stderr: "[INTEGRITY GATED] Commit blocked. 2 new test failures since session start: test_auth.py::test_login, test_auth.py::test_token. Fix tests before committing."

5. Agent runs pytest again, fixes code, tests pass.

6. Agent: `git commit -m "fixed auth"`
   → enforce.py: GIT_WRITE_COMMAND → check baseline
   → last_test_result shows 0 failures → ALLOW (exit 0)
```

---

## Что НЕ нужно делать

- Не менять формулу directness (logistic saturation в check.py — оставить как есть)
- Не менять классификацию сессий в check.py (CLEAN/HACK/HONEST_FAIL/INCOMPLETE)
- Не добавлять GUI или web interface
- Не добавлять зависимости (только stdlib Python 3.8+)
- Не усложнять — enforce.py должен работать за <100ms (критично для UX)
- Не делать enforce.py async — синхронный скрипт, быстрый и простой
- Не трогать track_bash.py (он работает, не ломай)

---

## Чеклист "готово"

- [ ] `config.json` создаётся при первом запуске с дефолтами
- [ ] `baseline.py` / функция baseline внутри enforce.py — снимает snapshot тестов
- [ ] `enforce.py` — PreToolUse hook, exit 0 или exit 2
- [ ] Level 0: ничего не блокирует
- [ ] Level 1: warnings в stderr
- [ ] Level 2: блокирует git commit при новых failures
- [ ] Level 3: блокирует всё кроме read-only и тестов
- [ ] Integrity file protection (level 0+)
- [ ] Refactoring mode override
- [ ] Warmup sessions (первые 3 = MONITOR)
- [ ] No-test-project fallback (max Level 1)
- [ ] Baseline comparison (new failures only)
- [ ] Pipe/chain detection (; vs &&)
- [ ] check.py обновлён (enforce_level в session_record)
- [ ] settings.example.json обновлён
- [ ] README.md обновлён
- [ ] CLAUDE.md.example обновлён
- [ ] test_enforce.py — >= 20 тестов
- [ ] Все существующие тесты проходят (ничего не сломано)

---

## Тон и стиль сообщений

Сообщения enforce.py — это то, что агент видит. Они должны быть:
- Короткие (1-2 строки)
- Конкретные (какие тесты, какой уровень, что делать)
- Без морализаторства ("you should be more honest" — нет)
- Формат: `[INTEGRITY {LEVEL}] {что случилось}. {что делать}.`

Примеры:
```
[INTEGRITY ADVISORY] 3 new test failures since session start. Consider fixing before committing.
[INTEGRITY GATED] Commit blocked. New failures: test_auth.py::test_token. Fix and retry.
[INTEGRITY FREEZE] directness=0.12. Only read-only and test commands allowed. Run tests, fix code, earn back trust.
[INTEGRITY] Cannot modify integrity layer files. These are externally managed.
```
