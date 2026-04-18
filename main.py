from fastapi import FastAPI, Request
import uvicorn
import httpx
import json
import os
import base64
from datetime import date, timedelta

app = FastAPI()

YANDEX_GPT_API_KEY = os.getenv("YANDEX_GPT_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
PERSONA_FILENAME = "persona_alice.txt"
PERSONA_DISK_PATH = f"disk:/{PERSONA_FILENAME}"

# ── Persona — чтение и запись на Яндекс Диск ─────────────────────────────────

async def read_persona(token: str) -> str | None:
    """Читаем файл persona_alice.txt с диска пользователя"""
    headers = {"Authorization": f"OAuth {token}"}
    try:
        # Получаем ссылку на скачивание
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://cloud-api.yandex.net/v1/disk/resources/download",
                headers=headers,
                params={"path": PERSONA_DISK_PATH}
            )
        if resp.status_code != 200:
            print(f"PERSONA READ: файл не найден ({resp.status_code})")
            return None

        download_url = resp.json().get("href")
        if not download_url:
            return None

        # Скачиваем содержимое
        async with httpx.AsyncClient() as client:
            resp2 = await client.get(download_url)

        content = resp2.text.strip()
        print(f"PERSONA READ: прочитано {len(content)} символов")
        return content if content else None  # пустой файл = нет персоны

    except Exception as e:
        print(f"PERSONA READ ERROR: {e}")
        return None


async def write_persona(token: str, content: str) -> bool:
    """Записываем/обновляем файл persona_alice.txt на диске"""
    headers = {"Authorization": f"OAuth {token}"}
    try:
        # Получаем ссылку на загрузку (overwrite=true — перезапишет если есть)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://cloud-api.yandex.net/v1/disk/resources/upload",
                headers=headers,
                params={"path": PERSONA_DISK_PATH, "overwrite": "true"}
            )
        if resp.status_code != 200:
            print(f"PERSONA WRITE: не удалось получить URL ({resp.status_code})")
            return False

        upload_url = resp.json().get("href")
        if not upload_url:
            return False

        # Загружаем файл
        async with httpx.AsyncClient() as client:
            resp2 = await client.put(
                upload_url,
                content=content.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"}
            )

        success = resp2.status_code in (200, 201)
        print(f"PERSONA WRITE: {'успешно' if success else 'ошибка ' + str(resp2.status_code)}")
        return success

    except Exception as e:
        print(f"PERSONA WRITE ERROR: {e}")
        return False


async def update_persona(token: str, old_persona: str | None, user_text: str, assistant_reply: str) -> str | None:
    """Обновляем портрет пользователя через GPT после каждого сообщения"""
    old_text = old_persona or "Данных пока нет."

    prompt = f"""Ты — система создания портрета пользователя для голосового ассистента.

Текущий портрет пользователя:
{old_text}

Пользователь только что сказал: "{user_text}"
Ассистент ответил: "{assistant_reply}"

Обнови портрет пользователя — добавь новые факты которые можно узнать из этого диалога.
Портрет должен содержать:
- Имя (если известно)
- Интересы и предпочтения
- Часто используемые команды
- Рабочий контекст (файлы которые ищет, темы писем)
- Любые другие полезные детали

Пиши кратко, в формате списка фактов. Не повторяй то что уже есть.
Верни ТОЛЬКО обновлённый портрет, без пояснений:"""

    headers = {
        "Authorization": f"Api-Key {YANDEX_GPT_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt-lite",
        "completionOptions": {"stream": False, "temperature": 0.3, "maxTokens": 300},
        "messages": [{"role": "user", "text": prompt}]
    }

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                headers=headers, json=payload
            )
        if resp.status_code == 200:
            new_persona = resp.json()["result"]["alternatives"][0]["message"]["text"]
            await write_persona(token, new_persona)
            print(f"PERSONA UPDATED: {new_persona[:100]}...")
            return new_persona
    except Exception as e:
        print(f"PERSONA UPDATE ERROR: {e}")


# ── YandexGPT — понимание намерений ──────────────────────────────────────────

async def understand_intent(user_text: str, persona: str | None) -> dict:
    persona_context = f"\nКонтекст о пользователе:\n{persona}\n" if persona else ""

    prompt = f"""Ты — анализатор команд для голосового ассистента.{persona_context}
Пользователь сказал: "{user_text}"

Определи намерение и верни ТОЛЬКО JSON без пояснений:

Возможные intent:
- search_disk — поиск файла на диске (нужен параметр "query")
- list_disk — показать последние файлы на диске
- get_link — получить ссылку на файл по номеру (нужен параметр "number" от 1 до 5)
- read_mail — прочитать последние письма
- search_mail — найти письма от отправителя (нужен параметр "sender")
- chat — просто поговорить или задать вопрос (нужен параметр "message")
- help — помощь
- exit — выход
- unknown — непонятная команда

Примеры:
"найди файл отчёт" -> {{"intent": "search_disk", "query": "отчёт"}}
"покажи документ про бюджет" -> {{"intent": "search_disk", "query": "бюджет"}}
"последние файлы" -> {{"intent": "list_disk"}}
"дай ссылку на первый" -> {{"intent": "get_link", "number": 1}}
"прочитай почту" -> {{"intent": "read_mail"}}
"письма от Иванова" -> {{"intent": "search_mail", "sender": "Иванов"}}
"как дела" -> {{"intent": "chat", "message": "как дела"}}
"что такое машинное обучение" -> {{"intent": "chat", "message": "что такое машинное обучение"}}
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
        if resp.status_code == 200:
            text = resp.json()["result"]["alternatives"][0]["message"]["text"]
            text = text.strip().replace("```json", "").replace("```", "").strip()
            return json.loads(text)
    except Exception as e:
        print(f"GPT ERROR: {e}")

    return fallback_intent(user_text)


async def chat_with_gpt(message: str, persona: str | None) -> str:
    """Свободный разговор с GPT с учётом портрета пользователя"""
    persona_block = f"Информация о пользователе:\n{persona}\n\n" if persona else ""

    system = (
        f"Ты — голосовой ассистент Алиса для корпоративного использования. "
        f"Отвечай кратко и по делу — ответ будет озвучен голосом. "
        f"Максимум 2-3 предложения.\n{persona_block}"
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
            return resp.json()["result"]["alternatives"][0]["message"]["text"]
    except Exception as e:
        print(f"CHAT GPT ERROR: {e}")

    return "Не смогла ответить на этот вопрос."


def fallback_intent(text: str) -> dict:
    t = text.lower()
    if any(w in t for w in ["найди", "поищи", "найти"]):
        query = t
        for w in ["найди", "поищи", "найти", "файл", "документ", "диск"]:
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
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://cloud-api.yandex.net/v1/disk/resources/last-uploaded",
            headers={"Authorization": f"OAuth {token}"},
            params={"limit": 7, "fields": "items.name,items.path,items.created"}
        )
    if resp.status_code != 200:
        return []
    return resp.json().get("items", [])


async def search_disk(token: str, query: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://cloud-api.yandex.net/v1/disk/resources/files",
            headers={"Authorization": f"OAuth {token}"},
            params={"limit": 20, "fields": "items.name,items.path,items.created"}
        )
    if resp.status_code != 200:
        return []
    items = resp.json().get("items", [])
    return [i for i in items if query.lower() in i.get("name", "").lower()]


async def get_public_link(token: str, path: str) -> str | None:
    headers = {"Authorization": f"OAuth {token}"}
    async with httpx.AsyncClient() as client:
        await client.put(
            "https://cloud-api.yandex.net/v1/disk/resources/publish",
            headers=headers, params={"path": path}
        )
        resp = await client.get(
            "https://cloud-api.yandex.net/v1/disk/resources",
            headers=headers, params={"path": path, "fields": "public_url"}
        )
    return resp.json().get("public_url")


def format_files(files: list[dict]) -> str:
    if not files:
        return "Файлы не найдены."
    lines = [f"{i}. {f.get('name','?').rsplit('.',1)[0]}" for i, f in enumerate(files[:5], 1)]
    return "Нашла: " + ", ".join(lines)


# ── Яндекс Почта ─────────────────────────────────────────────────────────────

async def get_recent_emails(token: str) -> list[dict]:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://mail.yandex.ru/api/v2/mailbox/folder/message/list",
                headers={"Authorization": f"OAuth {token}"},
                params={"fid": 1, "first": 0, "last": 5}
            )
        if resp.status_code == 200:
            return resp.json().get("envelopes", [])
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

@app.get("/")
async def root():
    return {"status": "ok"}


@app.post("/webhook")
async def alice_webhook(request: Request):
    body = await request.json()

    user_text: str = body.get("request", {}).get("original_utterance", "").strip()
    is_new_session: bool = body.get("session", {}).get("new", False)
    session_id: str = body.get("session", {}).get("session_id", "")
    user_token: str | None = body.get("session", {}).get("user", {}).get("access_token")

    # ── Нет токена ────────────────────────────────────────────────────────────
    if not user_token:
        return {
            "response": {"text": "Для работы нужно войти в аккаунт Яндекса.", "end_session": False},
            "start_account_linking": {},
            "version": "1.0"
        }

    # ── Читаем персону при старте сессии ─────────────────────────────────────
    if is_new_session:
        persona = await read_persona(user_token)
        sessions[session_id] = {"persona": persona, "files": []}

        if persona:
            # Извлекаем имя из персоны если есть
            greeting = "С возвращением! "
            for line in persona.split("\n"):
                if "имя" in line.lower() or "зовут" in line.lower():
                    greeting = f"С возвращением! Я тебя помню. "
                    break
        else:
            greeting = "Привет! Я твой ИИ-ассистент. Я буду запоминать твои предпочтения. "

        return _reply(
            greeting +
            "Говори свободно — понимаю любые фразы. "
            "Например: покажи файлы, прочитай почту, или просто задай вопрос."
        )

    # ── Получаем персону — из сессии или с диска если сессия потеряна ────────
    if session_id not in sessions:
        persona = await read_persona(user_token)
        sessions[session_id] = {"persona": persona, "files": []}
    else:
        persona = sessions.get(session_id, {}).get("persona")

    # ── GPT определяет намерение ──────────────────────────────────────────────
    intent_data = await understand_intent(user_text, persona)
    intent = intent_data.get("intent", "unknown")
    print(f"INTENT: {intent_data}")

    reply_text = ""

    # ── Обработка намерений ───────────────────────────────────────────────────

    if intent == "search_disk":
        query = intent_data.get("query", "").strip()
        if not query:
            reply_text = "Скажи название файла который ищешь."
        else:
            files = await search_disk(user_token, query)
            sessions[session_id]["files"] = files
            if not files:
                reply_text = f"Файлов с названием «{query}» не нашла."
            else:
                reply_text = format_files(files) + ". Назови номер чтобы получить ссылку."

    elif intent == "list_disk":
        files = await list_recent_files(user_token)
        sessions[session_id]["files"] = files
        if not files:
            reply_text = "На диске файлов не нашла."
        else:
            reply_text = format_files(files) + ". Назови номер чтобы получить ссылку."

    elif intent == "get_link":
        number = intent_data.get("number", 1)
        files = sessions.get(session_id, {}).get("files", [])
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
        reply_text = format_emails(emails) if emails else "Не удалось получить письма."

    elif intent == "search_mail":
        sender = intent_data.get("sender", "")
        emails = await get_recent_emails(user_token)
        filtered = [e for e in emails if sender.lower() in str(e.get("from", "")).lower()]
        reply_text = format_emails(filtered) if filtered else f"Писем от {sender} не нашла."

    elif intent == "chat":
        message = intent_data.get("message", user_text)
        reply_text = await chat_with_gpt(message, persona)

    elif intent == "help":
        reply_text = (
            "Говори свободно — я пойму. Например: "
            "покажи файлы на диске, найди документ отчёт, "
            "прочитай почту, или задай любой вопрос."
        )

    elif intent == "exit":
        # Обновляем персону перед выходом
        await update_persona(user_token, persona, user_text, "До встречи!")
        return _reply("До встречи! Запомнила наш разговор.", end=True)

    else:
        reply_text = "Не поняла. Попробуй: покажи файлы, прочитай почту, или задай вопрос."

    # ── Обновляем персону синхронно и сохраняем в сессию ────────────────────
    new_persona = await update_persona(user_token, persona, user_text, reply_text)
    if new_persona and session_id in sessions:
        sessions[session_id]["persona"] = new_persona

    return _reply(reply_text)


def _reply(text: str, end: bool = False) -> dict:
    return {"response": {"text": text, "end_session": end}, "version": "1.0"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
