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
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters
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


def register(ctx: "BotContext") -> None:
    app = ctx.connector.app
    setup = ctx.setup

    async def wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if setup is None or setup.is_done:
            return  # not in setup mode → let normal handlers run

        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None:
            return
        chat_id = chat.id
        text = (message.text or "").strip()

        async def reply(t: str):
            await context.bot.send_message(chat_id=chat_id, text=t)

        stage = setup.stage
        try:
            if stage == "pairing":
                await _pairing(ctx, chat_id, text, reply)
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

    # group -50 → runs before all normal handlers; self-disables when done.
    app.add_handler(MessageHandler(filters.ALL, wizard), group=-50)
    log.info("Onboarding wizard registered (current stage: %s)", setup.stage if setup else "n/a")


# --------------------------------------------------------------------------- #
# Stage: pairing
# --------------------------------------------------------------------------- #
async def _pairing(ctx, chat_id, text, reply):
    setup = ctx.setup
    code = setup.pairing_code
    if text.upper().replace(" ", "") == code.replace("-", "").upper() or text.upper() == code.upper():
        setup.paired_chat_id = chat_id
        ctx.config.allowed_chat_ids.add(int(chat_id))
        log.info("Bot paired to chat %s", chat_id)
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

    if text and not _looks_affirmative(text) and len(text) > 12 and not text.startswith("/"):
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

    await reply(
        "No reconocí eso como API key. Pegá la key completa, o escribí *listo* "
        "si ya te logueaste por navegador."
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
