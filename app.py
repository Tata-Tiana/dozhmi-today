#!/usr/bin/env python3
"""Local deal-priority demo. Ranking and comment parsing use code only."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(os.environ.get("DOZHMI_PORT", "8765"))
MAX_BODY_BYTES = 3_000_000
MAX_DEALS = 1_000
DEFAULT_PLAN_GAP = 1_550_000
MAX_DISCOUNT_WITHOUT_APPROVAL = 7
INVOICE_VALIDITY_BUSINESS_DAYS = 5
MAX_PRODUCTION_AND_DELIVERY_BUSINESS_DAYS = 19

SIGNAL_LABELS = {
    "paid": "оплата уже получена",
    "lost_to_competitor": "клиент выбрал конкурента",
    "duplicate": "сделка отмечена как дубль",
    "payment_promised": "клиент обещал оплатить",
    "invoice_requested": "клиент готов получить счёт",
    "payment_pending": "счёт выставлен, ждём оплату",
    "requested_revision": "клиент попросил изменить предложение",
    "contract_needed": "для сделки нужен договор",
    "unqualified_request": "запрос ещё не квалифицирован",
    "decision_maker_missing": "не установлен контакт с ЛПР",
    "price_objection": "есть возражение по цене",
    "deferred_payment_requested": "клиент просит отсрочку оплаты",
    "waiting_customer_info": "ждём данные от клиента",
    "customer_paused": "клиент отложил решение",
    "no_response": "клиент давно не отвечает",
    "needs_qualification": "потребность нужно уточнить",
}

SIGNAL_PRIORITY = {
    "payment_promised": 100,
    "invoice_requested": 92,
    "payment_pending": 82,
    "requested_revision": 78,
    "contract_needed": 74,
    "unqualified_request": 58,
    "needs_qualification": 54,
    "decision_maker_missing": 48,
    "price_objection": 44,
    "waiting_customer_info": 38,
    "no_response": 32,
    "customer_paused": 24,
    "deferred_payment_requested": 20,
}

ACTION_TEXT = {
    "payment_promised": "Связаться с клиентом и уточнить, прошёл ли платёж и когда он поступит. При необходимости повторно отправить счёт.",
    "invoice_requested": "Подготовить и отправить счёт. Уточнить ожидаемую дату оплаты.",
    "payment_pending": "Уточнить у клиента или бухгалтерии дату перевода. Проверить, не истёк ли срок счёта.",
    "requested_revision": "Пересчитать предложение с учётом запрошенного изменения, отправить клиенту и согласовать следующий контакт.",
    "contract_needed": "Подготовить или отправить проект договора и уточнить срок подписания и оплаты. Проверить, хватит ли времени на производство и доставку.",
    "unqualified_request": "Связаться с клиентом, уточнить состав потребности, сроки закупки, критерии сравнения и контакт принимающего решение.",
    "needs_qualification": "Уточнить потребность и следующий шаг у клиента, затем зафиксировать договорённость в CRM.",
    "decision_maker_missing": "Уточнить контакт принимающего решение и договориться о разговоре с ним.",
    "price_objection": "Выяснить, при каких условиях клиент готов продолжить. Скидку выше 7% согласовать с руководителем.",
    "waiting_customer_info": "Напомнить клиенту, какие данные нужны, и согласовать срок их получения.",
    "no_response": "Проверить актуальность интереса и связаться ещё раз по доступному каналу.",
    "customer_paused": "Уточнить подходящую дату возврата к разговору и поставить задачу на эту дату.",
    "deferred_payment_requested": "Объяснить условия предоплаты. Не обещать отсрочку без проверки правил и согласования.",
}

RULE_PATTERNS = {
    "paid": re.compile(r"(?:оплат\w*\s+(?:приш\w*|поступ\w*|получ\w*)|получ\w*\s+оплат\w*|оплата\s+получена|оплат\w*\s+100\s*%)", re.I),
    "lost_to_competitor": re.compile(r"(?:купил\w*|выбра\w*|уш\w*)[^.\n]{0,70}(?:конкурент\w*|друг\w*\s+поставщик\w*)|(?:конкурент\w*|друг\w*\s+поставщик\w*)[^.\n]{0,70}(?:купил\w*|выбра\w*|дешевле)", re.I),
    "duplicate": re.compile(r"(?:дубл\w*|повторн\w*\s+сделк\w*|заведена\s+повторно)", re.I),
    "payment_promised": re.compile(r"(?:обещал\w*[^.\n]{0,80}оплат\w*|оплат\w*[^.\n]{0,60}(?:на\s+этой\s+неделе|на\s+неделе|сегодня|завтра)|перевед\w*[^.\n]{0,50}деньг\w*|главбух[^.\n]{0,100}оплат\w*)", re.I),
    "invoice_requested": re.compile(r"(?:выстав\w*[^.\n]{0,60}сч[её]т|сч[её]т[^.\n]{0,60}(?:выстав\w*|нужен|не\s+отправлен|на\s+оплат\w*)|кп\s+устраивает[^.\n]{0,80}сч[её]т|(?:вышл\w*|направ\w*|пришл\w*)[^.\n]{0,50}сч[её]т)", re.I),
    "payment_pending": re.compile(r"(?:жд[её]м\s+оплат\w*|ожида\w*\s+оплат\w*|сч[её]т\s+отправлен[^.\n]{0,80}оплат\w*|плат[её]ж[^.\n]{0,60}(?:не\s+поступил|ожида\w*)|оплата[^.\n]{0,60}(?:не\s+прошла|задерж\w*)|бухгалтер[^.\n]{0,80}(?:отпуск|вернул\w*))", re.I),
    "requested_revision": re.compile(r"(?:(?:попросил\w*|нужн\w*|прос\w*)[^.\n]{0,80}(?:измен\w*|поменя\w*|пересчита\w*|скорректир\w*)|надо\s+пересчита\w*|не\s+ответил\w*[^.\n]{0,80}(?:пересчита\w*|цвет))", re.I),
    "contract_needed": re.compile(r"(?:договор[^.\n]{0,100}(?:не\s+отправлен|жд[её]т|подготов\w*|нужен|на\s+согласован\w*)|жд[её]т[^.\n]{0,60}договор|проект\s+договор\w*)", re.I),
    "unqualified_request": re.compile(r"(?:потребност\w*\s+не\s+выяснял\w*|(?:пришлите|нужн\w*|прос\w*)\s+(?:кп|коммерческ\w*\s+предложен\w*)[^.\n]{0,100}сравн\w*|первый\s+звонок)", re.I),
    "decision_maker_missing": re.compile(r"(?:секретар\w*[^.\n]{0,100}(?:передаст|руковод|директор)|контакт\w*[^.\n]{0,60}(?:директор|руководител)\w*\s+нет|лпр\s+нет)", re.I),
    "price_objection": re.compile(r"(?:дорог\w*|дешевле[^.\n]{0,40}конкурент|скидк\w*[^.\n]{0,50}(?:прос|просят|нужн))", re.I),
    "deferred_payment_requested": re.compile(r"(?:отсрочк\w*[^.\n]{0,50}(?:оплат|дн)|оплат\w*[^.\n]{0,50}отсрочк\w*|\b\d+\s*дн\w*\s+отсрочк\w*)", re.I),
    "waiting_customer_info": re.compile(r"(?:(?:жд[её]м|жд[её]т|ожида\w*)[^.\n]{0,60}(?:размер\w*|фото|данн\w*|документ\w*)|клиент[^.\n]{0,60}не\s+прислал\w*[^.\n]{0,30}(?:фото|размер\w*|данн\w*))", re.I),
    "customer_paused": re.compile(r"(?:пока\s+не\s+до\s+этого|верн[её]мся\s+позже|бюджет\s+на\s+\d{4}\s+год)", re.I),
    "no_response": re.compile(r"(?:не\s+отвеча\w*|не\s+дозвонил\w*|прочитан\w*\s+без\s+ответ\w*|клиент\s+пропал)", re.I),
    "needs_qualification": re.compile(r"(?:выяснить\s+потребност\w*|уточнить\s+потребност\w*|размер\w*\s+площадк\w*\s+не\s+известн\w*)", re.I),
}


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split())


def parse_amount(value: Any) -> float:
    raw = clean_text(value).replace("₽", "").replace("руб.", "").replace("руб", "")
    raw = raw.replace(" ", "").replace("\u202f", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", raw)
    return float(match.group(0)) if match else 0.0


def parse_date(value: Any) -> dt.date | None:
    raw = clean_text(value)
    if not raw:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    try:
        return dt.date.fromisoformat(raw[:10])
    except ValueError:
        return None


def parse_note_deadline(note: str, today: dt.date) -> dt.date | None:
    date_tokens = re.findall(r"\b(?:до|к)\s*(\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)", note, flags=re.I)
    candidates: list[dt.date] = []
    for token in date_tokens:
        parts = re.split(r"[./]", token)
        day, month = int(parts[0]), int(parts[1])
        year = int(parts[2]) if len(parts) == 3 else today.year
        if year < 100:
            year += 2000
        try:
            deadline = dt.date(year, month, day)
        except ValueError:
            continue
        if deadline >= today:
            candidates.append(deadline)
    return min(candidates) if candidates else None


def business_days_between(start: dt.date, end: dt.date) -> int:
    current = start
    count = 0
    while current < end:
        current += dt.timedelta(days=1)
        if current.weekday() < 5:
            count += 1
    return count


def is_government_customer(customer_type: str) -> bool:
    return "гос" in clean_text(customer_type).lower()


def fallback_signals(deal: dict[str, Any]) -> set[str]:
    note = clean_text(deal.get("comment", ""))
    stage = clean_text(deal.get("stage", "")).lower()
    found = {name for name, pattern in RULE_PATTERNS.items() if pattern.search(note)}
    if any(word in stage for word in ("оплачен", "успешно закрыта", "отгружена")):
        found.add("paid")
    if any(word in stage for word in ("проиграна", "отказ", "закрыта неуспешно")):
        found.add("lost_to_competitor")
    return found


def normalize_deals(raw_deals: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_deals, list) or len(raw_deals) > MAX_DEALS:
        raise ValueError(f"Передайте список не более чем из {MAX_DEALS} сделок.")
    deals: list[dict[str, Any]] = []
    for index, row in enumerate(raw_deals, start=1):
        if not isinstance(row, dict):
            continue
        deal_id = clean_text(row.get("id")) or str(index)
        client = clean_text(row.get("client")) or "Клиент не указан"
        deals.append(
            {
                "id": deal_id,
                "client": client,
                "customer_type": clean_text(row.get("customer_type")),
                "product": clean_text(row.get("product")),
                "amount": parse_amount(row.get("amount")),
                "stage": clean_text(row.get("stage")),
                "created_at": parse_date(row.get("created_at")),
                "last_contact": parse_date(row.get("last_contact")),
                "next_step_date": parse_date(row.get("next_step_date")),
                "comment": clean_text(row.get("comment")),
            }
        )
    return deals


def is_duplicate_candidate(deal: dict[str, Any], deals: list[dict[str, Any]]) -> bool:
    if "duplicate" in fallback_signals(deal):
        return True
    key = (deal["client"].casefold(), deal["product"].casefold(), round(deal["amount"]))
    same = [
        other
        for other in deals
        if other["id"] != deal["id"]
        and (other["client"].casefold(), other["product"].casefold(), round(other["amount"])) == key
    ]
    return bool(same and any("duplicate" in fallback_signals(other) for other in same))


def action_for(deal: dict[str, Any], signals: set[str], today: dt.date) -> tuple[str, str]:
    if "paid" in signals:
        return "Сделка уже оплачена. Не включать в список дожима.", "Оплата получена"
    if "lost_to_competitor" in signals:
        return "Клиент выбрал конкурента. Не включать в список дожима.", "Сделка проиграна"
    if "duplicate" in signals or is_duplicate_candidate(deal, []):
        return "Проверить связь с основной сделкой. Не дублировать контакт.", "В комментарии отмечен дубль"

    precedence = (
        "payment_promised",
        "invoice_requested",
        "payment_pending",
        "requested_revision",
        "contract_needed",
        "deferred_payment_requested",
        "unqualified_request",
        "needs_qualification",
        "decision_maker_missing",
        "price_objection",
        "waiting_customer_info",
        "no_response",
        "customer_paused",
    )
    primary = next((signal for signal in precedence if signal in signals), None)
    if primary is None:
        return "Не уверен, что делать: в данных нет однозначного следующего шага.", "Следующий шаг неясен"

    action = ACTION_TEXT[primary]
    if primary == "deferred_payment_requested" and not is_government_customer(deal["customer_type"]):
        action = "Объяснить условие 100% предоплаты и спросить, готов ли клиент продолжить. Отсрочку для этой сделки не обещать."
    if primary == "payment_pending" and "сч" in deal["stage"].lower():
        action = f"Уточнить у клиента дату перевода и дату выставления счёта. Если прошло более {INVOICE_VALIDITY_BUSINESS_DAYS} рабочих дней и оплаты нет, проверить необходимость перевыставить счёт."
    next_step = deal["next_step_date"]
    if next_step and next_step < today and primary not in ("payment_promised", "payment_pending", "invoice_requested"):
        action = f"Не уверен, что делать: следующий шаг просрочен с {next_step:%d.%m.%Y}. Сначала проверить его актуальность."
    return action, SIGNAL_LABELS[primary]


def score_deal(deal: dict[str, Any], signals: set[str], today: dt.date, plan_gap: float) -> tuple[int, list[str], str, str]:
    if "paid" in signals or "lost_to_competitor" in signals or "duplicate" in signals or is_duplicate_candidate(deal, []):
        reason = "; ".join(SIGNAL_LABELS[s] for s in ("paid", "lost_to_competitor", "duplicate") if s in signals)
        return -100, [reason or "закрытая сделка"], *action_for(deal, signals, today)

    score = max((SIGNAL_PRIORITY.get(signal, 0) for signal in signals), default=25)
    reasons = [SIGNAL_LABELS[signal] for signal in sorted(signals) if signal in SIGNAL_LABELS]
    if not signals:
        reasons.append("комментарий не распознан, вывод предварительный")

    if plan_gap > 0 and deal["amount"] > 0:
        amount_points = min(12, round(deal["amount"] / plan_gap * 12))
        score += amount_points
        reasons.append(f"сумма даёт до {amount_points} баллов вклада в план")

    deadline = parse_note_deadline(deal["comment"], today)
    if deadline:
        days_left = business_days_between(today, deadline)
        if days_left <= MAX_PRODUCTION_AND_DELIVERY_BUSINESS_DAYS:
            score += 12
            reasons.append(f"срок {deadline:%d.%m.%Y} близок к максимальным {MAX_PRODUCTION_AND_DELIVERY_BUSINESS_DAYS} рабочим дням на производство и доставку")
        elif days_left <= MAX_PRODUCTION_AND_DELIVERY_BUSINESS_DAYS + 5:
            score += 8
            reasons.append(f"есть срок {deadline:%d.%m.%Y}")
        elif days_left <= MAX_PRODUCTION_AND_DELIVERY_BUSINESS_DAYS + 10:
            score += 5
            reasons.append(f"есть срок {deadline:%d.%m.%Y}")

    next_step = deal["next_step_date"]
    if next_step:
        delta = (next_step - today).days
        seller_action = bool(signals & {"invoice_requested", "requested_revision", "contract_needed"})
        if delta == 0:
            score += 10
            reasons.append("следующий шаг назначен на сегодня")
        elif -7 <= delta < 0:
            score += 6
            reasons.append("следующий шаг просрочен менее чем на неделю")
        elif delta < -30:
            score -= 12
            reasons.append("следующий шаг просрочен больше месяца")
        elif delta < 0:
            score -= 5
            reasons.append("следующий шаг просрочен")
        elif delta > 0 and not seller_action:
            score -= min(15, 5 + delta // 3)
            reasons.append(f"следующий шаг в будущем: {next_step:%d.%m.%Y}")
        elif delta > 0:
            reasons.append(f"в CRM указан будущий контроль {next_step:%d.%m.%Y}")

    last_contact = deal["last_contact"]
    if last_contact:
        days_since_contact = (today - last_contact).days
        if 0 <= days_since_contact <= 3:
            score += 5
            reasons.append("контакт был недавно")
        elif 4 <= days_since_contact <= 7:
            score += 3
            reasons.append("контакт был на этой неделе")
        elif days_since_contact > 60:
            score -= 8
            reasons.append("давно не было контакта")

    if "deferred_payment_requested" in signals and not is_government_customer(deal["customer_type"]):
        score -= 25
        reasons.append("отсрочка не соответствует правилам для этого типа клиента")
    if "price_objection" in signals:
        discount_match = re.search(r"скидк\w*[^.\n]{0,40}?(\d{1,2})\s*%", deal["comment"], flags=re.I)
        if discount_match and int(discount_match.group(1)) > MAX_DISCOUNT_WITHOUT_APPROVAL:
            score -= 8
            reasons.append("скидка выше лимита менеджера и требует согласования")

    action, primary_reason = action_for(deal, signals, today)
    return max(0, min(100, score)), reasons, action, primary_reason


def analyze(payload: dict[str, Any]) -> dict[str, Any]:
    today = parse_date(payload.get("today")) or dt.date.today()
    plan_gap = parse_amount(payload.get("plan_gap", DEFAULT_PLAN_GAP))
    deals = normalize_deals(payload.get("deals"))
    if not deals:
        raise ValueError("В файле не нашлось строк со сделками.")

    ranked: list[dict[str, Any]] = []
    for deal in deals:
        signals = fallback_signals(deal)
        score, reasons, action, primary_reason = score_deal(deal, signals, today, plan_gap)
        ranked.append(
            {
                "id": deal["id"],
                "client": deal["client"],
                "customer_type": deal["customer_type"],
                "product": deal["product"],
                "amount": round(deal["amount"]),
                "stage": deal["stage"],
                "next_step_date": deal["next_step_date"].isoformat() if deal["next_step_date"] else None,
                "score": score,
                "signals": sorted(signals),
                "signal_labels": [SIGNAL_LABELS[signal] for signal in sorted(signals) if signal in SIGNAL_LABELS],
                "reasons": reasons,
                "action": action,
                "primary_reason": primary_reason,
                "excluded": score < 0,
            }
        )

    eligible = [row for row in ranked if not row["excluded"]]
    selected = sorted(eligible, key=lambda row: (row["score"], row["amount"]), reverse=True)[:5]
    for row in selected:
        row["reasons"] = row["reasons"][:4]
    selected_ids = {row["id"] for row in selected}
    selected_ranks = {row["id"]: index + 1 for index, row in enumerate(selected)}
    for row in ranked:
        row["selected_rank"] = selected_ranks.get(row["id"])
    excluded = [
        {"id": row["id"], "client": row["client"], "reason": "; ".join(row["reasons"][:2]) or row["primary_reason"]}
        for row in ranked
        if row["excluded"] or row["id"] not in selected_ids
    ]
    return {
        "today": today.isoformat(),
        "plan_gap": round(plan_gap),
        "mode": "Правила в коде",
        "warning": "",
        "selected": selected,
        "all_deals": ranked,
        "excluded_or_lower_priority": excluded,
        "final_line": "Итог: " + ", ".join(row["id"] for row in selected),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "DozhmiLocal/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # Do not print request bodies or deal information to the terminal.
        if self.path.startswith("/api/"):
            print(f"{self.log_date_time_string()} {self.command} {self.path.split('?')[0]}")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict[str, Any]) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            try:
                content = (ROOT / "index.html").read_bytes()
            except OSError:
                self._send(500, b"index.html is missing", "text/plain; charset=utf-8")
                return
            self._send(200, content, "text/html; charset=utf-8")
            return
        self._json(404, {"error": "Страница не найдена."})

    def do_POST(self) -> None:
        if self.path != "/api/analyze":
            self._json(404, {"error": "Страница не найдена."})
            return
        origin = self.headers.get("Origin")
        allowed_origins = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
        if origin and origin not in allowed_origins:
            self._json(403, {"error": "Запрос должен быть отправлен с локальной страницы приложения."})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Ожидается JSON."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("Файл пустой или слишком большой.")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = analyze(payload)
            self._json(200, result)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})
        except Exception as error:  # Keep the server alive and show a useful local error.
            self._json(500, {"error": f"Не удалось обработать файл ({type(error).__name__}). Проверьте формат CSV и дату."})


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Дожми сегодня запущено на {url}")
    print("Работает без нейросетей и внешних API. Обработка выполняется на этом компьютере.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
