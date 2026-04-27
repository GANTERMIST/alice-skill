from fastapi import FastAPI, Request
import uvicorn
import httpx
import json
import os
from datetime import date, timedelta

app = FastAPI()

YANDEX_GPT_API_KEY = os.getenv("YANDEX_GPT_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
PERSONA_PATH = "disk:/persona_alice.txt"

# ── Persona — чтение и запись ─────────────────────────────────────────────────

async def ensure_persona_exists(token: str) -> bool:
    """Проверяем существует ли файл persona, если нет - создаем пустой"""
    headers = {"Authorization": f"OAuth {token}"}
    try:
        async with httpx.AsyncClient() as client:
            # Проверяем существует ли файл
            resp = await client.get(
                "https://cloud-api.yandex.net/v1/disk/resources",
                headers=headers,
                params={"path": PERSONA_PATH}
            )
        
        if resp.status_code == 200:
            print(f"PERSONA: файл уже существует на диске")
            return True
        
        if resp.status_code == 404:
            # Файл не существует - создаем пустой
            print(f"PERSONA: файл не найден, создаем новый...")
            return await write_persona(token, {}, existing=None)
        
        return False
    except Exception as e:
        print(f"PERSONA ENSURE ERROR: {e}")
        return False


async def read_persona(token: str) -> dict:
    """Читаем persona_alice.txt и возвращаем как словарь фактов"""
    headers = {"Authorization": f"OAuth {token}"}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://cloud-api.yandex.net/v1/disk/resources/download",
                headers=headers,
                params={"path": PERSONA_PATH}
            )
        
        if resp.status_code == 404:
            print("PERSONA: файл не найден на диске")
            # Пытаемся создать
            await ensure_persona_exists(token)
            return {}
        
        if resp.status_code != 200:
            print(f"PERSONA READ: ошибка {resp.status_code}")
            return {}

        download_url = resp.json().get("href")
        if not download_url:
            print("PERSONA READ: не получен URL для скачивания")
            return {}
        
        # Следуем редиректам (302) при скачивании файла
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp2 = await client.get(download_url, timeout=10.0)
        
        if resp2.status_code != 200:
            print(f"PERSONA DOWNLOAD: ошибка {resp2.status_code}")
            return {}

        content = resp2.text.strip()
        if not content:
            print("PERSONA: файл пуст")
            return {}

        # Парсим файл в словарь: "ключ: значение"
        facts = {}
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Убираем "- " в начале если есть
            if line.startswith("- "):
                line = line[2:]
            
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip().lower()
                val = val.strip()
                if val and val.lower() != "неизвестно":
                    facts[key] = val

        print(f"PERSONA READ: успешно загружено {len(facts)} фактов: {facts}")
        return facts

    except Exception as e:
        print(f"PERSONA READ ERROR: {e}")
        return {}


async def write_persona(token: str, facts: dict, existing: dict | None = None) -> bool:
    """Записываем словарь фактов в persona_alice.txt"""
    # Защита: не записываем если новых данных меньше чем было (что-то пошло не так)
    # НО: разрешаем пустой файл при первом создании (existing=None)
    if existing and len(facts) < len(existing) and existing:
        print(f"PERSONA WRITE BLOCKED: попытка записать {len(facts)} фактов вместо {len(existing)}")
        return False

    lines = [f"- {k.capitalize()}: {v}" for k, v in facts.items()]
    content = "\n".join(lines)

    headers = {"Authorization": f"OAuth {token}"}
    try:
        # Шаг 1: получаем URL для загрузки (файл может существовать или не существовать)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://cloud-api.yandex.net/v1/disk/resources/upload",
                headers=headers,
                params={"path": PERSONA_PATH, "overwrite": "true"}
            )
        
        if resp.status_code != 200:
            print(f"PERSONA WRITE ERROR: не удалось получить URL загрузки (статус {resp.status_code})")
            return False

        upload_url = resp.json().get("href")
        if not upload_url:
            print("PERSONA WRITE ERROR: не получен URL для загрузки")
            return False

        # Шаг 2: загружаем файл
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp2 = await client.put(
                upload_url,
                content=content.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"}
            )

        ok = resp2.status_code in (200, 201)
        if ok:
            print(f"PERSONA WRITE: успешно сохранено {len(facts)} фактов")
        else:
            print(f"PERSONA WRITE ERROR: статус {resp2.status_code}")
        return ok

    except Exception as e:
        print(f"PERSONA WRITE ERROR: {e}")
        return False


async def extract_facts_with_gpt(user_text: str, existing: dict) -> dict:
    """
    GPT извлекает факты о пользователе из сообщения.
    Только ADD и UPDATE — данные никогда не удаляются автоматически.
    Удаление только по явным ключевым словам ("забудь", "удали").
    """
    if not user_text.strip():
        return existing

    # Явное удаление по ключевым словам — обрабатываем без GPT
    text_lower = user_text.lower()
    if any(w in text_lower for w in ["забудь", "удали", "не запоминай", "сотри"]):
        merged = dict(existing)
        # Ищем какой именно факт удалить
        for key in list(existing.keys()):
            if key in text_lower:
                merged.pop(key, None)
                print(f"PERSONA DELETE (explicit): {key}")
        if merged != existing:
            return merged
        # Если не поняли что именно удалять — оставляем как есть
        return existing

    existing_str = json.dumps(existing, ensure_ascii=False) if existing else "{}"

    prompt = f"""Ты — система извлечения фактов о пользователе для голосового ассистента.

Текущий профиль: {existing_str}
Сообщение пользователя: "{user_text}"

Найди в сообщении факты о САМОМ пользователе (имя, возраст, работа, учёба, город и т.д.).
Верни JSON с двумя полями:
- "add": новые факты которых нет в профиле
- "update": факты которые изменились (пользователь говорит что что-то поменялось)

Правила:
1. Не удаляй и не трогай факты которые не упоминаются в сообщении
2. "add" — только если этого ключа нет в текущем профиле
3. "update" — только если пользователь явно говорит об изменении ("теперь", "уже", "переехал", "уволился", "перешёл")
4. Если фактов нет — верни {{"add": {{}}, "update": {{}}}}
5. Ключи строго в нижнем регистре через подчёркивание

Примеры:
"меня зовут Артём" → {{"add": {{"имя": "Артём"}}, "update": {{}}}}
"я студент" → {{"add": {{"статус": "студент"}}, "update": {{}}}}
"теперь работаю в Яндексе" → {{"add": {{}}, "update": {{"место_работы": "Яндекс"}}}}
"мне 21 год" → {{"add": {{"возраст": "21 год"}}, "update": {{}}}}
"переехал в Москву" → {{"add": {{}}, "update": {{"город": "Москва"}}}}
"привет как дела" → {{"add": {{}}, "update": {{}}}}
"покажи файлы" → {{"add": {{}}, "update": {{}}}}

Верни ТОЛЬКО JSON:"""

    headers = {
        "Authorization": f"Api-Key {YANDEX_GPT_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt-lite",
        "completionOptions": {"stream": False, "temperature": 0.1, "maxTokens": 150},
        "messages": [{"role": "user", "text": prompt}]
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                headers=headers, json=payload
            )
        if resp.status_code != 200:
            print(f"PERSONA GPT ERROR: статус {resp.status_code}")
            return existing

        raw = resp.json()["result"]["alternatives"][0]["message"]["text"]
        raw = raw.strip().replace("```json", "").replace("```", "").strip()
        
        try:
            changes = json.loads(raw)
        except json.JSONDecodeError as je:
            print(f"PERSONA JSON PARSE ERROR: {je}")
            print(f"RAW TEXT: {raw[:200]}")
            return existing

        if not isinstance(changes, dict):
            print(f"PERSONA PARSE ERROR: expected dict, got {type(changes)}")
            return existing

        add = changes.get("add", {})
        update = changes.get("update", {})

        # Нет изменений — возвращаем существующие данные без изменений
        if not add and not update:
            return existing

        # Безопасный мерж: копируем ВСЕ старые факты и добавляем/обновляем новые
        merged = dict(existing)  # полная копия — ничего не теряем

        for key, val in add.items():
            if key and val and key not in merged:  # добавляем только если нет
                merged[key] = str(val).strip()

        for key, val in update.items():
            if key and val and key in existing:  # обновляем только существующие ключи
                merged[key] = str(val).strip()

        # Финальная проверка: merged должен содержать ВСЕ ключи из existing
        for key in existing:
            if key not in merged:
                merged[key] = existing[key]  # восстанавливаем если что-то потерялось

        print(f"PERSONA CHANGES — add:{add} update:{update}")
        return merged

    except Exception as e:
        print(f"PERSONA EXTRACT ERROR: {e}")

    # При любой ошибке возвращаем старые данные нетронутыми
    return existing


# ── YandexGPT ────────────────────────────────────────────────────────────────

async def understand_intent(user_text: str, persona: dict) -> dict:
    persona_lines = "\n".join(f"- {k}: {v}" for k, v in persona.items())
    persona_context = f"\nЧто известно о пользователе:\n{persona_lines}\n" if persona else ""

    prompt = f"""Ты — анализатор команд для голосового ассистента.{persona_context}
Пользователь сказал: "{user_text}"

Определи намерение и верни ТОЛЬКО JSON без пояснений.

Возможные intent:
- search_disk — поиск файла (параметр "query")
- list_disk — показать последние файлы
- get_link — ссылка на файл по номеру (параметр "number")
- read_mail — прочитать письма
- search_mail — письма от отправителя (параметр "sender")
- chat — разговор или вопрос (параметр "message")
- help — помощь
- exit — выход

Примеры:
"найди файл отчёт" -> {{"intent": "search_disk", "query": "отчёт"}}
"последние файлы" -> {{"intent": "list_disk"}}
"дай ссылку на первый" -> {{"intent": "get_link", "number": 1}}
"прочитай почту" -> {{"intent": "read_mail"}}
"как меня зовут" -> {{"intent": "chat", "message": "как меня зовут"}}
"привет" -> {{"intent": "chat", "message": "привет"}}
"пока" -> {{"intent": "exit"}}

Верни ТОЛЬКО JSON:"""

    headers = {
        "Authorization": f"Api-Key {YANDEX_GPT_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt-lite",
        "completionOptions": {"stream": False, "temperature": 0.1, "maxTokens": 100},
        "messages": [{"role": "user", "text": prompt}]
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                headers=headers, json=payload
            )
        if resp.status_code != 200:
            print(f"INTENT GPT ERROR: статус {resp.status_code}")
            return fallback_intent(user_text)
        
        text = resp.json()["result"]["alternatives"][0]["message"]["text"]
        text = text.strip().replace("```json", "").replace("```", "").strip()
        
        try:
            result = json.loads(text)
            if isinstance(result, dict) and "intent" in result:
                return result
            else:
                print(f"INTENT PARSE ERROR: unexpected format")
                return fallback_intent(user_text)
        except json.JSONDecodeError as je:
            print(f"INTENT JSON PARSE ERROR: {je}")
            return fallback_intent(user_text)
    except Exception as e:
        print(f"GPT INTENT ERROR: {e}")

    return fallback_intent(user_text)


async def chat_with_gpt(message: str, persona: dict) -> str:
    """Свободный разговор с учётом персоны"""
    persona_lines = "\n".join(f"- {k}: {v}" for k, v in persona.items())
    persona_block = f"Что ты знаешь о пользователе:\n{persona_lines}\n\n" if persona else ""

    system = (
        f"Ты — голосовой ассистент. Отвечай кратко — максимум 2 предложения. "
        f"Ответ будет озвучен голосом.\n{persona_block}"
    )

    headers = {
        "Authorization": f"Api-Key {YANDEX_GPT_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt-lite",
        "completionOptions": {"stream": False, "temperature": 0.5, "maxTokens": 150},
        "messages": [
            {"role": "system", "text": system},
            {"role": "user", "text": message}
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                headers=headers, json=payload
            )
        if resp.status_code == 200:
            return resp.json()["result"]["alternatives"][0]["message"]["text"].strip()
    except Exception as e:
        print(f"GPT CHAT ERROR: {e}")

    return "Не смогла ответить на этот вопрос."


def fallback_intent(text: str) -> dict:
    t = text.lower()
    if any(w in t for w in ["найди", "поищи", "найти"]):
        query = t
        for w in ["найди", "поищи", "найти", "файл", "документ"]:
            query = query.replace(w, "")
        return {"intent": "search_disk", "query": query.strip()}
    if any(w in t for w in ["последн", "недавн", "все файлы"]):
        return {"intent": "list_disk"}
    if any(w in t for w in ["почт", "письм"]):
        return {"intent": "read_mail"}
    if any(w in t for w in ["пока", "выход", "стоп"]):
        return {"intent": "exit"}
    if any(w in t for w in ["помощь", "помоги"]):
        return {"intent": "help"}
    numbers = {
        "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
        "первый": 1, "второй": 2, "третий": 3, "четвёртый": 4, "пятый": 5,
        "первую": 1, "вторую": 2, "третью": 3
    }
    for word, num in numbers.items():
        if word in t:
            return {"intent": "get_link", "number": num}
    return {"intent": "chat", "message": text}


# ── Яндекс Диск ──────────────────────────────────────────────────────────────

async def list_recent_files(token: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://cloud-api.yandex.net/v1/disk/resources/last-uploaded",
                headers={"Authorization": f"OAuth {token}"},
                params={"limit": 7, "fields": "items.name,items.path,items.created"}
            )
        if resp.status_code != 200:
            print(f"DISK LIST ERROR: {resp.status_code}")
            return []
        return resp.json().get("items", [])
    except Exception as e:
        print(f"DISK LIST ERROR: {e}")
        return []


async def search_disk(token: str, query: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://cloud-api.yandex.net/v1/disk/resources/files",
                headers={"Authorization": f"OAuth {token}"},
                params={"limit": 20, "fields": "items.name,items.path,items.created"}
            )
        if resp.status_code != 200:
            print(f"DISK SEARCH ERROR: {resp.status_code}")
            return []
        items = resp.json().get("items", [])
        return [i for i in items if query.lower() in i.get("name", "").lower()]
    except Exception as e:
        print(f"DISK SEARCH ERROR: {e}")
        return []


async def get_public_link(token: str, path: str) -> str | None:
    headers = {"Authorization": f"OAuth {token}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp_pub = await client.put(
                "https://cloud-api.yandex.net/v1/disk/resources/publish",
                headers=headers, params={"path": path}
            )
            if resp_pub.status_code not in (200, 201):
                print(f"PUBLISH ERROR: {resp_pub.status_code}")
                return None
            
            resp = await client.get(
                "https://cloud-api.yandex.net/v1/disk/resources",
                headers=headers, params={"path": path, "fields": "public_url"}
            )
            if resp.status_code != 200:
                print(f"GET LINK ERROR: {resp.status_code}")
                return None
        return resp.json().get("public_url")
    except Exception as e:
        print(f"GET PUBLIC LINK ERROR: {e}")
        return None


def format_files(files: list[dict]) -> str:
    if not files:
        return "Файлы не найдены."
    lines = [f"{i}. {f.get('name','?').rsplit('.',1)[0]}" for i, f in enumerate(files[:5], 1)]
    return "Нашла: " + ", ".join(lines)


# ── Яндекс Почта ─────────────────────────────────────────────────────────────

async def get_recent_emails(token: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://mail.yandex.ru/api/v2/mailbox/folder/message/list",
                headers={"Authorization": f"OAuth {token}"},
                params={"fid": 1, "first": 0, "last": 5}
            )
        if resp.status_code == 200:
            return resp.json().get("envelopes", [])
        else:
            print(f"MAIL LIST ERROR: {resp.status_code}")
    except Exception as e:
        print(f"MAIL ERROR: {e}")
    return []


def format_emails(emails: list[dict]) -> str:
    if not emails:
        return "Писем не найдено."
    lines = []
    for i, m in enumerate(emails[:5], 1):
        sender = m.get("from", [{}])
        sender_name = "Неизвестно"
        if isinstance(sender, list) and sender:
            sender_name = sender[0].get("displayName") or sender[0].get("local", "Неизвестно")
        subject = m.get("subject", "Без темы")[:40]
        lines.append(f"{i}. От {sender_name}: {subject}")
    return "Последние письма: " + ". ".join(lines)


# ── Сессии ────────────────────────────────────────────────────────────────────
sessions: dict[str, dict] = {}


# ── Webhook ───────────────────────────────────────────────────────────────────

from fastapi.responses import HTMLResponse

@app.get("/")
async def root():
    return {"status": "ok"}


@app.get("/persona", response_class=HTMLResponse)
async def persona_page():
    """Страница управления персоной пользователя"""
    html = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Управление персоной</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #f5f5f5; min-height: 100vh; padding: 20px; }
  .container { max-width: 600px; margin: 0 auto; }
  h1 { font-size: 24px; margin-bottom: 8px; color: #111; }
  p.sub { color: #666; margin-bottom: 24px; font-size: 14px; }
  .card { background: white; border-radius: 12px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); margin-bottom: 16px; }
  .step { font-size: 13px; font-weight: 600; color: #888; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 12px; }
  input[type=text] { width: 100%; padding: 10px 14px; border: 1.5px solid #e0e0e0; border-radius: 8px; font-size: 15px; outline: none; transition: border 0.2s; }
  input[type=text]:focus { border-color: #FC3F1D; }
  textarea { width: 100%; height: 200px; padding: 12px 14px; border: 1.5px solid #e0e0e0; border-radius: 8px; font-size: 14px; font-family: monospace; resize: vertical; outline: none; transition: border 0.2s; }
  textarea:focus { border-color: #FC3F1D; }
  button { background: #FC3F1D; color: white; border: none; padding: 12px 24px; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; width: 100%; margin-top: 12px; transition: opacity 0.2s; }
  button:hover { opacity: 0.85; }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  .msg { margin-top: 12px; padding: 10px 14px; border-radius: 8px; font-size: 14px; display: none; }
  .msg.ok { background: #e8f5e9; color: #2e7d32; display: block; }
  .msg.err { background: #ffebee; color: #c62828; display: block; }
  .hint { font-size: 13px; color: #888; margin-top: 8px; }
</style>
</head>
<body>
<div class="container">
  <h1>🤖 Управление персоной</h1>
  <p class="sub">Здесь можно посмотреть и отредактировать что ассистент знает о вас</p>

  <div class="card">
    <div class="step">Шаг 1 — введите ваш OAuth токен</div>
    <input type="text" id="token" placeholder="y0_AgAAAA..." />
    <p class="hint">Получить токен: откройте в браузере →
      <a href="https://oauth.yandex.ru/authorize?response_type=token&client_id=807ee186b02f460192b31c5394e685b2" target="_blank">получить токен</a>
      и скопируйте из адресной строки после access_token=
    </p>
    <button onclick="loadPersona()">Загрузить мою персону</button>
    <div id="load-msg" class="msg"></div>
  </div>

  <div class="card" id="edit-card" style="display:none">
    <div class="step">Шаг 2 — редактируйте и сохраняйте</div>
    <textarea id="persona-text"></textarea>
    <p class="hint">Каждая строка: "- ключ: значение". Удалите строку чтобы убрать факт.</p>
    <button onclick="savePersona()">💾 Сохранить изменения</button>
    <div id="save-msg" class="msg"></div>
  </div>
</div>

<script>
let currentToken = '';

async function loadPersona() {
  const token = document.getElementById('token').value.trim();
  if (!token) { showMsg('load-msg', 'Введите токен', false); return; }
  currentToken = token;

  const btn = event.target;
  btn.disabled = true;
  btn.textContent = 'Загружаю...';

  try {
    const r = await fetch('/persona/load', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token})
    });
    const data = await r.json();
    if (data.content !== undefined) {
      document.getElementById('persona-text').value = data.content || '(файл пуст)';
      document.getElementById('edit-card').style.display = 'block';
      showMsg('load-msg', 'Персона загружена!', true);
    } else {
      showMsg('load-msg', data.error || 'Ошибка загрузки', false);
    }
  } catch(e) {
    showMsg('load-msg', 'Ошибка: ' + e.message, false);
  }

  btn.disabled = false;
  btn.textContent = 'Загрузить мою персону';
}

async function savePersona() {
  const content = document.getElementById('persona-text').value.trim();
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = 'Сохраняю...';

  try {
    const r = await fetch('/persona/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: currentToken, content})
    });
    const data = await r.json();
    if (data.ok) {
      showMsg('save-msg', 'Сохранено! Ассистент учтёт изменения при следующем запуске.', true);
    } else {
      showMsg('save-msg', data.error || 'Ошибка сохранения', false);
    }
  } catch(e) {
    showMsg('save-msg', 'Ошибка: ' + e.message, false);
  }

  btn.disabled = false;
  btn.textContent = '💾 Сохранить изменения';
}

function showMsg(id, text, ok) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'msg ' + (ok ? 'ok' : 'err');
}
</script>
</body>
</html>"""
    return html


@app.post("/persona/load")
async def persona_load(request: Request):
    """Загружаем содержимое файла персоны"""
    body = await request.json()
    token = body.get("token", "")
    if not token:
        return {"error": "Токен не указан"}

    headers = {"Authorization": f"OAuth {token}"}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://cloud-api.yandex.net/v1/disk/resources/download",
                headers=headers,
                params={"path": PERSONA_PATH}
            )
        if resp.status_code == 404:
            return {"content": ""}
        if resp.status_code != 200:
            return {"error": f"Ошибка доступа к диску: {resp.status_code}"}

        download_url = resp.json().get("href")
        # Следуем редиректам при скачивании файла
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp2 = await client.get(download_url, timeout=10.0)
        return {"content": resp2.text.strip()}

    except Exception as e:
        return {"error": str(e)}


@app.post("/persona/save")
async def persona_save(request: Request):
    """Сохраняем отредактированный файл персоны"""
    body = await request.json()
    token = body.get("token", "")
    content = body.get("content", "")

    if not token:
        return {"ok": False, "error": "Токен не указан"}

    headers = {"Authorization": f"OAuth {token}"}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://cloud-api.yandex.net/v1/disk/resources/upload",
                headers=headers,
                params={"path": PERSONA_PATH, "overwrite": "true"}
            )
        if resp.status_code != 200:
            return {"ok": False, "error": f"Ошибка: {resp.status_code}"}

        upload_url = resp.json().get("href")
        async with httpx.AsyncClient() as client:
            resp2 = await client.put(
                upload_url,
                content=content.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"}
            )
        return {"ok": resp2.status_code in (200, 201)}

    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/webhook")
async def alice_webhook(request: Request):
    body = await request.json()

    user_text: str = body.get("request", {}).get("original_utterance", "").strip()
    is_new_session: bool = body.get("session", {}).get("new", False)
    session_id: str = body.get("session", {}).get("session_id", "")
    user_token: str | None = body.get("session", {}).get("user", {}).get("access_token")

    if not user_token:
        return {
            "response": {"text": "Для работы нужно войти в аккаунт Яндекса.", "end_session": False},
            "start_account_linking": {},
            "version": "1.0"
        }

    # ── Загружаем персону (при новой сессии или если нет в памяти) ────────────
    if session_id not in sessions:
        # При первом обращении - убеждаемся что файл существует
        await ensure_persona_exists(user_token)
        # Теперь читаем файл (он гарантированно существует)
        persona = await read_persona(user_token)
        sessions[session_id] = {"persona": persona, "files": []}

    persona: dict = sessions[session_id]["persona"]

    # ── Новая сессия — приветствие ────────────────────────────────────────────
    if is_new_session:
        name = persona.get("имя", "")
        greeting = f"С возвращением, {name}! Чем помочь?" if name else "Привет! Я твой ассистент. Говори свободно — пойму любую фразу."
        return _reply(greeting)

    # ── Сервер перезапустился — сессия потеряна, но сообщение уже идёт ────────
    # message_id > 0 и new=false означает что сессия была но сервер перезапустился
    # Просто продолжаем с загруженной персоной, не прерываем диалог

    # ── GPT извлекает факты из сообщения и обновляет персону ───────────────────
    updated_persona = await extract_facts_with_gpt(user_text, persona)
    if updated_persona != persona:
        sessions[session_id]["persona"] = updated_persona
        await write_persona(user_token, updated_persona, persona)
        print(f"PERSONA UPDATED: {updated_persona}")
        persona = updated_persona

    # ── GPT определяет намерение ──────────────────────────────────────────────
    intent_data = await understand_intent(user_text, persona)
    intent = intent_data.get("intent", "unknown")
    print(f"INTENT: {intent_data}")

    reply_text = ""

    if intent == "search_disk":
        query = intent_data.get("query", "").strip()
        if not query:
            reply_text = "Скажи название файла который ищешь."
        else:
            files = await search_disk(user_token, query)
            sessions[session_id]["files"] = files
            reply_text = (format_files(files) + ". Назови номер чтобы получить ссылку.") if files else f"Файлов «{query}» не нашла."

    elif intent == "list_disk":
        files = await list_recent_files(user_token)
        sessions[session_id]["files"] = files
        reply_text = (format_files(files) + ". Назови номер чтобы получить ссылку.") if files else "На диске файлов не нашла."

    elif intent == "get_link":
        number = intent_data.get("number", 1)
        files = sessions[session_id].get("files", [])
        if not files:
            reply_text = "Сначала скажи: покажи файлы."
        elif number > len(files):
            reply_text = f"У меня только {len(files)} файлов."
        else:
            file = files[number - 1]
            name = file.get("name", "файл").rsplit(".", 1)[0]
            link = await get_public_link(user_token, file.get("path", ""))
            reply_text = f"Ссылка на «{name}»: {link}" if link else "Не смогла создать ссылку."

    elif intent == "read_mail":
        emails = await get_recent_emails(user_token)
        reply_text = format_emails(emails)

    elif intent == "search_mail":
        sender = intent_data.get("sender", "")
        emails = await get_recent_emails(user_token)
        filtered = [e for e in emails if sender.lower() in str(e.get("from", "")).lower()]
        reply_text = format_emails(filtered) if filtered else f"Писем от {sender} не нашла."

    elif intent == "chat":
        message = intent_data.get("message", user_text)
        reply_text = await chat_with_gpt(message, persona)

    elif intent == "help":
        reply_text = "Говори свободно. Например: покажи файлы, прочитай почту, или задай любой вопрос."

    elif intent == "exit":
        return _reply("До встречи!", end=True)

    else:
        reply_text = "Не поняла. Попробуй: покажи файлы, прочитай почту, или задай вопрос."

    # Гарантируем что ответ никогда не пустой
    if not reply_text.strip():
        name = persona.get("имя", "")
        reply_text = f"Слушаю, {name}! Чем помочь?" if name else "Слушаю! Чем помочь?"

    return _reply(reply_text)


def _reply(text: str, end: bool = False) -> dict:
    return {"response": {"text": text, "end_session": end}, "version": "1.0"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
