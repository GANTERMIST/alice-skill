from fastapi import FastAPI, Request
import uvicorn
from pyngrok import ngrok

app = FastAPI()


@app.get("/")
async def root():
    """Просто проверка что сервер живой"""
    return {"status": "ok", "message": "Alice skill server is running"}


@app.post("/webhook")
async def alice_webhook(request: Request):
    body = await request.json()

    # ── Что сказал пользователь ──────────────────────────────────
    user_text: str = body.get("request", {}).get("original_utterance", "").lower().strip()
    is_new_session: bool = body.get("session", {}).get("new", False)

    # ── Логика ответов ───────────────────────────────────────────
    if is_new_session:
        response_text = (
            "Привет! Я корпоративный ассистент. "
            "Скажи например: покажи встречи, прочитай почту, или найди файл."
        )

    elif any(word in user_text for word in ["встреч", "календарь", "расписание"]):
        response_text = (
            "Функция календаря пока в разработке. "
            "Скоро смогу показывать твои встречи на день."
        )

    elif any(word in user_text for word in ["почт", "письм", "email"]):
        response_text = (
            "Функция почты пока в разработке. "
            "Скоро смогу зачитывать важные письма."
        )

    elif any(word in user_text for word in ["файл", "документ", "диск"]):
        response_text = (
            "Функция поиска по диску пока в разработке. "
            "Скоро смогу находить файлы по названию."
        )

    elif any(word in user_text for word in ["помощь", "помоги", "что умеешь", "команды"]):
        response_text = (
            "Я умею работать с календарём, почтой и диском. "
            "Попробуй сказать: покажи встречи на сегодня, "
            "прочитай последние письма, или найди файл отчёт."
        )

    elif any(word in user_text for word in ["пока", "выход", "закрой", "стоп"]):
        return {
            "response": {
                "text": "До встречи! Обращайся когда понадоблюсь.",
                "end_session": True,
            },
            "version": "1.0",
        }

    else:
        response_text = (
            f"Ты сказал: «{user_text}». "
            "Я пока не понял команду. Попробуй сказать: помощь."
        )

    # ── Ответ Алисе ──────────────────────────────────────────────
    return {
        "response": {
            "text": response_text,
            "end_session": False,
        },
        "version": "1.0",
    }


# ── Запуск напрямую через python main.py ─────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
    ngrok.connect(8000)
