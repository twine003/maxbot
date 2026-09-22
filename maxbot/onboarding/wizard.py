"""In-Telegram setup wizard — a conversation-driven state machine.

Registered as a high-priority handler (group -50). While setup is incomplete it
intercepts every message, drives the current stage, and stops propagation so the
normal handlers never see the message. Once `stage == done` it becomes a no-op
and normal operation resumes.

Flow: pairing → agent_select → agent_install → agent_auth → alma → done
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters
from telegram.ext import ApplicationHandlerStop

from . import installer

if TYPE_CHECKING:
    from ..context import BotContext

log = logging.getLogger(__name__)

# Each tuple: (answer_key, question text shown to the user).
ALMA_QUESTIONS: list[tuple[str, str]] = [
    ("name", "1/6 · ¿Cómo se va a llamar tu asistente? (ej: Max)"),
    ("role", "2/6 · ¿Para qué lo vas a usar principalmente? (ej: asistente personal, devops, finanzas...)"),
    ("language", "3/6 · ¿En qué idioma debe responder por defecto? (ej: español)"),
    ("tone", "4/6 · ¿Qué tono/personalidad querés? (ej: directo y conciso; cálido; técnico)"),
    ("rules", "5/6 · ¿Alguna regla o límite importante? (ej: confirmar antes de borrar archivos). Escribí 'ninguna' si no aplica."),
    ("owner", "6/6 · Contame algo sobre vos que el asistente deba saber siempre (nombre, rol, contexto). Escribí 'nada' si no."),
]

_AFFIRMATIVE = {"listo", "ok", "okay", "ya", "ya esta", "ya está", "done", "hecho", "si", "sí"}

# Ventana de vida de una invitación (/invitar). Corta a propósito: el código
# viaja por fuera (WhatsApp, voz) y autoriza un chat nuevo.
INVITE_TTL_MINUTES = 60


def register(ctx: "BotContext") -> None:
    app = ctx.connector.app
    setup = ctx.setup

    async def wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if setup is None:
            return

        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None:
            return
        chat_id = chat.id
        text = (message.text or "").strip()
        user = update.effective_user
        user_id = user.id if user is not None else None

        async def reply(t: str):
            await context.bot.send_message(chat_id=chat_id, text=t)

        if setup.is_done:
            handled = await _pair_additional_chat(ctx, chat_id, user_id, text, reply)
            if handled:
                raise ApplicationHandlerStop
            return  # normal operation → let lower-priority handlers run

        stage = setup.stage
        try:
            if stage == "pairing":
                await _pairing(ctx, chat_id, user_id, text, reply)
            elif stage == "agent_select":
                await _agent_select(ctx, chat_id, text, reply)
            elif stage == "agent_install":
                # An install is already running / was interrupted — re-trigger.
                await _agent_install(ctx, setup.agent, reply)
            elif stage == "agent_auth":
                await _agent_auth(ctx, chat_id, text, reply)
            elif stage == "alma":
                await _alma_answer(ctx, chat_id, text, reply)
        except Exception as e:  # never let the wizard crash the bot
            log.exception("wizard error at stage %s: %s", stage, e)
            await reply(f"Ups, error en el asistente de configuración: {e}")

        raise ApplicationHandlerStop

    async def cmd_invitar(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Genera un código de un solo uso para dar acceso a OTRA persona.

        El código de pairing permanente solo lo acepta el dueño, así que no
        sirve para esto: la otra persona lo enviaría desde su propio chat y
        quedaría rechazada en silencio.
        """
        if setup is None or not setup.is_done:
            return
        if not ctx.is_allowed(update):
            return
        user = update.effective_user
        owner = setup.owner_user_id
        if owner is not None and owner > 0 and (user is None or user.id != owner):
            await update.message.reply_text("Solo el dueño del bot puede generar invitaciones.")
            return

        inv = setup.new_invite(ttl_minutes=INVITE_TTL_MINUTES, created_by=user.id if user else None)
        try:
            username = (await context.bot.get_me()).username
        except Exception:
            username = None
        enlace = f"https://t.me/{username}" if username else "el chat del bot"
        mencion = f"@{username} " if username else ""
        await update.message.reply_text(
            f"🎟️ Invitación de un solo uso (vence en {INVITE_TTL_MINUTES} minutos):\n\n"
            f"    {inv['code']}\n\n"
            "Para dar acceso a un chat privado: pasale el código a la persona y que "
            f"me escriba a {enlace} enviando solo ese código.\n\n"
            "Para dar acceso a un grupo: agregame al grupo y escribí ahí "
            f"«{mencion}{inv['code']}».\n\n"
            "Se quema al primer uso. Si se vence, pedí otro con /invitar."
        )
        log.info("Invitación generada por el dueño (vence %s)", inv.get("expires_at"))

    # group -50 → runs before all normal handlers; self-disables when done.
    app.add_handler(MessageHandler(filters.ALL, wizard), group=-50)
    app.add_handler(CommandHandler("invitar", cmd_invitar))
    log.info("Onboarding wizard registered (current stage: %s)", setup.stage if setup else "n/a")


# --------------------------------------------------------------------------- #
# Stage: pairing
# --------------------------------------------------------------------------- #
async def _pairing(ctx, chat_id, user_id, text, reply):
    setup = ctx.setup
    code = setup.pairing_code
    if _message_has_pairing_code(text, code):
        setup.paired_chat_id = chat_id
        if user_id is not None:
            setup.owner_user_id = user_id
        ctx.config.allowed_chat_ids.add(int(chat_id))
        log.info("Bot paired to chat %s (owner user %s)", chat_id, user_id)
        await reply(
            "✅ ¡Emparejado! Este chat ahora es el dueño del bot.\n\n"
            "Vamos a configurar tu agente de IA."
        )
        await _enter_agent_select(ctx, reply)
        return
    await reply(
        "👋 Para activar el bot, enviá el *código de emparejamiento* que aparece "
        "en la consola del servidor donde corre maxbot.\n\n"
        "Se ve así: `ABC-123`."
    )


async def _pair_additional_chat(ctx, chat_id, user_id, text, reply) -> bool:
    """Authorize a second chat (typically a private group) after onboarding.

    The pairing code stays valid forever, so it is only honoured when sent by
    the owner (the user who paired the bot). Anyone else sending it is ignored
    silently — no reply, so the message does not confirm the code is valid.
    """
    setup = ctx.setup
    if await _redeem_invite(ctx, chat_id, user_id, text, reply):
        return True
    code = setup.pairing_code
    if not _message_has_pairing_code(text, code):
        return False
    if setup.paired_chat_id == chat_id or chat_id in setup.allowed_chat_ids:
        await reply("Este chat ya estaba autorizado para hablar con el bot.")
        return True
    owner = setup.owner_user_id
    # owner > 0 means we know the owner's user id (a private chat id equals the
    # user id). If pairing happened from a group we cannot tell who the owner
    # is, so we keep the legacy behaviour and accept the code from anyone.
    if owner is not None and owner > 0 and user_id != owner:
        log.warning(
            "Pairing code sent by non-owner user %s in chat %s — ignored", user_id, chat_id
        )
        return True
    setup.add_allowed_chat_id(chat_id)
    ctx.config.allowed_chat_ids.add(int(chat_id))
    log.info("Additional chat authorized: %s", chat_id)
    await reply(
        "✅ Grupo autorizado. Desde ahora pueden hablar conmigo aquí.\n\n"
        "Para invocarme en grupo, usen mi @usuario, respondan a un mensaje mío o escriban comandos como /start."
    )
    return True


async def _redeem_invite(ctx, chat_id, user_id, text, reply) -> bool:
    """Canjea una invitación de un solo uso creada por el dueño con /invitar.

    Al revés que el código de pairing permanente, esta SÍ la puede enviar otra
    persona desde su propio chat — para eso existe. Vale una vez y vence.
    """
    setup = ctx.setup
    invite = setup.invite
    if not invite:
        return False
    code = invite.get("code") or ""
    if not code or not _message_has_pairing_code(text, code):
        return False

    if _invite_expired(invite.get("expires_at")):
        setup.clear_invite()
        log.info("Invitación vencida usada en chat %s", chat_id)
        await reply(
            "Ese código de invitación ya venció. Pedile al dueño del bot que genere otro."
        )
        return True

    if setup.paired_chat_id == chat_id or chat_id in setup.allowed_chat_ids:
        setup.clear_invite()
        await reply("Este chat ya estaba autorizado para hablar conmigo.")
        return True

    setup.add_allowed_chat_id(chat_id)
    ctx.config.allowed_chat_ids.add(int(chat_id))
    setup.clear_invite()
    log.info("Chat %s autorizado por invitación (user %s)", chat_id, user_id)
    await reply(
        "✅ ¡Listo! Ya podés hablar conmigo desde este chat. Escribime lo que necesites."
    )
    await _notify_owner(
        ctx,
        f"🔓 Se usó la invitación que generaste: el chat `{chat_id}` quedó autorizado.\n"
        "Si no fuiste vos quien la compartió, sacalo de `allowed_chat_ids` en "
        "`setup_state.json` y reiniciá el bot.",
    )
    return True


def _invite_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return False  # sin fecha legible, no la damos por vencida
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= exp


async def _notify_owner(ctx, text: str) -> None:
    """Avisa al chat del dueño. Nunca debe romper el flujo que la llamó."""
    target = ctx.setup.paired_chat_id if ctx.setup else None
    if not target:
        return
    try:
        await ctx.connector.bot.send_message(chat_id=target, text=text)
    except Exception as e:
        log.warning("no se pudo avisar al dueño (%s): %s", target, e)


# --------------------------------------------------------------------------- #
# Stage: agent_select
# --------------------------------------------------------------------------- #
async def _enter_agent_select(ctx, reply):
    ctx.setup.stage = "agent_select"
    await reply(
        "🤖 ¿Qué agente de IA querés usar?\n\n"
        "1️⃣  Claude Code  (Anthropic)\n"
        "2️⃣  Codex  (OpenAI)\n\n"
        "Respondé *1* o *2* (o escribí 'claude' / 'codex')."
    )


async def _agent_select(ctx, chat_id, text, reply):
    choice = text.lower()
    if choice in ("1", "claude", "claude code", "anthropic"):
        agent = "claude"
    elif choice in ("2", "codex", "openai"):
        agent = "codex"
    else:
        await reply("No entendí. Respondé *1* (Claude Code) o *2* (Codex).")
        return

    ctx.setup.agent = agent
    spec = installer.AGENTS[agent]
    # Point the paired chat's active model at the chosen agent.
    try:
        ctx.sessions.set_active_model(chat_id, agent)
    except Exception:
        pass

    if installer.find_cli(spec["cli"]):
        await reply(f"✅ {spec['label']} ya está instalado. Vamos a autenticarlo.")
        await _enter_agent_auth(ctx, reply)
    else:
        await reply(f"Voy a instalar {spec['label']}. Esto puede tardar varios minutos…")
        await _agent_install(ctx, agent, reply)


# --------------------------------------------------------------------------- #
# Stage: agent_install
# --------------------------------------------------------------------------- #
async def _agent_install(ctx, agent, reply):
    ctx.setup.stage = "agent_install"
    spec = installer.AGENTS[agent]

    if not installer.npm_path():
        await reply(
            "⚠️ No encontré *npm* (Node.js) en el servidor, que hace falta para "
            f"instalar {spec['label']}.\n\n"
            "Instalá Node.js LTS desde https://nodejs.org y, cuando termines, "
            "escribime *listo* para reintentar."
        )
        return

    ok, tail = await installer.install_agent(agent)
    if not ok:
        await reply(
            f"❌ La instalación de {spec['label']} falló.\n\n"
            f"Detalle:\n{tail[:600]}\n\n"
            "Podés instalarlo a mano con:\n"
            f"`npm install -g {spec['npm_package']}`\n"
            "y luego escribirme *listo*."
        )
        return

    # Patch the live runner so it uses the freshly installed binary.
    _refresh_runner_executable(ctx, agent)
    await reply(f"✅ {spec['label']} instalado. Ahora autenticación.")
    await _enter_agent_auth(ctx, reply)


# --------------------------------------------------------------------------- #
# Stage: agent_auth
# --------------------------------------------------------------------------- #
async def _enter_agent_auth(ctx, reply):
    ctx.setup.stage = "agent_auth"
    agent = ctx.setup.agent
    spec = installer.AGENTS[agent]
    await reply(
        f"🔐 Autenticación de {spec['label']}.\n\n"
        "Opción A — pegame una API key (la guardo en tu .env, no se comparte):\n"
        f"   conseguila en {spec['console_url']}\n\n"
        "Opción B — si preferís login por navegador, corré en la terminal del "
        f"servidor:\n   `{spec['login_cmd']}`\n"
        "seguí los pasos y después escribime *listo*."
    )


async def _agent_auth(ctx, chat_id, text, reply):
    agent = ctx.setup.agent
    spec = installer.AGENTS[agent]
    env_file = ctx.config.config_path.parent / ".env"

    if _looks_api_key(agent, text):
        # Treat as an API key paste.
        key = text.strip()
        installer.write_env_key(env_file, spec["env_key"], key)
        os.environ[spec["env_key"]] = key
        log.info("Stored %s for %s", spec["env_key"], agent)
        await reply("✅ Credencial guardada.")
        await _finish_auth(ctx, reply)
        return

    if _looks_affirmative(text):
        await reply("✅ Tomo tu login por navegador como hecho.")
        await _finish_auth(ctx, reply)
        return

    prefix = spec.get("key_prefix", "")
    await reply(
        "No reconocí eso como API key válida. "
        + (f"Para {spec['label']} debe empezar con `{prefix}`. " if prefix else "")
        + "Pegá la key completa, o escribí *listo* si ya te logueaste por navegador."
    )


async def _finish_auth(ctx, reply):
    agent = ctx.setup.agent
    _refresh_runner_executable(ctx, agent)
    ok, ver = await installer.verify_cli(agent)
    if ok:
        await reply(f"🧪 CLI verificado ({ver.splitlines()[0] if ver else 'ok'}).")
    await _enter_alma(ctx, reply)


# --------------------------------------------------------------------------- #
# Stage: alma (guided persona wizard)
# --------------------------------------------------------------------------- #
async def _enter_alma(ctx, reply):
    setup = ctx.setup
    setup.save_alma({"step": 0, "answers": {}})
    setup.stage = "alma"
    await reply(
        "🌱 Último paso: el *ALMA* de tu asistente.\n"
        "Te hago 6 preguntas cortas para escribir su personalidad y reglas."
    )
    await reply(ALMA_QUESTIONS[0][1])


async def _alma_answer(ctx, chat_id, text, reply):
    setup = ctx.setup
    alma = setup.alma
    step = int(alma.get("step", 0))
    answers = alma.get("answers", {})

    if not text:
        await reply("Escribime una respuesta de texto, por favor 🙂")
        return

    key = ALMA_QUESTIONS[step][0]
    answers[key] = text
    step += 1
    setup.save_alma({"step": step, "answers": answers})

    if step < len(ALMA_QUESTIONS):
        await reply(ALMA_QUESTIONS[step][1])
        return

    # All answers collected → write persona files and go live.
    _write_alma_files(ctx, answers)
    setup.stage = "done"
    name = answers.get("name", "tu asistente")
    await reply(
        f"🎉 ¡Listo! {name} está configurado y activo.\n\n"
        "Ya podés conversar normalmente. Escribí /capabilities para ver todo lo "
        "que puede hacer, o simplemente mandale un mensaje."
    )


def _write_alma_files(ctx, answers: dict) -> None:
    """Write persona/*.md + system_instruction.md, and apply it live."""
    base = ctx.config.config_path.parent
    persona = base / "persona"
    persona.mkdir(parents=True, exist_ok=True)

    name = answers.get("name", "Asistente")
    role = answers.get("role", "asistente personal")
    language = answers.get("language", "español")
    tone = answers.get("tone", "directo y claro")
    rules = answers.get("rules", "")
    owner = answers.get("owner", "")

    alma_md = (
        f"# ALMA de {name}\n\n"
        f"Sos **{name}**, {role}. Trabajás conversando por Telegram sobre un CLI "
        f"de IA con acceso a la shell, archivos, web y herramientas MCP.\n\n"
        f"## Identidad\n"
        f"- Nombre: {name}\n"
        f"- Rol: {role}\n"
        f"- Idioma por defecto: {language}\n"
        f"- Tono: {tone}\n"
    )
    (persona / "ALMA.md").write_text(alma_md, encoding="utf-8")

    rules_md = (
        f"# Reglas de {name}\n\n"
        + (f"- {rules}\n" if rules and rules.lower() not in ("ninguna", "no", "nada") else "- (sin reglas extra)\n")
        + "- Nunca imprimas secretos (tokens, contraseñas, API keys) en el chat.\n"
        + "- Confirmá antes de acciones destructivas o irreversibles.\n"
    )
    (persona / "REGLAS.md").write_text(rules_md, encoding="utf-8")

    contexto_md = (
        f"# Contexto del dueño\n\n"
        + (f"{owner}\n" if owner and owner.lower() not in ("nada", "no") else "(sin contexto adicional)\n")
    )
    (persona / "CONTEXTO.md").write_text(contexto_md, encoding="utf-8")

    system_instruction = (
        f"{alma_md}\n"
        f"## Estilo\n"
        f"- Respondé en {language}. Sé conciso y directo: esto es un chat, no un documento.\n"
        f"- Cuando corras un comando o cambies un archivo, decí en una línea qué hiciste.\n"
        f"- Si algo es ambiguo y el resultado importa, hacé una pregunta corta en vez de adivinar.\n\n"
        f"{rules_md}\n"
        f"{contexto_md}\n"
    )
    si_file = base / "system_instruction.md"
    si_file.write_text(system_instruction, encoding="utf-8")

    # Apply live: runners read config.system_instruction every turn.
    manifest = ctx.shared.get("capabilities_manifest", "")
    ctx.config.system_instruction = (
        system_instruction + ("\n\n" + manifest if manifest else "")
    )
    log.info("ALMA written to %s and applied live", si_file)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _looks_affirmative(text: str) -> bool:
    return text.strip().lower() in _AFFIRMATIVE


def _message_has_pairing_code(text: str, code: str) -> bool:
    normalized_text = text.upper().replace(" ", "")
    normalized_code = code.replace("-", "").upper()
    return normalized_code in normalized_text or code.upper() in text.upper()


def _looks_api_key(agent: str, text: str) -> bool:
    if not text or text.startswith("/"):
        return False
    spec = installer.AGENTS[agent]
    prefix = spec.get("key_prefix")
    value = text.strip()
    return bool(prefix and value.startswith(prefix) and len(value) > len(prefix) + 12)


def _refresh_runner_executable(ctx, agent: str) -> None:
    spec = installer.AGENTS[agent]
    path = installer.find_cli(spec["cli"])
    if not path:
        return
    try:
        runner = ctx.runners.get(agent)
        runner.executable = path
        log.info("Runner '%s' executable updated -> %s", agent, path)
    except KeyError:
        pass
