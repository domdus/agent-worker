"""User-facing texts in the language of Accept-Language (0.7.1)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from mcp import Client
from mcp import types as mtypes

from agent_worker import i18n
from agent_worker import mcp_server
from agent_worker import openai_compat
from agent_worker.jobs import Job, JobManager, Store
from agent_worker.mcp_server import build_mcp

# The languages of AI Workspace (domdus/ha-ai-workspace custom_components/ai_workspace/translations).
AI_WORKSPACE = {"az", "cs", "de", "en", "eo", "es", "fr", "it", "ko", "nl", "pl", "ru", "sv", "zh-Hans", "zh-Hant"}


# ---- the catalogue ---------------------------------------------------------------------------------


def test_the_languages_are_those_of_ai_workspace():
    assert set(i18n.LANGUAGES) == AI_WORKSPACE and len(i18n.LANGUAGES) == 15
    assert set(i18n.MESSAGES) == AI_WORKSPACE


@pytest.mark.parametrize("lang", sorted(AI_WORKSPACE))
def test_every_key_exists_in_every_language(lang):
    english = i18n.MESSAGES["en"]
    table = i18n.MESSAGES[lang]
    assert set(table) == set(english), {"missing": set(english) - set(table), "extra": set(table) - set(english)}
    for key, text in table.items():
        assert isinstance(text, str) and text.strip(), key


@pytest.mark.parametrize("lang", sorted(AI_WORKSPACE))
def test_placeholders_match_english(lang):
    for key, template in i18n.MESSAGES["en"].items():
        assert i18n.placeholders(i18n.MESSAGES[lang][key]) == i18n.placeholders(template), (lang, key)


@pytest.mark.parametrize("lang", sorted(AI_WORKSPACE))
def test_every_text_renders(lang):
    for key, template in i18n.MESSAGES["en"].items():
        values = {name: f"<{name}>" for name in i18n.placeholders(template)}
        text = i18n.t(lang, key, **values)
        assert all(value in text for value in values.values()), (lang, key)


@pytest.mark.parametrize("lang", sorted(AI_WORKSPACE))
def test_commands_and_machine_names_stay(lang):
    # approve / deny are what the user types; the setting names and endpoints are as the docs say.
    assert "**approve**" in i18n.t(lang, "chat.approval_reply") and "**deny**" in i18n.t(lang, "chat.approval_reply")
    assert "POST /jobs/abc/approve" in i18n.t(lang, "input.how", job="abc")
    assert "MAX_CONCURRENT_JOBS=3" in i18n.t(lang, "step.wait_slot", limit=3)
    assert "APPROVAL_TIMEOUT_SECONDS" in i18n.t(lang, "reason.approval_timeout", seconds=600, what="")
    assert "JOB_IDLE_TIMEOUT_SECONDS" in i18n.t(lang, "reason.idle", seconds=5)


# ---- Accept-Language --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "lang"),
    [
        (None, "en"),
        ("", "en"),
        ("de", "de"),
        ("de-DE,de;q=0.9,en;q=0.8", "de"),
        ("en-US,en;q=0.9,de;q=0.8", "en"),
        ("fr-CH, fr;q=0.9, en;q=0.8, de;q=0.7, *;q=0.5", "fr"),
        ("ja,ko;q=0.5", "ko"),
        ("ja", "en"),
        ("*", "en"),
        ("de;q=0.2,nl;q=0.8", "nl"),
        ("de;q=0,sv", "sv"),
        ("zh-CN", "zh-Hans"),
        ("zh", "zh-Hans"),
        ("zh-Hans-SG", "zh-Hans"),
        ("zh-TW", "zh-Hant"),
        ("zh-HK,zh;q=0.8", "zh-Hant"),
        ("zh-Hant", "zh-Hant"),
        ("zh_hant", "zh-Hant"),
        ("pt-BR,eo;q=0.3", "eo"),
        ("az-Latn-AZ", "az"),
        ("cs-CZ", "cs"),
        ("garbage;;;q=x,,", "en"),
    ],
)
def test_negotiate(header, lang):
    assert i18n.negotiate(header) == lang


def test_from_headers_is_case_insensitive_for_plain_dicts():
    assert i18n.from_headers({"Accept-Language": "it-IT"}) == "it"
    assert i18n.from_headers({"accept-language": "pl"}) == "pl"
    assert i18n.from_headers(None) == "en"


def test_mcp_language_comes_from_the_request_headers():
    ctx = SimpleNamespace(headers={"accept-language": "ru-RU,ru;q=0.9"})
    assert mcp_server._lang(ctx) == "ru"
    assert mcp_server._lang(None) == "en"
    assert mcp_server._lang(SimpleNamespace(headers=None)) == "en"


# ---- messages kept for later -----------------------------------------------------------------------


def test_nested_messages_and_lists_render_per_language():
    note = i18n.msg("fb.limit", engine="claude", reset=i18n.msg("fb.reset", time="18:30"))
    wait = i18n.msg("wait.no_engine", notes=[note, i18n.msg("fb.not_signed_in", engine="cursor")],
                    when=i18n.msg("wait.resume_at", time="18:30"))
    assert i18n.render("de", wait) == (
        "Keine Engine frei (claude am Limit (Reset 18:30); cursor nicht angemeldet) – Job wartet und geht um 18:30 "
        "automatisch weiter"
    )
    assert i18n.english(wait).startswith("No engine available (claude at its usage limit (reset 18:30); cursor not")
    assert i18n.split(wait) == (i18n.english(wait), wait)
    assert i18n.split("plain") == ("plain", None)
    with pytest.raises(KeyError):
        i18n.msg("no.such.key")


def test_event_text():
    step = i18n.msg("step.running")
    assert i18n.event_text({"type": "status", "text": "Running", "i18n": step}, "fr") == "En cours"
    assert i18n.event_text({"type": "status", "text": "Running", "i18n": step}, "en") == "Running"
    tool = {"type": "tool", "phase": "start", "text": "Calling Bash..."}
    assert i18n.event_text(tool, "de") == "Ruft Bash auf..."
    assert i18n.event_text(tool, "en") == "Calling Bash..."
    # Anything else (engine output, an ssh label with its command) stays as it is.
    assert i18n.event_text({"type": "tool", "phase": "start", "text": "ssh root@prod: reboot"}, "de") == (
        "ssh root@prod: reboot")


# ---- MCP ---------------------------------------------------------------------------------------------


def _job(**fields) -> Job:
    base = dict(id="job1", task="t", workspace="default", session="", model="m", profile="full", origin="mcp")
    base.update(fields)
    return Job(**base)


def test_approval_question_in_german():
    approval = {"id": "a1", "tool": "ssh", "host": "prod", "hostname": "10.0.0.5", "user": "root",
                "command": "systemctl restart nginx"}
    text = mcp_server._approval_message(_job(), approval, "de")
    assert text == ("Agent-Job job1 möchte ssh auf root@prod (10.0.0.5) ausführen:\n\nsystemctl restart nginx\n\n"
                    "Erlauben?")
    english = mcp_server._approval_message(_job(), {**approval, "command": ""})
    assert english.startswith("Agent job job1 wants to run ssh on root@prod (10.0.0.5):")
    assert "(interactive session or file transfer)" in english and english.endswith("Allow it?")


def test_elicitation_schema_is_localized_but_keeps_its_fields():
    schema = mcp_server._schema("nl")
    assert set(schema["properties"]) == {"approve", "note"} and schema["required"] == ["approve"]
    assert schema["properties"]["note"]["title"] == "Notitie"
    assert schema["properties"]["note"]["description"] == "Optionele notitie voor de agent"
    model = mcp_server._answer_model("ko").model_json_schema()
    assert set(model["properties"]) == {"approve", "note"}
    assert model["properties"]["approve"]["title"] == "허용"
    assert mcp_server._schema()["properties"]["note"]["description"] == "Optional note for the agent"
    assert mcp_server._answer_model("en") is mcp_server.ApprovalAnswer


def test_status_message_per_language_keeps_the_status_name():
    job = _job(status="queued", waiting_for="slot")
    assert job.status_message() == "queued: waiting for a free slot (MAX_CONCURRENT_JOBS)"
    assert job.status_message(lang="es") == "queued: esperando un hueco libre (MAX_CONCURRENT_JOBS)"
    job = _job(status="input_required", pending_approval={"tool": "ssh", "host": "prod", "user": "", "command": "ls"})
    assert job.status_message() == (
        "input_required: waiting for a human to approve ssh on prod: ls (POST /jobs/job1/approve or MCP elicitation)")
    assert job.status_message(lang="de") == (
        "input_required: wartet darauf, dass ein Mensch ssh auf prod freigibt: ls "
        "(POST /jobs/job1/approve oder MCP-Elicitation)")


def test_final_progress_text():
    job = _job(status="cancelled", error="Cancelled.", error_i18n=i18n.msg("reason.cancelled"))
    assert mcp_server._final_text(job) == "cancelled"
    assert mcp_server._final_text(job, "de") == "abgebrochen (cancelled): Abgebrochen."
    assert mcp_server._final_text(_job(status="completed"), "sv") == "klart (completed)"


def _manager(settings, engine, **overrides):
    overrides.setdefault("mcp_wait_seconds", 1)
    overrides.setdefault("mcp_max_wait", 1)
    return JobManager(replace(settings, **overrides), engine, Store(settings.data_dir / "worker.sqlite"))


async def test_followed_call_asks_and_reports_in_the_request_language(settings, engine, monkeypatch):
    # The in-process client carries no HTTP headers: give the call the language its header would.
    monkeypatch.setattr(mcp_server, "_lang", lambda ctx: i18n.negotiate("de-DE,de;q=0.9"))
    manager = _manager(settings, engine, ssh_approval_hosts=("prod",))
    results: list = []

    async def hook(spec):
        results.append(await manager.remote_call(spec.workspace, "ssh", "prod", "prod", "root", "reboot"))

    engine.hook = hook
    asked: list = []

    async def elicit(_ctx, params):
        asked.append(params)
        return mtypes.ElicitResult(action="accept", content={"approve": True, "note": ""})

    messages: list[str] = []

    async def progress(_value, _total, message):
        messages.append(message or "")

    async with Client(build_mcp(manager), mode="legacy", elicitation_callback=elicit) as client:
        data = (await asyncio.wait_for(
            client.call_tool("agent_run", {"task": "reboot prod"}, progress_callback=progress), 15
        )).structured_content
    assert data["status"] == "completed" and results == [(True, "")]
    # Machine-readable fields stay English.
    assert set(data) >= {"job_id", "status", "status_message"}
    params = asked[0]
    assert params.message.startswith(f"Agent-Job {data['job_id']} möchte ssh auf root@prod ausführen")
    assert "reboot" in params.message and params.message.endswith("Erlauben?")
    schema = params.requested_schema
    assert schema["properties"]["note"]["title"] == "Notiz"
    assert any("fragt den Benutzer (Elicitation)" in m and "reboot" in m for m in messages)
    assert all(m.startswith(f"job {data['job_id']}: ") for m in messages)
    assert messages[-1].endswith("abgeschlossen (completed)")


# ---- /v1 --------------------------------------------------------------------------------------------


def test_chat_approval_prompt_in_french():
    job = _job(pending_approval={"tool": "ssh", "host": "prod", "user": "root", "command": "reboot"})
    text = openai_compat.approval_prompt(job, "fr")
    assert "**Autorisation requise :** `ssh` sur **root@prod**" in text
    assert "```\nreboot\n```" in text and "**approve**" in text and "**deny**" in text
    assert "Reply **approve** to run it or **deny** to stop." in openai_compat.approval_prompt(job)


@pytest.mark.parametrize("word", ["oui", "Ja", "да", "是", "jes", "approve"])
def test_yes_words(word):
    assert openai_compat.decision_word(word) is True


@pytest.mark.parametrize("word", ["non", "nej", "нет", "拒绝", "deny"])
def test_no_words(word):
    assert openai_compat.decision_word(word) is False


def test_chat_answer_for_a_stopped_job():
    job = _job(status="failed", error="The job ran longer than JOB_MAX_RUNTIME_SECONDS (5s) and was stopped.",
               error_i18n=i18n.msg("reason.runtime", seconds=5))
    assert openai_compat._answer(job, "it") == (
        "**Errore di Agent Worker:** Il job è durato più di JOB_MAX_RUNTIME_SECONDS (5s) ed è stato fermato.")
    assert openai_compat._answer(job) == f"**Agent worker error:** {job.error}"
    assert openai_compat._answer(_job(status="cancelled"), "cs") == "(zrušeno)"
