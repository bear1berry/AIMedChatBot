"""Text rendering helpers used by the bot handlers.

The original project relied on a more sophisticated templating module that
is not present in this repository snapshot.  This lightweight implementation
keeps the handlers operational by returning human-readable strings that cover
all expected screens and error messages.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Any

from bot.config import ASSISTANT_MODES


def _mode_title(mode_cfg: Dict[str, Any]) -> str:
    emoji = mode_cfg.get("emoji") or "🧠"
    title = mode_cfg.get("title") or "Универсальный"
    return f"{emoji} {title}"


def _fmt_datetime(dt_iso: str | None) -> str:
    if not dt_iso:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_iso)
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return dt_iso


def render_onboarding(
    first_name: str | None,
    is_new: bool,
    mode_title: str,
    limits: Dict[str, Any],
    ref_stats: Dict[str, Any],
) -> str:
    greeting = "👋 Привет" if is_new else "👋 Рад снова тебя видеть"
    username = f", {first_name}" if first_name else ""
    ref_part = ""
    if ref_stats.get("ref_code"):
        ref_part = f"\nТвоя реферальная ссылка уже готова: {ref_stats['ref_code']}"
    limit_part = "∞" if limits.get("daily_limit") is None else limits.get("daily_limit", 0)
    return (
        f"{greeting}{username}!"\
        f"\nСейчас активен режим: {mode_title}."\
        f"\nДневной лимит: {limit_part} запросов."\
        "\n\nВоспользуйся меню ниже, чтобы выбрать режим, посмотреть профиль или подписку."\
        f"{ref_part}"
    )


def render_help() -> str:
    return (
        "ℹ️ <b>Справка</b>\n\n"
        "• Используй меню для переключения режима и просмотра лимитов.\n"
        "• Premium убирает дневные ограничения.\n"
        "• Реферальная система добавляет бонусные сообщения."
    )


def render_profile(
    user_id: int,
    tg_user: Any,
    mode_cfg: Dict[str, Any],
    limits: Dict[str, Any],
    plan: Dict[str, Any],
    ref_stats: Dict[str, Any],
    referral_link: str,
) -> str:
    premium_until = _fmt_datetime(plan.get("premium_until")) if plan.get("premium_until") else "—"
    limit_caption = "безлимит" if limits.get("daily_limit") is None else f"{limits.get('used_today', 0)} / {limits.get('daily_limit', 0)}"
    return (
        "👤 <b>Профиль</b>\n\n"
        f"ID: <code>{user_id}</code>\n"
        f"Имя: {tg_user.full_name if tg_user else '—'}\n"
        f"Режим: {_mode_title(mode_cfg)}\n"
        f"Тариф: {plan.get('code', 'basic')}\n"
        f"Premium активен до: {premium_until}\n"
        f"Лимит на сегодня: {limit_caption}\n"
        f"Всего запросов: {limits.get('total_used', 0)}\n\n"
        f"Рефералов: {ref_stats.get('ref_count', 0)} (бонус: {ref_stats.get('ref_bonus_messages', 0)})\n"
        f"Реферальная ссылка: {referral_link}"
    )


def render_limits(mode_cfg: Dict[str, Any], limits: Dict[str, Any], plan: Dict[str, Any]) -> str:
    limit_caption = "безлимит" if limits.get("daily_limit") is None else limits.get("daily_limit", 0)
    remaining = "∞" if limits.get("remaining_daily") is None else limits.get("remaining_daily", 0)
    return (
        "📊 <b>Лимиты</b>\n\n"
        f"Режим: {_mode_title(mode_cfg)}\n"
        f"Тариф: {plan.get('code', 'basic')}\n"
        f"Дневной лимит: {limit_caption}\n"
        f"Осталось сегодня: {remaining}\n"
        f"Всего использовано: {limits.get('total_used', 0)}"
    )


def render_modes_root() -> str:
    lines = [f"• {_mode_title(cfg)} — {cfg.get('description', '')}" for cfg in ASSISTANT_MODES.values()]
    return "🧠 <b>Режимы</b>\n\n" + "\n".join(lines)


def render_mode_changed(mode_cfg: Dict[str, Any]) -> str:
    return f"Режим переключён на {_mode_title(mode_cfg)}"


def render_back_to_main() -> str:
    return "Возврат в главное меню. Чем могу помочь?"


def render_subscription_root(limits: Dict[str, Any], plan: Dict[str, Any], tariffs: Dict[str, Any]) -> str:
    premium_until = _fmt_datetime(plan.get("premium_until")) if plan.get("premium_until") else "—"
    tariffs_lines = [f"• {tar['title']}: {tar['price_usdt']} {tar['asset']}" for tar in tariffs.values()]
    limits_info = "безлимит" if limits.get("daily_limit") is None else limits.get("daily_limit", 0)
    return (
        "💎 <b>Подписка</b>\n\n"
        f"Текущий тариф: {plan.get('code', 'basic')}\n"
        f"Premium активен до: {premium_until}\n"
        f"Дневной лимит: {limits_info}\n\n"
        "Доступные тарифы:\n" + "\n".join(tariffs_lines)
    )


def render_subscription_not_available() -> str:
    return "Оплата временно недоступна. Попробуйте позже."


def render_payment_error() -> str:
    return "Не удалось создать счёт. Повторите попытку позже."


def render_subscription_invoice(tariff: Dict[str, Any], invoice: Dict[str, Any]) -> str:
    return (
        "Инвойс создан!\n\n"
        f"Тариф: {tariff.get('title')}\n"
        f"Сумма: {tariff.get('price_usdt')} {tariff.get('asset')}\n"
        f"Ссылка на оплату: {invoice.get('url')}"
    )


def render_referrals(stats: Dict[str, Any], referral_link: str) -> str:
    return (
        "👥 <b>Рефералы</b>\n\n"
        f"Всего рефералов: {stats.get('ref_count', 0)}\n"
        f"Бонусные сообщения: {stats.get('ref_bonus_messages', 0)}\n"
        f"Твоя ссылка: {referral_link}"
    )


def render_empty_prompt_error() -> str:
    return "Сообщение пустое. Напишите вопрос или задачу."


def render_too_long_error(max_tokens: int) -> str:
    return f"Сообщение слишком длинное. Максимальная длина — {max_tokens} символов."


def render_daily_limit_reached(limits: Dict[str, Any]) -> str:
    return (
        "⛔ Лимит исчерпан."
        f" Вы использовали {limits.get('used_today', 0)} из {limits.get('daily_limit', 0)} сообщений на сегодня."
    )


def render_thinking_message() -> str:
    return "Думаю над ответом…"


def render_generic_error() -> str:
    return "Произошла ошибка при обращении к модели. Попробуйте ещё раз позже."


def normalize_model_answer(answer: str) -> str:
    # Простейшая нормализация: убираем лишние пробелы по краям
    return answer.strip()
